"""
Keepalive against a real, in-process grpcio server reached through a TCP proxy that can blackhole the path (#81 D5,
T6).

A blackholed path is the failure keepalive exists for: the proxy keeps both sockets open and silently discards
every byte, so neither end sees a FIN or RST and a quiet stream would wait forever.  With channel_options() the
client's unanswered ping fails the call with UNAVAILABLE; without them the call stays open.

The channel is built by ServiceConfig.create_channel(), so the test covers the config wiring as well as the
options.  Second-scale values keep it fast; grpc-core accepts them (measured: detection 0.7 s after the blackhole
at 0.5 s / 0.5 s).  Both halves have explicit time limits (plan R8): the negative half watches for exactly as long
as the positive half is allowed, since keepalive would already have fired by then, and each call also carries a
total gRPC deadline as a backstop so a regression fails instead of hanging the suite.
"""

import os
import socket
import sys
import threading
import time
import unittest
from concurrent import futures

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

import contextlib

from dp_python_lib.config import ServiceConfig

KEEPALIVE_TIME_SECONDS = 0.5
KEEPALIVE_TIMEOUT_SECONDS = 0.5
MARGIN_SECONDS = 1.0
DETECTION_LIMIT_SECONDS = KEEPALIVE_TIME_SECONDS + KEEPALIVE_TIMEOUT_SECONDS + MARGIN_SECONDS
"""How long after the blackhole the positive half may take, and exactly how long the negative half watches."""
BACKSTOP_DEADLINE_SECONDS = 10.0
"""Total gRPC deadline on each call, well above DETECTION_LIMIT_SECONDS, so a regression cannot hang the suite."""
SETTLE_SECONDS = 0.3
"""How long the call runs over a healthy path before the blackhole, so the stream is established end to end."""
TOTAL_BUDGET_SECONDS = 2 * (SETTLE_SECONDS + DETECTION_LIMIT_SECONDS) + 1.0
"""Both halves together must finish inside this, so a change that makes the test slow is noticed."""

_SERVICE = "keepalive.test.Quiet"
_METHOD = f"/{_SERVICE}/Hang"


def _hang(request, context):
    """A server stream that never sends: it stays open until the client goes away."""
    while context.is_active():
        time.sleep(0.05)
    return
    yield  # a generator, so grpcio treats this as a streaming handler


class _BlackholeProxy:
    """A TCP proxy that forwards until blackhole() and then silently discards everything, closing nothing."""

    def __init__(self, target_port: int) -> None:
        self._target_port = target_port
        self._blackholed = threading.Event()
        self._closed = False
        self._sockets: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        self._start(self._accept)

    def _start(self, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        with self._lock:
            self._threads.append(thread)
        thread.start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return  # listener closed
            upstream = socket.create_connection(("127.0.0.1", self._target_port))
            with self._lock:
                if self._closed:
                    client.close()
                    upstream.close()
                    return
                self._sockets += [client, upstream]
            self._start(self._pump, client, upstream)
            self._start(self._pump, upstream, client)

    def _pump(self, source: socket.socket, sink: socket.socket) -> None:
        while True:
            try:
                data = source.recv(65536)
                if not data:
                    return
                if not self._blackholed.is_set():
                    sink.sendall(data)
            except OSError:
                return  # socket closed by close()

    def blackhole(self) -> None:
        self._blackholed.set()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            sockets, threads = list(self._sockets), list(self._threads)
        self._listener.close()
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        for thread in threads:
            thread.join(timeout=2)


class TestKeepaliveThroughABlackholedPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        handler = grpc.method_handlers_generic_handler(_SERVICE, {"Hang": grpc.unary_stream_rpc_method_handler(_hang)})
        cls.server.add_generic_rpc_handlers((handler,))
        cls.port = cls.server.add_insecure_port("127.0.0.1:0")
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop(grace=None)

    def _quiet_stream_over_a_blackholed_path(self, service_config_kwargs: dict) -> tuple[grpc.RpcError | None, float]:
        """
        Opens a quiet server stream through a fresh proxy, blackholes the path, and waits up to
        DETECTION_LIMIT_SECONDS for the call to fail.

        :return: The RpcError the call failed with (None if it was still open at the limit, when it is cancelled
            here), and the seconds from the blackhole to the failure or the cancel.
        """
        proxy = _BlackholeProxy(self.port)
        self.addCleanup(proxy.close)
        config = ServiceConfig(host="127.0.0.1", port=proxy.port, **service_config_kwargs)
        channel = config.create_channel()
        self.addCleanup(channel.close)

        stream = channel.unary_stream(_METHOD)(b"", timeout=BACKSTOP_DEADLINE_SECONDS)
        outcome: dict = {}

        def consume() -> None:
            try:
                for _ in stream:
                    outcome["message"] = True
            except grpc.RpcError as e:
                outcome["error"] = e

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        consumer.join(SETTLE_SECONDS)
        self.assertTrue(consumer.is_alive(), f"the stream ended before the blackhole: {outcome}")

        proxy.blackhole()
        blackholed_at = time.monotonic()
        consumer.join(DETECTION_LIMIT_SECONDS)
        elapsed = time.monotonic() - blackholed_at
        if consumer.is_alive():
            stream.cancel()
            consumer.join(2)
            return None, elapsed
        self.assertNotIn("message", outcome)
        return outcome.get("error"), elapsed

    def test_keepalive_fails_the_quiet_stream_and_its_absence_does_not(self):
        started = time.monotonic()

        # The interval is deliberately below dp-service's 30 s minimum, which logs a WARNING; this server has no
        # such limit, and the pings never reach it anyway once the path is blackholed.
        with self.assertLogs("dp_python_lib.config.config", "WARNING"):
            error, elapsed = self._quiet_stream_over_a_blackholed_path(
                {
                    "keepalive_time_seconds": KEEPALIVE_TIME_SECONDS,
                    "keepalive_timeout_seconds": KEEPALIVE_TIMEOUT_SECONDS,
                }
            )
        self.assertIsNotNone(error, f"keepalive did not fail the call within {DETECTION_LIMIT_SECONDS} s")
        assert error is not None
        self.assertEqual(error.code(), grpc.StatusCode.UNAVAILABLE, error.details())
        self.assertIn("ping timeout", error.details() or "")
        self.assertLess(elapsed, DETECTION_LIMIT_SECONDS)

        # The same path without keepalive: still open when the positive half's limit has passed.
        error, elapsed = self._quiet_stream_over_a_blackholed_path({"keepalive_time_seconds": 0})
        self.assertIsNone(error, f"the call failed without keepalive: {error}")
        self.assertGreaterEqual(elapsed, DETECTION_LIMIT_SECONDS)

        self.assertLess(time.monotonic() - started, TOTAL_BUDGET_SECONDS)


if __name__ == "__main__":
    unittest.main()
