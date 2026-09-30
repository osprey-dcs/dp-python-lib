import logging
import os
import sys
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client import data_frame as dfb
from dp_python_lib.client import sample_status_conversions as ssc
from dp_python_lib.client.mldp_client import MldpClient
from dp_python_lib.client.query_client import PvQuery, QueryParams, SampleStatusFilter
from dp_python_lib.client.sample_status_client import (
    QuerySampleStatusesRequestParams,
    SampleStatusColumn,
    SampleStatusFrame,
    SaveSampleStatusesRequestParams,
    sampling_clock,
    timestamp_list,
)
from dp_python_lib.client.time_conversions import from_epoch_nanos

from .ingest_support import ingest_confirmed, register_provider

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _exact_nanos(value: datetime) -> int:
    """
    Converts a tz-aware datetime to epoch nanoseconds using integer arithmetic only.

    These tests exist to assert that timestamps round-trip *exactly*, so the expected value must not itself go
    through a float.  datetime.timestamp() returns a float whose ULP at present-day epochs is ~238ns -- it cannot
    represent nanoseconds at all -- so computing the expectation that way would be checking exactness against a
    value derived from the very representation the API's exact-match contract rules out.
    """
    return (value - _EPOCH) // timedelta(microseconds=1) * 1_000


class TestSampleStatusClientIntegration(unittest.TestCase):
    """
    Integration tests for SampleStatusClient that require a running MLDP ecosystem.

    Prerequisites:
    - MLDP services running (default annotation service at localhost:50053), however started
    - The Annotation Service must be dp-grpc 1.16.0 or later: the sample status API is new in 1.16.0

    To run these tests:
    1. Start the MLDP ecosystem
    2. Run: python -m unittest tests.integration.test_sample_status_client_integration -v

    Each test writes into a run-unique (domain, layer) and PV namespace and deletes what it wrote, so runs neither
    collide with each other nor with real data.
    """

    ANNOTATION_ADDRESS = "localhost:50053"

    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        cls.logger = logging.getLogger(__name__)
        cls.logger.info("Setting up sample status integration test environment")

        cls._verify_services_available()

        cls.client = MldpClient()
        cls.sample_status = cls.client.annotation.sample_status

        cls._verify_sample_status_api_available()

        # Run-unique namespace, so concurrent or repeated runs cannot see each other's statuses.
        cls.run_id = str(int(time.time() * 1000))
        cls.domain = f"itest_domain_{cls.run_id}"
        cls.layer = f"itest_layer_{cls.run_id}"
        cls.pv_name = f"ITEST:SAMPLE:STATUS:{cls.run_id}"

        # A fixed, whole-second base time well clear of "now", so the range is stable across the run.
        cls.base_time = datetime(2024, 2, 2, 18, 4, 12, tzinfo=timezone.utc)
        cls.range_begin = cls.base_time - timedelta(hours=1)
        cls.range_end = cls.base_time + timedelta(hours=1)

        cls.logger.info("Using domain=%s layer=%s pv=%s", cls.domain, cls.layer, cls.pv_name)

    @classmethod
    def _verify_services_available(cls):
        cls.logger.info("Checking if MLDP annotation service is available")
        try:
            channel = grpc.insecure_channel(cls.ANNOTATION_ADDRESS)
            grpc.channel_ready_future(channel).result(timeout=5)
            cls.logger.info("Annotation service is reachable at %s", cls.ANNOTATION_ADDRESS)
            channel.close()
        except grpc.FutureTimeoutError:
            raise unittest.SkipTest(
                f"MLDP annotation service not available at {cls.ANNOTATION_ADDRESS}. "
                "Please start the MLDP ecosystem before running integration tests."
            ) from None
        except Exception as e:
            raise unittest.SkipTest(
                f"Cannot connect to MLDP annotation service: {e}. Please ensure the MLDP ecosystem is running."
            ) from None

    @classmethod
    def _verify_sample_status_api_available(cls):
        """
        Skips if the reachable Annotation Service predates the sample status API.

        Reachability alone is not enough here: a pre-1.16.0 server accepts the connection and then answers every
        sample status call with UNIMPLEMENTED, which would surface as a wall of assertion failures rather than as
        the "backend not available" skip these tests intend.  Probe with a harmless query.
        """
        probe = QuerySampleStatusesRequestParams(
            begin_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
            pv_names=["__sample_status_capability_probe__"],
        )
        result = cls.sample_status.query_sample_statuses(probe)
        message = result.result_status.message or ""
        if result.result_status.is_error and "Method not found" in message:
            raise unittest.SkipTest(
                f"Annotation service at {cls.ANNOTATION_ADDRESS} does not implement the sample status API "
                f"({message}). It is new in dp-grpc 1.16.0; upgrade the server to run these tests."
            )
        cls.logger.info("Sample status API is available")

    def _cleanup(self, layer=None):
        """Removes everything this run wrote in the given layer, so a failed assertion cannot leak state."""
        self.sample_status.delete_sample_statuses(
            begin_time=self.range_begin,
            end_time=self.range_end,
            domain=self.domain,
            layer=layer or self.layer,
            all_pvs=True,
        )

    def _query_params(self, **overrides):
        """Builds query params over this run's range and namespace."""
        kwargs = {
            "begin_time": self.range_begin,
            "end_time": self.range_end,
            "pv_names": [self.pv_name],
            "domains": [self.domain],
        }
        kwargs.update(overrides)
        return QuerySampleStatusesRequestParams(**kwargs)

    def test_sparse_round_trip(self):
        """
        save (timestamp_list) -> query -> expand to rows -> delete -> query confirms deletion.

        Asserts real success at each step, and in particular that the timestamps come back matching what was sent:
        exact-timestamp matching is the property most likely to break silently against a real server.
        """
        self.addCleanup(self._cleanup)

        times = [
            self.base_time,
            self.base_time + timedelta(milliseconds=250),
            self.base_time + timedelta(milliseconds=500),
        ]
        frame = SampleStatusFrame(
            domain=self.domain,
            layer=self.layer,
            data_timestamps=timestamp_list(times),
            columns=[
                SampleStatusColumn(
                    pv_name=self.pv_name,
                    status_codes=[2, 2, 1],
                    confidence=[0.9, 0.8, 0.5],
                    reasons=["beam loss", "beam loss", "settling"],
                )
            ],
        )

        save_result = self.sample_status.save_sample_statuses(
            SaveSampleStatusesRequestParams(frames=[frame], source="integration test", modified_by="itest")
        )
        self.assertFalse(save_result.result_status.is_error, save_result.result_status.message)
        self.assertEqual(save_result.saved_count, 3)

        query_result = self.sample_status.query_sample_statuses(self._query_params())
        self.assertFalse(query_result.result_status.is_error, query_result.result_status.message)

        rows = ssc.buckets_to_rows(query_result.sample_status_buckets)
        self.assertEqual(len(rows), 3)

        # The timestamps must round-trip exactly -- statuses attach to samples by exact nanosecond match.
        expected_nanos = sorted(_exact_nanos(t) for t in times)
        self.assertEqual(sorted(r.epoch_nanos for r in rows), expected_nanos)

        by_nanos = {r.epoch_nanos: r for r in rows}
        first = by_nanos[expected_nanos[0]]
        self.assertEqual(first.status_code, 2)
        self.assertEqual(first.reason, "beam loss")
        self.assertAlmostEqual(first.confidence, 0.9, places=5)
        self.assertEqual(first.domain, self.domain)
        self.assertEqual(first.layer, self.layer)

        delete_result = self.sample_status.delete_sample_statuses(
            begin_time=self.range_begin,
            end_time=self.range_end,
            domain=self.domain,
            layer=self.layer,
            pv_names=[self.pv_name],
        )
        self.assertFalse(delete_result.result_status.is_error, delete_result.result_status.message)
        self.assertEqual(delete_result.deleted_count, 3)

        after = self.sample_status.query_sample_statuses(self._query_params())
        self.assertFalse(after.result_status.is_error, after.result_status.message)
        self.assertEqual(ssc.buckets_to_rows(after.sample_status_buckets), [])

    def test_dense_sampling_clock_round_trip(self):
        """
        Saves a SamplingClock-labeled interval and asserts the expanded timestamps reproduce the clock exactly.

        This is the path unit tests cannot falsify: expansion arithmetic is verified locally, but only a real server
        round-trip shows that what comes back lines up with what the clock described.
        """
        self.addCleanup(self._cleanup)

        count = 100
        period_nanos = 1_000_000  # 1 kHz
        axis = sampling_clock(start_time=self.base_time, period_nanos=period_nanos, count=count)
        status_codes = [i % 3 for i in range(count)]

        frame = SampleStatusFrame(
            domain=self.domain,
            layer=self.layer,
            data_timestamps=axis,
            columns=[SampleStatusColumn(pv_name=self.pv_name, status_codes=status_codes)],
        )
        save_result = self.sample_status.save_sample_statuses(
            SaveSampleStatusesRequestParams(frames=[frame], modified_by="itest")
        )
        self.assertFalse(save_result.result_status.is_error, save_result.result_status.message)
        self.assertEqual(save_result.saved_count, count)

        rows = list(ssc.iter_rows(self.sample_status.iter_sample_statuses(self._query_params())))
        self.assertEqual(len(rows), count)

        start_nanos = _exact_nanos(self.base_time)
        self.assertEqual(
            sorted(r.epoch_nanos for r in rows),
            [start_nanos + i * period_nanos for i in range(count)],
        )

        # confidence and reasons were never supplied; absence must stay absent rather than arriving as 0.0 / "".
        self.assertTrue(all(r.confidence is None for r in rows))
        self.assertTrue(all(r.reason is None for r in rows))

    def test_upsert_replaces_status_in_full(self):
        """
        Re-saving the same (pvName, timestamp, domain, layer) key replaces it: the status code changes and a
        previously-stored reason is cleared, rather than being merged forward.
        """
        self.addCleanup(self._cleanup)

        times = [self.base_time]
        original = SampleStatusFrame(
            domain=self.domain,
            layer=self.layer,
            data_timestamps=timestamp_list(times),
            columns=[SampleStatusColumn(self.pv_name, status_codes=[2], reasons=["beam loss"])],
        )
        self.assertFalse(
            self.sample_status.save_sample_statuses(
                SaveSampleStatusesRequestParams(frames=[original], modified_by="itest")
            ).result_status.is_error
        )

        # Same key, new code, reasons deliberately omitted.
        replacement = SampleStatusFrame(
            domain=self.domain,
            layer=self.layer,
            data_timestamps=timestamp_list(times),
            columns=[SampleStatusColumn(self.pv_name, status_codes=[1])],
        )
        self.assertFalse(
            self.sample_status.save_sample_statuses(
                SaveSampleStatusesRequestParams(frames=[replacement], modified_by="itest")
            ).result_status.is_error
        )

        rows = ssc.buckets_to_rows(self.sample_status.query_sample_statuses(self._query_params()).sample_status_buckets)
        self.assertEqual(len(rows), 1, "the upsert must replace the status, not add a second one")
        self.assertEqual(rows[0].status_code, 1)
        self.assertIsNone(rows[0].reason, "a full replace must clear the previously stored reason")

    def test_layers_do_not_collide(self):
        """
        The same PV and timestamp in two layers are two distinct statuses, and a layer filter separates them.
        This is what makes an operator override and a model's guess able to coexist.
        """
        other_layer = f"{self.layer}_alt"
        self.addCleanup(self._cleanup)
        self.addCleanup(self._cleanup, other_layer)

        times = [self.base_time]
        for layer, code in ((self.layer, 2), (other_layer, 7)):
            frame = SampleStatusFrame(
                domain=self.domain,
                layer=layer,
                data_timestamps=timestamp_list(times),
                columns=[SampleStatusColumn(self.pv_name, status_codes=[code])],
            )
            result = self.sample_status.save_sample_statuses(
                SaveSampleStatusesRequestParams(frames=[frame], modified_by="itest")
            )
            self.assertFalse(result.result_status.is_error, result.result_status.message)

        both = ssc.buckets_to_rows(self.sample_status.query_sample_statuses(self._query_params()).sample_status_buckets)
        self.assertEqual(len(both), 2)
        self.assertEqual({r.layer for r in both}, {self.layer, other_layer})

        filtered = ssc.buckets_to_rows(
            self.sample_status.query_sample_statuses(self._query_params(layers=[other_layer])).sample_status_buckets
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].status_code, 7)

    def test_streaming_returns_the_same_statuses(self):
        """The streaming RPC must surface the same statuses as the unary one for the same query."""
        self.addCleanup(self._cleanup)

        times = [self.base_time + timedelta(milliseconds=100 * i) for i in range(5)]
        frame = SampleStatusFrame(
            domain=self.domain,
            layer=self.layer,
            data_timestamps=timestamp_list(times),
            columns=[SampleStatusColumn(self.pv_name, status_codes=[1, 2, 1, 2, 1])],
        )
        self.assertFalse(
            self.sample_status.save_sample_statuses(
                SaveSampleStatusesRequestParams(frames=[frame], modified_by="itest")
            ).result_status.is_error
        )

        unary = list(ssc.iter_rows(self.sample_status.iter_sample_statuses(self._query_params())))
        streamed = list(ssc.iter_rows(self.sample_status.iter_sample_statuses_stream(self._query_params())))

        self.assertEqual(len(unary), 5)
        self.assertEqual(
            sorted((r.epoch_nanos, r.status_code) for r in unary),
            sorted((r.epoch_nanos, r.status_code) for r in streamed),
        )

    def test_delete_matching_nothing_is_success(self):
        """A delete that matches nothing is a success with deleted_count == 0, not an error."""
        result = self.sample_status.delete_sample_statuses(
            begin_time=self.range_begin,
            end_time=self.range_end,
            domain=f"{self.domain}_nonexistent",
            layer=self.layer,
            pv_names=[self.pv_name],
        )
        self.assertFalse(result.result_status.is_error, result.result_status.message)
        self.assertEqual(result.deleted_count, 0)

    def test_query_matching_nothing_is_success(self):
        """An empty result set is a success, not an error -- the caller must be able to tell them apart."""
        params = self._query_params(domains=[f"{self.domain}_nonexistent"])
        result = self.sample_status.query_sample_statuses(params)
        self.assertFalse(result.result_status.is_error, result.result_status.message)
        self.assertEqual(result.sample_status_buckets, [])


class TestSampleStatusQueryFiltering(unittest.TestCase):
    """
    A sample status filter on a live v2 query (#17 PR B): label some ingested samples, then query the data.

    Six samples a second apart.  Samples 1 and 4 are labeled code 2 ("bad"), sample 2 code 1 ("suspect"), the
    rest not at all.  exclude(code 2) must drop exactly samples 1 and 4 -- keeping the unlabeled ones, since an
    absent status asserts nothing -- and include(code 2) must keep only them.  The filter names the status code,
    so sample 2's other code proves the match is by code and not merely by "has a status".

    Needs ingestion (localhost:50051), query (localhost:50052), and annotation (localhost:50053).  The statuses are
    deleted afterwards; the ingested samples stay, under a run-unique PV name, as with every ingesting test.
    """

    BAD = 2
    SUSPECT = 1
    COUNT = 6
    PERIOD_NANOS = 1_000_000_000

    @classmethod
    def setUpClass(cls):
        for label, address in (
            ("ingestion", "localhost:50051"),
            ("query", "localhost:50052"),
            ("annotation", "localhost:50053"),
        ):
            channel = grpc.insecure_channel(address)
            try:
                grpc.channel_ready_future(channel).result(timeout=5)
            except grpc.FutureTimeoutError:
                raise unittest.SkipTest(f"MLDP {label} service not available at {address}") from None
            finally:
                channel.close()

        cls.client = MldpClient()
        cls.sample_status = cls.client.annotation.sample_status
        run_id = uuid.uuid4().hex[:12]
        cls.pv_name = f"ITEST:SAMPLE:FILTER:{run_id}"
        cls.domain = f"itest_filter_domain_{run_id}"
        cls.layer = f"itest_filter_layer_{run_id}"
        cls.t0 = datetime.fromtimestamp(int(time.time()) - 3600, tz=timezone.utc)
        cls.t0_nanos = _exact_nanos(cls.t0)
        cls.end = cls.t0 + timedelta(seconds=cls.COUNT)

        ingestion = cls.client.ingestion_client
        frame = dfb.data_frame(
            sampling_clock(cls.t0, period_nanos=cls.PERIOD_NANOS, count=cls.COUNT),
            [dfb.double_column(cls.pv_name, [float(i) for i in range(cls.COUNT)])],
        )
        try:
            ingest_confirmed(ingestion, register_provider(ingestion, f"itest_filter_{run_id}"), frame)
        except (RuntimeError, TimeoutError) as e:
            raise unittest.SkipTest(f"could not ingest test data: {e}") from None

        labeled = {1: cls.BAD, 2: cls.SUSPECT, 4: cls.BAD}
        saved = cls.sample_status.save_sample_statuses(
            SaveSampleStatusesRequestParams(
                frames=[
                    SampleStatusFrame(
                        domain=cls.domain,
                        layer=cls.layer,
                        data_timestamps=timestamp_list(
                            [from_epoch_nanos(cls.t0_nanos + i * cls.PERIOD_NANOS) for i in labeled]
                        ),
                        columns=[SampleStatusColumn(cls.pv_name, status_codes=list(labeled.values()))],
                    )
                ],
                source="dp-python-lib integration test",
                modified_by="dp-python-lib-integration-test",
            )
        )
        if saved.result_status.is_error:
            raise unittest.SkipTest(f"could not save sample statuses: {saved.result_status.message}")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "sample_status", None) is not None:
            cls.sample_status.delete_sample_statuses(
                begin_time=cls.t0, end_time=cls.end, domain=cls.domain, layer=cls.layer, pv_names=[cls.pv_name]
            )

    def _sample_indexes(self, sample_status_filter=None):
        """The indexes (0..COUNT-1) of the samples a query over the whole range returns."""
        params = QueryParams(
            begin_time=self.t0,
            end_time=self.end,
            pv_selector=PvQuery.name_list([self.pv_name]),
            sample_status_filter=sample_status_filter,
        )
        result = self.client.query.query_samples(params)
        self.assertFalse(result.result_status.is_error, result.result_status.message)
        table = result.column_table
        (column,) = [c for c in table.dataColumns if c.name == self.pv_name] or [None]
        if column is None:
            return []
        stamps = [ts.epochSeconds * 1_000_000_000 + ts.nanoseconds for ts in table.timestampList.timestamps]
        return [
            (t - self.t0_nanos) // self.PERIOD_NANOS
            for t, v in zip(stamps, column.dataValues, strict=True)
            if v.WhichOneof("value") is not None
        ]

    def test_unfiltered_query_returns_every_sample(self):
        self.assertEqual(self._sample_indexes(), list(range(self.COUNT)))

    def test_exclude_drops_exactly_the_matching_samples(self):
        self.assertEqual(
            self._sample_indexes(SampleStatusFilter.exclude(self.domain, status_codes=[self.BAD])), [0, 2, 3, 5]
        )

    def test_include_keeps_only_the_matching_samples(self):
        self.assertEqual(self._sample_indexes(SampleStatusFilter.include(self.domain, status_codes=[self.BAD])), [1, 4])

    def test_include_without_codes_keeps_every_labeled_sample(self):
        self.assertEqual(self._sample_indexes(SampleStatusFilter.include(self.domain)), [1, 2, 4])


if __name__ == "__main__":
    unittest.main()
