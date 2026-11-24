"""
Deadlines and stream idle timeouts (#81) against a real, in-process grpcio server.

A mocked stub cannot show what these exist for: a real deadline expiring, and the idle watchdog cancelling a real
call from its own thread while the caller is blocked in next().  The server listens on an ephemeral localhost port,
and its handlers hang, send promptly, or send slowly, as each test needs.  Bounds are fractions of a second, so the
module stays fast; each test also asserts it finished well inside a fixed limit, so a regression to "waits forever"
fails instead of hanging the suite.
"""

import os
import sys
import threading
import time
import unittest
from concurrent import futures
from datetime import datetime, timezone

import grpc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client.query_client import PvQuery, QueryClient, QueryParams
from dp_python_lib.grpc import query_pb2, query_pb2_grpc

IDLE = 0.5  # idle bound used by the stream tests, seconds
LIMIT = 5.0  # every test must finish within this, seconds


def _page():
    response = query_pb2.QuerySamplesResponse()
    response.sampleQueryResult.SetInParent()
    return response


class _Servicer(query_pb2_grpc.DpQueryServiceServicer):
    """mode: "hang" (never answer), "fast" (three messages at once), or "slow" (three, `delay` apart)."""

    def __init__(self):
        self.mode = "fast"
        self.delay = 0.0

    @staticmethod
    def _hang(context):
        while context.is_active():
            time.sleep(0.02)

    def querySamples(self, request, context):
        if self.mode == "hang":
            self._hang(context)
        return _page()

    def querySamplesStream(self, request, context):
        if self.mode == "hang":
            self._hang(context)
            return
        for _ in range(3):
            if self.mode == "slow":
                time.sleep(self.delay)
            yield _page()


def _watchdog_threads():
    return sum(1 for t in threading.enumerate() if t.name.startswith("dp-idle-watchdog"))


class TestTimeoutsAgainstInProcessServer(unittest.TestCase):
    def setUp(self):
        self.servicer = _Servicer()
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        query_pb2_grpc.add_DpQueryServiceServicer_to_server(self.servicer, self.server)
        port = self.server.add_insecure_port("localhost:0")
        self.server.start()
        self.channel = grpc.insecure_channel(f"localhost:{port}")
        self.client = QueryClient(self.channel)
        self.params = QueryParams(
            begin_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
            pv_selector=PvQuery.name_list(["PV:1"]),
        )
        self.started = time.monotonic()

    def tearDown(self):
        self.channel.close()
        self.server.stop(grace=None)
        self.assertLess(time.monotonic() - self.started, LIMIT)

    # ---- unary deadline

    def test_unary_call_to_a_hung_server_fails_with_deadline_exceeded_and_the_hint(self):
        self.servicer.mode = "hang"
        result = self.client.query_samples(self.params, timeout=0.3)
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(
            "gRPC error: Deadline Exceeded (deadline 0.3 s, passed as timeout=)", result.result_status.message
        )

    def test_unary_call_within_its_deadline_succeeds(self):
        result = self.client.query_samples(self.params, timeout=2)
        self.assertFalse(result.result_status.is_error)

    # ---- stream idle timeout

    def test_stream_from_a_hung_server_raises_the_idle_timeout(self):
        self.servicer.mode = "hang"
        began = time.monotonic()
        with self.assertRaises(RuntimeError) as ctx:
            list(self.client.iter_query_samples_stream(self.params, idle_timeout=IDLE))
        elapsed = time.monotonic() - began
        self.assertEqual(
            "querySamplesStream failed during streaming: gRPC error: stream idle timeout: no message from the server "
            "within 0.5 s",
            str(ctx.exception),
        )
        self.assertGreaterEqual(elapsed, IDLE)
        self.assertLess(elapsed, IDLE + 1.5)

    def test_slow_server_within_the_bound_and_a_slower_consumer_do_not_trip(self):
        # Server sends every 0.3 s (inside the 0.5 s bound); the consumer then spends 0.8 s (past it) on each
        # message.  Only time waiting on the server counts, so this completes.
        self.servicer.mode = "slow"
        self.servicer.delay = 0.3
        received = 0
        for _page_result in self.client.iter_query_samples_stream(self.params, idle_timeout=IDLE):
            received += 1
            time.sleep(0.8)
        self.assertEqual(3, received)

    def test_slow_server_past_the_bound_trips(self):
        self.servicer.mode = "slow"
        self.servicer.delay = 1.0
        with self.assertRaisesRegex(RuntimeError, "stream idle timeout"):
            list(self.client.iter_query_samples_stream(self.params, idle_timeout=IDLE))

    def test_idle_timeout_none_with_a_total_deadline(self):
        # No idle bound, so only the total deadline ends the call, and its error names it.
        self.servicer.mode = "hang"
        with self.assertRaises(RuntimeError) as ctx:
            list(self.client.iter_query_samples_stream(self.params, idle_timeout=None, timeout=0.5))
        self.assertIn("gRPC error: Deadline Exceeded (deadline 0.5 s, passed as timeout=)", str(ctx.exception))

    def test_default_idle_bound_lets_a_healthy_stream_complete(self):
        self.assertEqual(3, len(list(self.client.iter_query_samples_stream(self.params))))

    def test_watchdog_thread_ends_with_the_stream(self):
        before = _watchdog_threads()
        list(self.client.iter_query_samples_stream(self.params, idle_timeout=IDLE))
        self.assertEqual(before, _watchdog_threads())

    def test_closing_the_stream_early_ends_the_watchdog_thread(self):
        before = _watchdog_threads()
        stream = self.client.iter_query_samples_stream(self.params, idle_timeout=IDLE)
        next(stream)
        stream.close()
        self.assertEqual(before, _watchdog_threads())


if __name__ == "__main__":
    unittest.main()
