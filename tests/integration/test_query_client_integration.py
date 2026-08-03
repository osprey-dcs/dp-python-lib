import unittest
import time
import logging
import grpc
import sys
import os
from datetime import datetime, timezone

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from dp_python_lib.client.mldp_client import MldpClient
from dp_python_lib.client.query_client import QueryParams, PvQuery, ConfigQuery


class TestQueryClientIntegration(unittest.TestCase):
    """
    Integration tests for QueryClient that require a running MLDP query service.

    Prerequisites:
    - MLDP query service running (default at localhost:50052), with the v2 query handling enabled.

    To run these tests:
    1. Start the MLDP ecosystem (e.g. docker compose up -d).
    2. Run: python -m unittest tests.integration.test_query_client_integration -v

    Test-data note:
    The query service is read-only, and this library does not yet wrap a data-ingestion RPC, so these tests cannot
    create their own queryable data.  We deliberately do NOT assume the server is pre-populated with useful data
    (a fresh test database has no reason to contain any).  Therefore:
      - The tests below assert the query *mechanics* end-to-end (the live RPC completes, returns a well-formed
        result, tolerates an empty result set, and paging/streaming iterate correctly) without requiring any
        specific data to exist.
      - The full closed-loop assertions (ingest known data -> query it back -> assert exact values, trimmed
        half-open range boundaries, dense alignment on real columns, multi-page paging) are deferred until the
        ingestion API client lands.  See osprey-dcs/dp-python-lib#17; test_closed_loop_round_trip is skipped with
        that reference rather than silently omitted.
    """

    QUERY_ADDRESS = 'localhost:50052'

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
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
            )
        except Exception as e:
            raise unittest.SkipTest(
                f"Cannot connect to MLDP query service: {e}. "
                "Please ensure the MLDP ecosystem is running."
            )

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
        self.assertFalse(result.result_status.is_error,
                         f"querySamples failed: {result.result_status.message}")

        table = result.column_table
        self.assertIsNotNone(table, "a successful querySamples should carry a ColumnTable")

        # Dense alignment invariant: every data column matches the timestamp count (holds even for 0 rows).
        n_rows = len(table.timestampList.timestamps)
        for column in table.dataColumns:
            self.assertEqual(
                len(column.dataValues), n_rows,
                f"column {column.name!r} is not index-aligned with the timestampList")
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

    @unittest.skip(
        "Closed-loop ingest->query round-trip is deferred pending the ingestion API client "
        "(osprey-dcs/dp-python-lib#17): ingest a known dataset, query it back, and assert exact value "
        "round-trip, trimmed half-open [begin, end) boundaries, dense alignment on real columns, and "
        "multi-page paging."
    )
    def test_closed_loop_round_trip(self):
        # Implementation blocked on the ingestion client (#17).  Intended shape:
        #   1. Register a provider and ingest a small known dataset (known PV, known timestamps/values)
        #      over a controlled time window.
        #   2. querySamples() over [begin, end) for that PV; assert the exact values and timestamps round-trip.
        #   3. Assert half-open trimming: a sample exactly at `end` is excluded, one at `begin` is included.
        #   4. Assert dense column/timestamp alignment on the real columns.
        #   5. Ingest enough rows to force multiple pages at a small `limit`; assert iter_query_samples()
        #      concatenates them in order.
        raise NotImplementedError


if __name__ == '__main__':
    unittest.main(verbosity=2)
