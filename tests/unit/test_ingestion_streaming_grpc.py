"""
The streaming ingest methods against a real, in-process grpcio server (#17, D5).

A mocked stub cannot show the behavior these tests exist for: grpcio consumes a request iterator on its own thread,
and when the iterator raises it cancels the call and reports only UNKNOWN "Exception iterating requests!".  The
client must re-raise the caller's own exception instead.  The server here listens on an ephemeral localhost port
and records what it received, so the tests can also check that requests sent before the failure did arrive.
"""

import contextlib
import os
import sys
import threading
import time
import unittest
from concurrent import futures

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client import data_frame as dfb
from dp_python_lib.client.ingestion_client import IngestDataRequestParams, IngestionClient
from dp_python_lib.grpc import ingestion_pb2, ingestion_pb2_grpc

REJECTED_ID = "reject-me"


class _RecordingServicer(ingestion_pb2_grpc.DpIngestionServiceServicer):
    """Acks every request except REJECTED_ID, and records the ids it received."""

    def __init__(self):
        self.received: list[str] = []
        self._lock = threading.Lock()

    def _record(self, request):
        with self._lock:
            self.received.append(request.clientRequestId)

    @staticmethod
    def _response(request):
        response = ingestion_pb2.IngestDataResponse(
            providerId=request.providerId, clientRequestId=request.clientRequestId
        )
        if request.clientRequestId == REJECTED_ID:
            response.exceptionalResult.message = "rejected by test server"
        else:
            response.ackResult.numRows = request.ingestionDataFrame.dataTimestamps.samplingClock.count
            response.ackResult.numColumns = len(request.ingestionDataFrame.doubleColumns)
        return response

    def ingestDataStream(self, request_iterator, context):
        ids, rejected = [], []
        for request in request_iterator:
            self._record(request)
            ids.append(request.clientRequestId)
            if request.clientRequestId == REJECTED_ID:
                rejected.append(request.clientRequestId)
        response = ingestion_pb2.IngestDataStreamResponse(clientRequestIds=ids, rejectedRequestIds=rejected)
        if rejected:
            response.exceptionalResult.message = "one or more requests were rejected"
        else:
            response.ingestDataStreamResult.numRequests = len(ids)
        return response

    def ingestDataBidiStream(self, request_iterator, context):
        for request in request_iterator:
            self._record(request)
            yield self._response(request)


def _params(request_id):
    frame = dfb.data_frame(dfb.sampling_clock(1_770_000_000, 1_000, 2), [dfb.double_column("pv", [1.0, 2.0])])
    return IngestDataRequestParams("provider-1", frame, client_request_id=request_id)


class _Boom(Exception):
    """The caller's own exception type, which the client must hand back unchanged."""


def _failing_after(count):
    """A request generator that yields `count` requests and then raises _Boom."""
    for index in range(count):
        yield _params(f"ok-{index}")
    raise _Boom("producer failed")


class TestStreamingAgainstInProcessServer(unittest.TestCase):
    def setUp(self):
        self.servicer = _RecordingServicer()
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        ingestion_pb2_grpc.add_DpIngestionServiceServicer_to_server(self.servicer, self.server)
        port = self.server.add_insecure_port("localhost:0")
        self.server.start()
        self.channel = grpc.insecure_channel(f"localhost:{port}")
        self.client = IngestionClient(self.channel)

    def tearDown(self):
        self.channel.close()
        self.server.stop(grace=None)

    # ---- the grpcio behavior D5 works around

    def test_grpcio_itself_hides_the_iterator_exception(self):
        # Pins the premise: through the raw stub, the caller's exception is gone.  If grpcio ever starts chaining or
        # re-raising it, this fails, and _RequestFeed can be reconsidered.
        stub = ingestion_pb2_grpc.DpIngestionServiceStub(self.channel)

        def requests():
            yield ingestion_pb2.IngestDataRequest(providerId="p", clientRequestId="r")
            raise _Boom("producer failed")

        with self.assertRaises(grpc.RpcError) as ctx:
            stub.ingestDataStream(requests())
        self.assertEqual(ctx.exception.code(), grpc.StatusCode.UNKNOWN)
        self.assertNotIsInstance(ctx.exception.__cause__, _Boom)

    # ---- ingestDataStream

    def test_stream_happy_path(self):
        result = self.client.ingest_data_stream(_params(f"id-{i}") for i in range(3))
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.num_requests, 3)
        self.assertEqual(result.client_request_ids, ["id-0", "id-1", "id-2"])
        self.assertEqual(result.rejected_request_ids, [])

    def test_stream_partial_reject_keeps_the_response(self):
        result = self.client.ingest_data_stream([_params("a"), _params(REJECTED_ID), _params("b")])
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.rejected_request_ids, [REJECTED_ID])
        self.assertEqual(result.client_request_ids, ["a", REJECTED_ID, "b"])
        self.assertIsNone(result.num_requests)

    def test_stream_reraises_the_callers_exception(self):
        with self.assertRaises(_Boom) as ctx:
            self.client.ingest_data_stream(_failing_after(2))
        self.assertEqual(str(ctx.exception), "producer failed")
        self.assertIsInstance(ctx.exception.__cause__, grpc.RpcError)
        if sys.version_info >= (3, 11):
            self.assertTrue(any("2 request(s) had been handed to gRPC" in note for note in ctx.exception.__notes__))
        # Handed over is not delivered: grpcio's cancellation races the sends, so the server holds some prefix of
        # the requests produced before the failure -- which is why the note says "any that reached the server".
        self.assertIn(self.servicer.received, (["ok-0", "ok-1"], ["ok-0"], []))

    def test_stream_reraises_when_the_first_request_fails(self):
        with self.assertRaises(_Boom):
            self.client.ingest_data_stream(_failing_after(0))
        self.assertEqual(self.servicer.received, [])

    # ---- ingestDataBidiStream

    def test_bidi_yields_one_result_per_request_including_rejects(self):
        results = list(self.client.iter_ingest_data_bidi_stream([_params("a"), _params(REJECTED_ID), _params("b")]))
        self.assertEqual([r.client_request_id for r in results], ["a", REJECTED_ID, "b"])
        self.assertEqual([r.result_status.is_error for r in results], [False, True, False])
        self.assertEqual(results[1].result_status.message, "rejected by test server")
        self.assertEqual((results[0].num_rows, results[0].num_columns), (2, 1))

    def test_bidi_reraises_the_callers_exception(self):
        seen = []
        with self.assertRaises(_Boom) as ctx:
            for result in self.client.iter_ingest_data_bidi_stream(_failing_after(2)):
                seen.append(result.client_request_id)
        self.assertIsInstance(ctx.exception.__cause__, grpc.RpcError)
        # As for the client stream, delivery of the requests before the failure races the cancellation, and so
        # does the arrival of their acks.
        self.assertIn(self.servicer.received, (["ok-0", "ok-1"], ["ok-0"], []))
        self.assertIn(seen, (["ok-0", "ok-1"], ["ok-0"], []))

    def _gated_producer(self):
        """1,000 requests whose producer blocks after three until the gate opens, and a record of what it made."""
        gate = threading.Event()
        produced = []

        def gated():
            for index in range(1_000):
                if index == 3:
                    gate.wait(5)
                produced.append(index)
                yield _params(f"n-{index}")

        return gated(), gate, produced

    def _assert_stopped_sending(self, gate, produced):
        gate.set()
        time.sleep(0.3)  # time enough for an uncancelled call to drain all 1,000
        self.assertLess(len(produced), 10)
        self.assertLess(len(self.servicer.received), 10)

    def test_bidi_stops_sending_when_the_results_are_closed(self):
        # grpcio pulls requests on its own thread.  The producer blocks after a few requests until the caller has
        # closed the results; without a cancel, grpcio would then drain the rest of it into the server.
        requests, gate, produced = self._gated_producer()
        results = self.client.iter_ingest_data_bidi_stream(requests)
        next(results)
        results.close()
        self._assert_stopped_sending(gate, produced)

    def test_bidi_stops_sending_on_a_break_inside_closing(self):
        # The documented way to stop early.  A bare break would not do: the loop's iterator stays referenced, so it
        # is not closed until collected, and grpcio keeps sending meanwhile.
        requests, gate, produced = self._gated_producer()
        with contextlib.closing(self.client.iter_ingest_data_bidi_stream(requests)) as results:
            for _result in results:
                break
        self._assert_stopped_sending(gate, produced)

    def test_bidi_transport_failure_raises_runtime_error(self):
        self.server.stop(grace=None)
        with self.assertRaises(RuntimeError) as ctx:
            list(self.client.iter_ingest_data_bidi_stream([_params("a")]))
        self.assertIn("gRPC error", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
