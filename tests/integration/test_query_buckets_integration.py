"""
Live-server tests for the bucket-oriented v2 query (issue #16; plan/tickets/16/plan.md, PR B).

Every test ingests its own data through ingest_support and waits for the request's SUCCESS status before querying,
so there is no visibility polling: the status document is written after the buckets.  One ingest request makes one
bucket per column, which is what lets these tests reason about bucket boundaries exactly.

What only a live server shows, and so what these pin:
  - a bucket comes back whole and in its stored form: a SamplingClock axis verbatim, never trimmed to the query (T4);
  - limit counts buckets, and a PV's buckets reassemble exactly across pages (T5);
  - the stream returns the same buckets as the unary pages (T5);
  - ColumnMetadata and its provenance read back, and excludeColumnMetadata removes it (T6) -- the first live read
    of column metadata, since the samples path populates none.

Prerequisites: a live MLDP ecosystem, ingestion at localhost:50051 and query at localhost:50052; the tests self-skip
without one.  Each run ingests under run-unique PV names and leaves those samples behind -- the archive has no
delete RPC -- the same residue the other ingesting integration tests leave.
"""

import os
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client import MldpClient, PvQuery, QueryParams, from_epoch_nanos
from dp_python_lib.client import bucket_conversions as bc
from dp_python_lib.client import data_frame as dfb
from dp_python_lib.client import data_frame_conversions as dfc

from .ingest_support import INGESTION_ADDRESS, QUERY_ADDRESS, ingest_confirmed, register_provider, require_services

try:
    import pandas  # noqa: F401 -- availability probe for the [analysis] tests

    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


class TestQueryBucketsIntegration(unittest.TestCase):
    PERIOD = 1_000_000  # 1 ms
    CLOCK_COUNT = 10
    PAGED_BUCKETS = 3  # per PV, one ingest request apiece
    PAGED_COUNT = 5  # samples per paged bucket

    @classmethod
    def setUpClass(cls):
        require_services(("ingestion", INGESTION_ADDRESS), ("query", QUERY_ADDRESS))
        cls.client = MldpClient()
        cls.run_id = uuid.uuid4().hex[:12]
        cls.provider_name = f"itest_buckets_{cls.run_id}"
        # Whole seconds, so t0's epoch nanos are exact integer arithmetic.
        cls.t0 = datetime.fromtimestamp(int(time.time()) - 3600, tz=timezone.utc)
        cls.t0_nanos = int(cls.t0.timestamp()) * 1_000_000_000

        cls.pv_clock = cls._pv("CLOCK")
        cls.clock_values = [float(i) for i in range(cls.CLOCK_COUNT)]
        cls.pv_a, cls.pv_b = cls._pv("A"), cls._pv("B")
        cls.pv_meta = cls._pv("META")
        cls.metadata = dfb.column_metadata(
            tags=["itest", "derived"],
            attributes={"unit": "mm", "run": cls.run_id},
            provenance=dfb.provenance(
                source="itest",
                process="x2",
                derived_from=[dfb.pv_source(cls.pv_clock, (cls.t0, from_epoch_nanos(cls.t0_nanos + 10 * cls.PERIOD)))],
            ),
        )

        ingestion = cls.client.ingestion_client
        try:
            cls.provider_id = register_provider(ingestion, cls.provider_name)
            ingest_confirmed(ingestion, cls.provider_id, cls._clock_frame(cls.pv_clock, cls.clock_values))
            # A and B: PAGED_BUCKETS back-to-back buckets each, so a small limit has to page through both PVs.
            for pv, offset in ((cls.pv_a, 0.0), (cls.pv_b, 1000.0)):
                for n in range(cls.PAGED_BUCKETS):
                    start = cls.t0_nanos + n * cls.PAGED_COUNT * cls.PERIOD
                    values = [offset + n * cls.PAGED_COUNT + i for i in range(cls.PAGED_COUNT)]
                    ingest_confirmed(ingestion, cls.provider_id, cls._clock_frame(pv, values, start))
            meta_frame = dfb.data_frame(
                dfb.sampling_clock(cls.t0, period_nanos=cls.PERIOD, count=3),
                [dfb.double_column(cls.pv_meta, [0.0, 2.0, 4.0], metadata=cls.metadata)],
            )
            ingest_confirmed(ingestion, cls.provider_id, meta_frame)
        except (RuntimeError, TimeoutError) as e:
            raise unittest.SkipTest(f"could not ingest the bucket test data: {e}") from None

    @classmethod
    def _pv(cls, suffix):
        return f"ITEST:BUCKETS:{cls.run_id}:{suffix}"

    @classmethod
    def _clock_frame(cls, pv, values, start_nanos=None):
        start = from_epoch_nanos(cls.t0_nanos if start_nanos is None else start_nanos)
        return dfb.data_frame(
            dfb.sampling_clock(start, period_nanos=cls.PERIOD, count=len(values)), [dfb.double_column(pv, values)]
        )

    def _params(self, pvs, begin_offset, end_offset, **kwargs):
        return QueryParams(
            begin_time=from_epoch_nanos(self.t0_nanos + begin_offset),
            end_time=from_epoch_nanos(self.t0_nanos + end_offset),
            pv_selector=PvQuery.name_list(pvs),
            **kwargs,
        )

    def _one_bucket(self, params):
        result = self.client.query.query_buckets(params)
        self.assertFalse(result.result_status.is_error, result.result_status.message)
        self.assertEqual(result.next_page_token, "")
        self.assertEqual(len(result.data_buckets), 1)
        return result.data_buckets[0]

    @staticmethod
    def _key(bucket):
        """A bucket's identity for comparing result sets: PV, axis, and values."""
        return bucket.pvName, tuple(bc.bucket_timestamps(bucket)), tuple(bc.bucket_values(bucket))

    # ------------------------------------------------------------------
    # one bucket: stored form, whole, then trimmed
    # ------------------------------------------------------------------

    def test_a_sampling_clock_bucket_comes_back_verbatim(self):
        bucket = self._one_bucket(self._params([self.pv_clock], 0, self.CLOCK_COUNT * self.PERIOD))

        self.assertEqual(bucket.pvName, self.pv_clock)
        self.assertEqual(bucket.providerId, self.provider_id)
        self.assertEqual(bucket.providerName, self.provider_name)
        self.assertEqual(bucket.dataTimestamps.WhichOneof("value"), "samplingClock")
        clock = bucket.dataTimestamps.samplingClock
        self.assertEqual((clock.periodNanos, clock.count), (self.PERIOD, self.CLOCK_COUNT))
        self.assertEqual(clock.startTime.epochSeconds * 1_000_000_000 + clock.startTime.nanoseconds, self.t0_nanos)

        column = bc.bucket_column(bucket)
        self.assertEqual(type(column).__name__, "DoubleColumn")
        self.assertEqual(column.name, self.pv_clock)
        self.assertEqual(bc.bucket_values(bucket), self.clock_values)
        self.assertEqual(
            bc.bucket_timestamps(bucket), [self.t0_nanos + i * self.PERIOD for i in range(self.CLOCK_COUNT)]
        )

    def test_a_sub_window_returns_the_boundary_bucket_whole_and_trim_reduces_it_exactly(self):
        # [3 ms, 6 ms) lies inside the one 10-sample bucket: the server returns all 10, untrimmed.
        begin, end = 3 * self.PERIOD, 6 * self.PERIOD
        bucket = self._one_bucket(self._params([self.pv_clock], begin, end))
        self.assertEqual(bc.bucket_values(bucket), self.clock_values)

        trimmed = bc.trim_bucket(bucket, from_epoch_nanos(self.t0_nanos + begin), from_epoch_nanos(self.t0_nanos + end))
        self.assertIsNotNone(trimmed)
        # Half-open at both bounds, and still a clock -- its start shifted to the first retained sample.
        self.assertEqual(bc.bucket_values(trimmed), [3.0, 4.0, 5.0])
        self.assertEqual(trimmed.dataTimestamps.WhichOneof("value"), "samplingClock")
        self.assertEqual(bc.bucket_timestamps(trimmed), [self.t0_nanos + i * self.PERIOD for i in (3, 4, 5)])
        self.assertEqual(trimmed.providerId, bucket.providerId)

    @unittest.skipUnless(HAS_PANDAS, "requires the [analysis] extra")
    def test_query_buckets_to_dataframes_trims_only_when_asked(self):
        params = self._params([self.pv_clock], 3 * self.PERIOD, 6 * self.PERIOD)

        whole = bc.query_buckets_to_dataframes(self.client.query, params)[self.pv_clock]
        self.assertEqual(whole[self.pv_clock].tolist(), self.clock_values)

        trimmed = bc.query_buckets_to_dataframes(self.client.query, params, trim=True)[self.pv_clock]
        self.assertEqual(trimmed[self.pv_clock].tolist(), [3.0, 4.0, 5.0])
        self.assertEqual([ts.value for ts in trimmed.index], [self.t0_nanos + i * self.PERIOD for i in (3, 4, 5)])

    # ------------------------------------------------------------------
    # paging and streaming
    # ------------------------------------------------------------------

    def _paged_params(self, limit=0):
        return self._params([self.pv_a, self.pv_b], 0, self.PAGED_BUCKETS * self.PAGED_COUNT * self.PERIOD, limit=limit)

    def _expected_paged_values(self, offset):
        return [offset + i for i in range(self.PAGED_BUCKETS * self.PAGED_COUNT)]

    def test_limit_counts_buckets_and_pvs_reassemble_across_pages(self):
        pages = list(self.client.query.iter_query_buckets(self._paged_params(limit=2)))

        # 6 buckets at 2 per page: limit counts buckets, not samples (there are 30 samples).
        self.assertEqual([len(page.data_buckets) for page in pages], [2, 2, 2])
        buckets = [bucket for page in pages for bucket in page.data_buckets]

        # The server's observed order is (pvName, firstTime), so each PV's buckets arrive contiguous.  The client does
        # not rely on that (buckets_by_pv() sorts), but the cookbook describes it, so a change should be noticed.
        self.assertEqual([b.pvName for b in buckets], [self.pv_a] * 3 + [self.pv_b] * 3)

        grouped = bc.buckets_by_pv(reversed(buckets))  # deliberately out of order
        self.assertEqual(list(grouped), [self.pv_b, self.pv_a])
        for pv, offset in ((self.pv_a, 0.0), (self.pv_b, 1000.0)):
            values = [v for bucket in grouped[pv] for v in bc.bucket_values(bucket)]
            stamps = [t for bucket in grouped[pv] for t in bc.bucket_timestamps(bucket)]
            self.assertEqual(values, self._expected_paged_values(offset))
            self.assertEqual(stamps, [self.t0_nanos + i * self.PERIOD for i in range(len(values))])

    @unittest.skipUnless(HAS_PANDAS, "requires the [analysis] extra")
    def test_paged_buckets_assemble_into_one_dataframe_per_pv(self):
        frames = bc.query_buckets_to_dataframes(self.client.query, self._paged_params(limit=2))

        self.assertEqual(set(frames), {self.pv_a, self.pv_b})
        for pv, offset in ((self.pv_a, 0.0), (self.pv_b, 1000.0)):
            df = frames[pv]
            self.assertEqual(df[pv].tolist(), self._expected_paged_values(offset))
            self.assertEqual(len(df.attrs["buckets"]), self.PAGED_BUCKETS)
            self.assertTrue(all(entry["provider_id"] == self.provider_id for entry in df.attrs["buckets"]))
            self.assertTrue(all(entry["column_name"] == pv for entry in df.attrs["buckets"]))

    @unittest.skipUnless(HAS_PANDAS, "requires the [analysis] extra")
    def test_max_buckets_stops_paging(self):
        with self.assertRaisesRegex(ValueError, "max_buckets=3"):
            bc.query_buckets_to_dataframes(self.client.query, self._paged_params(limit=2), max_buckets=3)

    def test_the_stream_returns_the_same_buckets_as_the_unary_pages(self):
        unary = [
            b for page in self.client.query.iter_query_buckets(self._paged_params(limit=2)) for b in page.data_buckets
        ]
        messages = list(self.client.query.iter_query_buckets_stream(self._paged_params(limit=2)))
        streamed = [b for message in messages for b in message.data_buckets]

        self.assertTrue(all(message.next_page_token == "" for message in messages))
        self.assertGreater(len(messages), 1)  # the stream cuts messages by limit too
        self.assertEqual(sorted(map(self._key, streamed)), sorted(map(self._key, unary)))
        self.assertEqual(len(streamed), 2 * self.PAGED_BUCKETS)

    def test_an_empty_result_is_a_success_with_no_buckets(self):
        result = self.client.query.query_buckets(self._params([self._pv("NEVER-INGESTED")], 0, self.PERIOD))
        self.assertFalse(result.result_status.is_error, result.result_status.message)
        self.assertEqual(result.data_buckets, [])
        self.assertEqual(result.next_page_token, "")

    # ------------------------------------------------------------------
    # column metadata: the first path that reads it back live
    # ------------------------------------------------------------------

    def test_column_metadata_reads_back_and_is_absent_when_excluded(self):
        bucket = self._one_bucket(self._params([self.pv_meta], 0, 3 * self.PERIOD))
        column = bc.bucket_column(bucket)
        self.assertTrue(column.HasField("metadata"))
        self.assertEqual(column.metadata, self.metadata)
        summary = dfc.column_metadata_dict(column)
        self.assertEqual(summary["provenance"]["derived_from"][0]["pv_name"], self.pv_clock)

        excluded = self._one_bucket(self._params([self.pv_meta], 0, 3 * self.PERIOD, exclude_column_metadata=True))
        self.assertFalse(bc.bucket_column(excluded).HasField("metadata"))
        self.assertEqual(bc.bucket_values(excluded), [0.0, 2.0, 4.0])

    @unittest.skipUnless(HAS_PANDAS, "requires the [analysis] extra")
    def test_dataframe_attrs_carry_metadata_or_none_when_excluded(self):
        result = self.client.query.query_buckets(self._params([self.pv_meta], 0, 3 * self.PERIOD))
        df = result.to_dataframes()[self.pv_meta]
        self.assertEqual(df.attrs["column_metadata"][self.pv_meta]["tags"], ["itest", "derived"])

        # The query excluded it, so the buckets carry none -- reported as None, not as an empty summary.
        excluded = self.client.query.query_buckets(
            self._params([self.pv_meta], 0, 3 * self.PERIOD, exclude_column_metadata=True)
        )
        df = excluded.to_dataframes()[self.pv_meta]
        self.assertIsNone(df.attrs["buckets"][0]["column_metadata"])
        self.assertNotIn("column_metadata", df.attrs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
