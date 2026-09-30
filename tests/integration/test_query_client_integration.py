import logging
import os
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client import data_frame as dfb
from dp_python_lib.client.mldp_client import MldpClient
from dp_python_lib.client.query_client import PvQuery, QueryParams
from dp_python_lib.client.time_conversions import from_epoch_nanos

from .ingest_support import ingest_confirmed, register_provider


class TestQueryClientIntegration(unittest.TestCase):
    """
    Integration tests for QueryClient that require a running MLDP query service.

    Prerequisites:
    - MLDP query service running (default at localhost:50052), with the v2 query handling enabled.  The closed-loop
      class also needs ingestion at localhost:50051.

    To run these tests:
    1. Start the MLDP ecosystem (e.g. docker compose up -d).
    2. Run: python -m unittest tests.integration.test_query_client_integration -v

    The mechanics tests below assume no particular data (a fresh database has none), so they assert only that the
    live RPCs complete and return well-formed results.  TestQueryClosedLoop ingests its own data and asserts the
    values exactly.
    """

    QUERY_ADDRESS = "localhost:50052"

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        cls.logger = logging.getLogger(__name__)
        cls.logger.info("Setting up query integration test environment")

        cls._verify_services_available()

        cls.client = MldpClient()
        cls.logger.info("MldpClient initialized successfully")

    @classmethod
    def _verify_services_available(cls):
        cls.logger.info("Checking if MLDP query service is available")
        try:
            channel = grpc.insecure_channel(cls.QUERY_ADDRESS)
            grpc.channel_ready_future(channel).result(timeout=5)
            cls.logger.info("Query service is reachable at %s", cls.QUERY_ADDRESS)
            channel.close()
        except grpc.FutureTimeoutError:
            raise unittest.SkipTest(
                f"MLDP query service not available at {cls.QUERY_ADDRESS}. "
                "Please start the MLDP ecosystem before running integration tests."
            ) from None
        except Exception as e:
            raise unittest.SkipTest(
                f"Cannot connect to MLDP query service: {e}. Please ensure the MLDP ecosystem is running."
            ) from None

    def _params(self):
        """A bounded, well-formed query over a recent one-hour window for a broad name pattern."""
        now = int(time.time())
        begin = now - 3600
        end = now
        return QueryParams(
            begin_time=begin,
            end_time=end,
            pv_selector=PvQuery.pattern(".*"),
            limit=100,
        )

    def test_query_client_available(self):
        """The query client should be wired up when a query channel is configured."""
        self.assertIsNotNone(self.client.query, "client.query should be initialized")

    def test_query_samples_mechanics(self):
        """
        A bounded querySamples() against the live server should complete without a transport/business error and
        return a well-formed page.  An empty result set is acceptable (no data assumed); the point is that the
        wire path, request construction, and response handling all work end-to-end.
        """
        result = self.client.query.query_samples(self._params())
        self.assertFalse(result.result_status.is_error, f"querySamples failed: {result.result_status.message}")

        table = result.column_table
        self.assertIsNotNone(table, "a successful querySamples should carry a ColumnTable")

        # Dense alignment invariant: every data column matches the timestamp count (holds even for 0 rows).
        n_rows = len(table.timestampList.timestamps)
        for column in table.dataColumns:
            self.assertEqual(
                len(column.dataValues),
                n_rows,
                f"column {column.name!r} is not index-aligned with the timestampList",
            )
        self.logger.info("querySamples returned %d row(s), %d column(s)", n_rows, len(table.dataColumns))

    def test_iter_query_samples_paging(self):
        """
        iter_query_samples() should iterate to completion against the live server without raising, exercising the
        transparent-paging loop (nextPageToken threading) regardless of how many pages come back.
        """
        pages = 0
        rows = 0
        for page in self.client.query.iter_query_samples(self._params()):
            pages += 1
            table = page.column_table
            if table is not None:
                rows += len(table.timestampList.timestamps)
        self.assertGreaterEqual(pages, 1, "iter_query_samples should yield at least one page")
        self.logger.info("iter_query_samples iterated %d page(s), %d total row(s)", pages, rows)

    def test_query_samples_stream_mechanics(self):
        """
        iter_query_samples_stream() should iterate the server stream to completion without raising.  An empty
        stream (zero messages) is acceptable; this exercises the streaming wire path and per-message handling.
        """
        messages = 0
        for page in self.client.query.iter_query_samples_stream(self._params()):
            messages += 1
            self.assertFalse(page.result_status.is_error)
        self.logger.info("iter_query_samples_stream received %d message(s)", messages)


class TestQueryClosedLoop(unittest.TestCase):
    """
    Ingest known data, query it back, and assert it exactly (#17 PR B).

    Two PVs on one run-unique prefix: A every 10 ms and B every 20 ms over the same start, so a query over both
    has rows where only A has a sample -- which is what dense alignment has to express.  Each ingest is confirmed
    through its request-status document before anything is queried, so there is no visibility polling: SUCCESS is
    written after the buckets.
    """

    PERIOD_A = 10_000_000
    PERIOD_B = 20_000_000
    COUNT_A = 50

    @classmethod
    def setUpClass(cls):
        for label, address in (("ingestion", "localhost:50051"), ("query", "localhost:50052")):
            channel = grpc.insecure_channel(address)
            try:
                grpc.channel_ready_future(channel).result(timeout=5)
            except grpc.FutureTimeoutError:
                raise unittest.SkipTest(f"MLDP {label} service not available at {address}") from None
            finally:
                channel.close()
        cls.client = MldpClient()
        run_id = uuid.uuid4().hex[:12]
        cls.pv_a = f"ITEST:QUERY:{run_id}:A"
        cls.pv_b = f"ITEST:QUERY:{run_id}:B"
        # Whole seconds, so t0's epoch nanos are exact integer arithmetic.
        cls.t0 = datetime.fromtimestamp(int(time.time()) - 3600, tz=timezone.utc)
        cls.t0_nanos = int(cls.t0.timestamp()) * 1_000_000_000
        cls.values_a = [float(i) for i in range(cls.COUNT_A)]
        cls.values_b = [100.0 + i for i in range(cls.COUNT_A // 2)]

        ingestion = cls.client.ingestion_client
        try:
            provider_id = register_provider(ingestion, f"itest_query_{run_id}")
            for pv, period, values in ((cls.pv_a, cls.PERIOD_A, cls.values_a), (cls.pv_b, cls.PERIOD_B, cls.values_b)):
                frame = dfb.data_frame(
                    dfb.sampling_clock(cls.t0, period_nanos=period, count=len(values)), [dfb.double_column(pv, values)]
                )
                ingest_confirmed(ingestion, provider_id, frame)
        except (RuntimeError, TimeoutError) as e:
            raise unittest.SkipTest(f"could not ingest the closed-loop test data: {e}") from None

    def _at(self, nanos_after_t0):
        return from_epoch_nanos(self.t0_nanos + nanos_after_t0)

    def _params(self, begin_offset, end_offset, pvs=None, limit=0):
        return QueryParams(
            begin_time=self._at(begin_offset),
            end_time=self._at(end_offset),
            pv_selector=PvQuery.name_list(pvs or [self.pv_a, self.pv_b]),
            limit=limit,
        )

    @staticmethod
    def _rows(table):
        """{column name: [(epoch nanos, value or None for a gap)]}, asserting dense alignment on the way."""
        stamps = [ts.epochSeconds * 1_000_000_000 + ts.nanoseconds for ts in table.timestampList.timestamps]
        out = {}
        for column in table.dataColumns:
            assert len(column.dataValues) == len(stamps), f"{column.name} is not aligned with the timestamps"
            out[column.name] = [
                (t, v.doubleValue if v.WhichOneof("value") is not None else None)
                for t, v in zip(stamps, column.dataValues, strict=True)
            ]
        return out

    def test_values_and_timestamps_round_trip_exactly(self):
        result = self.client.query.query_samples(self._params(0, self.COUNT_A * self.PERIOD_A, [self.pv_a]))
        self.assertFalse(result.result_status.is_error, result.result_status.message)
        self.assertEqual(
            self._rows(result.column_table)[self.pv_a],
            [(self.t0_nanos + i * self.PERIOD_A, v) for i, v in enumerate(self.values_a)],
        )

    def test_range_is_half_open_at_both_bounds(self):
        # [10 ms, 40 ms): the sample exactly at begin is kept, the one exactly at end is not, and so is the one
        # before begin -- both bounds fall inside the ingested bucket, so this is per-sample trimming.
        result = self.client.query.query_samples(self._params(self.PERIOD_A, 4 * self.PERIOD_A, [self.pv_a]))
        self.assertFalse(result.result_status.is_error, result.result_status.message)
        self.assertEqual(
            [t - self.t0_nanos for t, _ in self._rows(result.column_table)[self.pv_a]],
            [self.PERIOD_A, 2 * self.PERIOD_A, 3 * self.PERIOD_A],
        )

    def test_columns_on_different_clocks_are_densely_aligned(self):
        result = self.client.query.query_samples(self._params(0, 6 * self.PERIOD_A))
        self.assertFalse(result.result_status.is_error, result.result_status.message)
        rows = self._rows(result.column_table)
        self.assertEqual([v for _, v in rows[self.pv_a]], self.values_a[:6])
        # B has a sample on every other row; the rows between are gaps (an unset DataValue), not zeros.
        self.assertEqual([v for _, v in rows[self.pv_b]], [100.0, None, 101.0, None, 102.0, None])

    def test_paging_at_a_small_limit_concatenates_in_order(self):
        params = self._params(0, self.COUNT_A * self.PERIOD_A, [self.pv_a], limit=7)
        pages = list(self.client.query.iter_query_samples(params))
        self.assertGreater(len(pages), 1)
        rows = [row for page in pages for row in self._rows(page.column_table).get(self.pv_a, [])]
        self.assertEqual(rows, [(self.t0_nanos + i * self.PERIOD_A, v) for i, v in enumerate(self.values_a)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
