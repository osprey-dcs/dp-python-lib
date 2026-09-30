"""
Live-server tests for IngestionClient (issue #17; plan/tickets/17/plan.md, PR B).

Every ingest here is closed-loop: an ack says only that a request passed validation, so each test confirms the
outcome through await_request_statuses() -- the request-status document is the one record of whether data landed.
Two tests exist precisely because the ack and the status disagree: an unknown providerId and a re-ingest of the
same PV + first timestamp are both ACKED, and both end in ERROR (T2, T5).

Prerequisites: a live MLDP ecosystem, ingestion at localhost:50051 and query at localhost:50052; the tests self-skip
without one.  Each run ingests under run-unique PV names and leaves those samples behind -- the archive has no
delete RPC -- the same residue the other ingesting integration tests leave.
"""

import logging
import os
import sys
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client import (
    IngestDataRequestParams,
    IngestionRequestStatus,
    MldpClient,
    PvQuery,
    QueryParams,
    RegisterProviderRequestParams,
    chunked_request_params,
)
from dp_python_lib.client import data_frame as dfb

from .ingest_support import INGESTION_ADDRESS, QUERY_ADDRESS, ingest_confirmed, register_provider, require_services

# The server's bucket-span cap (Buckets.maxBucketSpanSeconds, 86400 s by default).  The client deliberately leaves
# caps to the server, so a frame spanning more than this passes every client check and is rejected by the server
# at validation -- which makes it the reject the stream tests need.
SERVER_MAX_SPAN_SECONDS = 86_400


class TestIngestionClientIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        require_services(("ingestion", INGESTION_ADDRESS), ("query", QUERY_ADDRESS))
        cls.client = MldpClient()
        cls.ingestion = cls.client.ingestion_client
        cls.run_id = uuid.uuid4().hex[:12]
        cls.provider_id = register_provider(cls.ingestion, f"itest_ingestion_{cls.run_id}")
        # Whole seconds, well in the past, so no test's samples straddle "now" or collide across tests: each test
        # takes its own PV name, and a PV's bucket id is its name plus its first timestamp.
        cls.t0 = datetime.fromtimestamp(int(time.time()) - 3600, tz=timezone.utc)

    def _pv(self, suffix):
        return f"ITEST:INGEST:{self.run_id}:{suffix}"

    def _frame(self, pv, values, start=None, period_nanos=1_000_000):
        return dfb.data_frame(
            dfb.sampling_clock(start or self.t0, period_nanos=period_nanos, count=len(values)),
            [dfb.double_column(pv, list(values))],
        )

    def _over_span_frame(self, pv):
        """Two samples further apart than the server's span cap: valid to the client, rejected by the server."""
        return self._frame(pv, [1.0, 2.0], period_nanos=(SERVER_MAX_SPAN_SECONDS + 1) * 1_000_000_000)

    def _statuses(self, request_ids, since):
        """{request id: IngestionRequestStatus}, asserting exactly one document per id."""
        found = self.ingestion.await_request_statuses(self.provider_id, request_ids, since=since)
        for request_id, documents in found.items():
            self.assertEqual(len(documents), 1, f"{request_id} has {len(documents)} status documents")
        return {rid: IngestionRequestStatus(docs[0].ingestionRequestStatus) for rid, docs in found.items()}

    def _query_values(self, pv, begin, end):
        """(epoch nanos, value) pairs for one PV over [begin, end), across every page."""
        params = QueryParams(begin_time=begin, end_time=end, pv_selector=PvQuery.name_list([pv]), limit=1000)
        rows = []
        for page in self.client.query.iter_query_samples(params):
            table = page.column_table
            columns = [c for c in table.dataColumns if c.name == pv]
            if not columns:
                continue
            for ts, value in zip(table.timestampList.timestamps, columns[0].dataValues, strict=True):
                rows.append((ts.epochSeconds * 1_000_000_000 + ts.nanoseconds, value.doubleValue))
        return rows

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------

    def test_registering_the_same_name_again_returns_the_same_provider(self):
        again = self.ingestion.register_provider(
            RegisterProviderRequestParams(
                f"itest_ingestion_{self.run_id}", description=None, tag_list=None, attribute_map=None
            )
        )
        self.assertFalse(again.result_status.is_error, again.result_status.message)
        self.assertEqual(again.provider_id, self.provider_id)
        self.assertFalse(again.is_new_provider)

    # ------------------------------------------------------------------
    # unary
    # ------------------------------------------------------------------

    def test_unary_ingest_is_acked_and_reaches_success(self):
        pv = self._pv("unary")
        params = IngestDataRequestParams(self.provider_id, self._frame(pv, [0.1, 0.2, 0.3]))
        since = datetime.now(timezone.utc)
        ack = self.ingestion.ingest_data(params)
        self.assertFalse(ack.result_status.is_error, ack.result_status.message)
        self.assertEqual(ack.client_request_id, params.client_request_id)
        self.assertEqual((ack.num_rows, ack.num_columns), (3, 1))
        self.assertEqual(
            self._statuses([params.client_request_id], since),
            {params.client_request_id: IngestionRequestStatus.SUCCESS},
        )
        # SUCCESS means queryable: the values come straight back, nanosecond-exact.
        base = int(self.t0.timestamp()) * 1_000_000_000
        self.assertEqual(
            self._query_values(pv, self.t0, self.t0 + timedelta(seconds=1)),
            [(base, 0.1), (base + 1_000_000, 0.2), (base + 2_000_000, 0.3)],
        )

    def test_an_unknown_provider_id_is_acked_and_then_ends_in_error(self):
        # Contrary to the proto comments (osprey-dcs/dp-grpc#165): the provider is looked up only by the async job.
        bogus_provider = uuid.uuid4().hex[:24]
        params = IngestDataRequestParams(bogus_provider, self._frame(self._pv("bogus-provider"), [1.0]))
        since = datetime.now(timezone.utc)
        ack = self.ingestion.ingest_data(params)
        self.assertFalse(ack.result_status.is_error, "an unknown providerId is expected to be ACKED")
        found = self.ingestion.await_request_statuses(bogus_provider, [params.client_request_id], since=since)
        (document,) = found[params.client_request_id]
        self.assertEqual(document.ingestionRequestStatus, IngestionRequestStatus.ERROR)
        self.assertIn("providerId", document.statusMessage)

    def test_reingesting_the_same_pv_and_first_timestamp_is_acked_and_then_ends_in_error(self):
        pv = self._pv("duplicate")
        ingest_confirmed(self.ingestion, self.provider_id, self._frame(pv, [1.0, 2.0]))
        params = IngestDataRequestParams(self.provider_id, self._frame(pv, [3.0, 4.0]))
        since = datetime.now(timezone.utc)
        ack = self.ingestion.ingest_data(params)
        self.assertFalse(ack.result_status.is_error, "a duplicate bucket is expected to be ACKED")
        self.assertEqual(
            self._statuses([params.client_request_id], since),
            {params.client_request_id: IngestionRequestStatus.ERROR},
        )
        # The first ingest's values stand.
        self.assertEqual(
            [value for _, value in self._query_values(pv, self.t0, self.t0 + timedelta(seconds=1))], [1.0, 2.0]
        )

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------

    def _three_requests_middle_rejected(self, label):
        return [
            IngestDataRequestParams(self.provider_id, self._frame(self._pv(f"{label}-a"), [1.0, 2.0])),
            IngestDataRequestParams(self.provider_id, self._over_span_frame(self._pv(f"{label}-b"))),
            IngestDataRequestParams(self.provider_id, self._frame(self._pv(f"{label}-c"), [5.0, 6.0])),
        ]

    def test_stream_reports_the_rejected_request_and_ingests_the_others(self):
        requests = self._three_requests_middle_rejected("stream")
        ids = [r.client_request_id for r in requests]
        since = datetime.now(timezone.utc)
        summary = self.ingestion.ingest_data_stream(requests)

        # A partial reject is an error result that KEEPS its response, and does not raise.
        self.assertTrue(summary.result_status.is_error)
        self.assertEqual(summary.rejected_request_ids, [ids[1]])
        self.assertEqual(summary.client_request_ids, ids)
        self.assertIsNone(summary.num_requests)
        self.assertEqual(
            self._statuses(ids, since),
            {
                ids[0]: IngestionRequestStatus.SUCCESS,
                ids[1]: IngestionRequestStatus.REJECTED,
                ids[2]: IngestionRequestStatus.SUCCESS,
            },
        )

    def test_bidi_yields_one_result_per_request_in_order_including_the_reject(self):
        requests = self._three_requests_middle_rejected("bidi")
        ids = [r.client_request_id for r in requests]
        since = datetime.now(timezone.utc)
        results = list(self.ingestion.iter_ingest_data_bidi_stream(requests))

        self.assertEqual([r.client_request_id for r in results], ids)
        self.assertEqual([r.result_status.is_error for r in results], [False, True, False])
        self.assertTrue(results[1].result_status.message)
        self.assertEqual(
            self._statuses(ids, since),
            {
                ids[0]: IngestionRequestStatus.SUCCESS,
                ids[1]: IngestionRequestStatus.REJECTED,
                ids[2]: IngestionRequestStatus.SUCCESS,
            },
        )

    def test_a_chunked_frame_ingests_as_n_requests_and_reads_back_whole(self):
        pv = self._pv("chunked")
        n = 2_000
        values = [i * 0.5 for i in range(n)]
        frame = self._frame(pv, values)
        # Small enough to force several chunks; still room for the two budgeted worst-case ids (2 x 1,024 bytes).
        chunks = list(dfb.split_data_frame(frame, max_bytes=8_000))
        self.assertGreater(len(chunks), 2)

        base = f"itest-chunked-{self.run_id}"
        since = datetime.now(timezone.utc)
        summary = self.ingestion.ingest_data_stream(chunked_request_params(self.provider_id, chunks, base))
        self.assertFalse(summary.result_status.is_error, summary.result_status.message)
        self.assertEqual(summary.num_requests, len(chunks))
        ids = [f"{base}-{i}" for i in range(len(chunks))]
        self.assertEqual(summary.client_request_ids, ids)
        self.assertEqual(set(self._statuses(ids, since).values()), {IngestionRequestStatus.SUCCESS})

        rows = self._query_values(pv, self.t0, self.t0 + timedelta(seconds=10))
        base_nanos = int(self.t0.timestamp()) * 1_000_000_000
        self.assertEqual(rows, [(base_nanos + i * 1_000_000, v) for i, v in enumerate(values)])

    # ------------------------------------------------------------------
    # the server's inbound message limit
    # ------------------------------------------------------------------

    def _oversized_frame(self, pv):
        """Comfortably over the server's default 4,096,000-byte inbound limit: 600k doubles is ~5.4 MB."""
        return self._frame(pv, [0.0] * 600_000, period_nanos=100_000)

    def test_an_oversized_unary_request_fails_with_the_split_hint(self):
        # Over the limit the call fails as RESOURCE_EXHAUSTED -- a transport error, not a reject -- and the hint is
        # gated on the server's size-violation text, so this pins that text as well as the hint.
        result = self.ingestion.ingest_data(
            IngestDataRequestParams(self.provider_id, self._oversized_frame(self._pv("big")))
        )
        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error:", result.result_status.message)
        self.assertIn("split_data_frame", result.result_status.message)

    def test_an_oversized_request_on_a_stream_fails_the_call_with_the_split_hint(self):
        requests = [
            IngestDataRequestParams(self.provider_id, self._frame(self._pv("before-big"), [1.0])),
            IngestDataRequestParams(self.provider_id, self._oversized_frame(self._pv("big-streamed"))),
        ]
        summary = self.ingestion.ingest_data_stream(requests)
        self.assertTrue(summary.result_status.is_error)
        self.assertIn("split_data_frame", summary.result_status.message)
        self.assertIsNone(summary.response, "the call failed as a whole, so there is no summary response")

    # ------------------------------------------------------------------
    # non-scalar columns (verifiable only as far as SUCCESS until #16 wraps queryBuckets)
    # ------------------------------------------------------------------

    def test_non_scalar_columns_reach_success(self):
        frame = dfb.data_frame(
            dfb.sampling_clock(self.t0, period_nanos=1_000_000, count=2),
            [
                dfb.double_array_column(self._pv("waveform"), [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]),
                dfb.int32_array_column(self._pv("map"), [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]),
                dfb.image_column(
                    self._pv("camera"), [b"\x00\x01", b"\x02\x03"], width=1, height=2, channels=1, encoding="raw-u8"
                ),
                dfb.struct_column(self._pv("struct"), [b"\x0a\x01", b"\x0a\x02"], schema_id="itest:v1"),
                dfb.serialized_column(self._pv("serialized"), b"opaque", encoding="itest-bytes"),
            ],
        )
        ingest_confirmed(self.ingestion, self.provider_id, frame)


if __name__ == "__main__":
    unittest.main(verbosity=2)
