import logging
import os
import sys
import threading
import time
import unittest
from unittest.mock import Mock

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client.result import ApiResultBase
from dp_python_lib.client.service_api_client_base import (
    DEFAULT,
    DEFAULT_CALL_TIMEOUT_SECONDS,
    DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    ClientTimeout,
    ServiceApiClientBase,
    _IdleWatchdog,
)


class _FakeApiResult(ApiResultBase):
    """
    Stand-in for the concrete *ApiResult classes, which all share the (is_error, message, response) constructor
    that _dispatch relies on.
    """

    def __init__(self, is_error: bool, message: str, response=None) -> None:
        super().__init__(is_error, message)
        self.response = response


class _FakeClient(ServiceApiClientBase):
    """Minimal concrete client, so _dispatch can be exercised without a real service stub."""

    def __init__(self, channel, **kwargs) -> None:
        # A factory that ignores the channel: _dispatch is handed the bound stub method by the caller, so the
        # stub instance this creates is never exercised.
        super().__init__(channel, lambda _channel: Mock(), **kwargs)


def _response_with_field(field_name):
    """Build a Mock response whose HasField() returns True only for field_name."""
    response = Mock()
    response.HasField = Mock(side_effect=lambda field: field == field_name)
    return response


class TestDispatch(unittest.TestCase):
    """
    Unit tests for ServiceApiClientBase._dispatch, the shared three-tier sender.  The 18 unary _send_* methods that
    delegate to it are covered by their own per-client test modules; these tests cover the helper's own branches,
    including the RpcError code() guard that no per-client test reaches.
    """

    def setUp(self):
        self.client = _FakeClient(Mock())
        self.request = Mock()

    def _dispatch(self, stub_call, **kwargs):
        return self.client._dispatch(
            stub_call,
            self.request,
            _FakeApiResult,
            "someResult",
            "someOperation",
            **kwargs,
        )

    def test_success_returns_result_wrapping_response(self):
        response = _response_with_field("someResult")
        stub_call = Mock(return_value=response)

        result = self._dispatch(stub_call)

        stub_call.assert_called_once_with(self.request, timeout=DEFAULT_CALL_TIMEOUT_SECONDS)
        self.assertIsInstance(result, _FakeApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual("", result.result_status.message)
        self.assertIs(response, result.response)

    def test_business_error_returns_exceptional_result_message(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "PV not found"
        stub_call = Mock(return_value=response)

        result = self._dispatch(stub_call)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual("PV not found", result.result_status.message)
        self.assertIsNone(result.response)

    def test_unrecognized_response_is_an_error_naming_the_success_field(self):
        stub_call = Mock(return_value=_response_with_field("somethingElse"))

        result = self._dispatch(stub_call)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)
        self.assertIn("someResult", result.result_status.message)
        self.assertIsNone(result.response)

    def test_grpc_error_returns_details_message(self):
        error = grpc.RpcError()
        error.details = Mock(return_value="Connection timeout")
        error.code = Mock(return_value="UNAVAILABLE")
        stub_call = Mock(side_effect=error)

        result = self._dispatch(stub_call)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual("gRPC error: Connection timeout", result.result_status.message)
        self.assertIsNone(result.response)

    def test_grpc_error_without_resolvable_code_still_returns_details_message(self):
        # A bare RpcError has no usable code(); the helper must log without it rather than raise.  Test mocks raise
        # exactly this shape, which is why the guard exists.
        error = grpc.RpcError()
        error.details = Mock(return_value="Connection timeout")
        stub_call = Mock(side_effect=error)

        with self.assertLogs("dp_python_lib.client.service_api_client_base", level=logging.ERROR):
            result = self._dispatch(stub_call)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual("gRPC error: Connection timeout", result.result_status.message)

    def test_unexpected_exception_returns_unexpected_error_message(self):
        stub_call = Mock(side_effect=ValueError("Invalid parameter"))

        result = self._dispatch(stub_call)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual("Unexpected error: Invalid parameter", result.result_status.message)
        self.assertIsNone(result.response)

    def test_request_and_success_logs_are_invoked(self):
        response = _response_with_field("someResult")
        stub_call = Mock(return_value=response)
        request_log = Mock()
        success_log = Mock()

        self._dispatch(stub_call, request_log=request_log, success_log=success_log)

        request_log.assert_called_once_with()
        # The success log is handed the response, so senders can report counts read off the result.
        success_log.assert_called_once_with(response)

    def test_success_log_not_invoked_on_error(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "boom"
        request_log = Mock()
        success_log = Mock()

        self._dispatch(Mock(return_value=response), request_log=request_log, success_log=success_log)

        request_log.assert_called_once_with()
        success_log.assert_not_called()

    def test_business_error_log_names_the_operation_with_the_raw_op_name(self):
        # All three error tiers name the operation with the camelCase op_name.  The hand-written senders this
        # helper replaced capitalized it in this one log line only; asserting the format keeps the choice
        # deliberate, since nothing else in the suite covers log content.
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "boom"

        with self.assertLogs("dp_python_lib.client.service_api_client_base", level=logging.WARNING) as captured:
            self._dispatch(Mock(return_value=response))

        self.assertIn("someOperation API returned business error: boom", "\n".join(captured.output))

    def test_default_logs_used_when_callables_omitted(self):
        stub_call = Mock(return_value=_response_with_field("someResult"))

        with self.assertLogs("dp_python_lib.client.service_api_client_base", level=logging.INFO) as captured:
            self._dispatch(stub_call)

        self.assertIn("Calling someOperation API", "\n".join(captured.output))
        self.assertIn("someOperation completed successfully", "\n".join(captured.output))

    def test_rpc_error_hint_is_appended_to_the_grpc_error_message(self):
        error = grpc.RpcError()
        error.details = Mock(return_value="too big")
        stub_call = Mock(side_effect=error)
        hint = Mock(return_value=" (split it)")

        result = self._dispatch(stub_call, rpc_error_hint=hint)

        # Appended, never substituted: the "gRPC error: <details>" prefix is part of the result contract.
        self.assertEqual("gRPC error: too big (split it)", result.result_status.message)
        hint.assert_called_once_with(error)

    def test_rpc_error_hint_not_consulted_on_success_or_business_error(self):
        hint = Mock(return_value=" (unused)")
        for field in ("someResult", "exceptionalResult"):
            with self.subTest(field=field):
                response = _response_with_field(field)
                response.exceptionalResult.message = "rejected"
                self._dispatch(Mock(return_value=response), rpc_error_hint=hint)
        hint.assert_not_called()


def _deadline_exceeded():
    error = grpc.RpcError()
    error.details = Mock(return_value="Deadline Exceeded")
    error.code = Mock(return_value=grpc.StatusCode.DEADLINE_EXCEEDED)
    return error


class TestDispatchTimeout(unittest.TestCase):
    """The per-call deadline (#81 D1/D2): resolution, forwarding, validation, and the DEADLINE_EXCEEDED hint."""

    def setUp(self):
        self.request = Mock()

    def _dispatch(self, client, stub_call, **kwargs):
        return client._dispatch(stub_call, self.request, _FakeApiResult, "someResult", "someOperation", **kwargs)

    def _ok(self):
        return Mock(return_value=_response_with_field("someResult"))

    def test_omitted_timeout_forwards_the_client_default(self):
        stub_call = self._ok()
        self._dispatch(_FakeClient(Mock()), stub_call)
        stub_call.assert_called_once_with(self.request, timeout=DEFAULT_CALL_TIMEOUT_SECONDS)

    def test_explicit_default_sentinel_is_the_same_as_omitting_it(self):
        stub_call = self._ok()
        self._dispatch(_FakeClient(Mock(), call_timeout=12.5), stub_call, timeout=DEFAULT)
        stub_call.assert_called_once_with(self.request, timeout=12.5)

    def test_per_call_timeout_overrides_the_client_default(self):
        stub_call = self._ok()
        self._dispatch(_FakeClient(Mock(), call_timeout=12.5), stub_call, timeout=3)
        stub_call.assert_called_once_with(self.request, timeout=3)

    def test_per_call_none_sends_no_timeout_keyword(self):
        stub_call = self._ok()
        self._dispatch(_FakeClient(Mock()), stub_call, timeout=None)
        stub_call.assert_called_once_with(self.request)

    def test_client_timeout_zero_or_none_disables_the_default(self):
        for value in (0, 0.0, None):
            with self.subTest(value=value):
                stub_call = self._ok()
                self._dispatch(_FakeClient(Mock(), call_timeout=value), stub_call)
                stub_call.assert_called_once_with(self.request)

    def test_default_timeout_argument_replaces_the_client_default(self):
        # How export gets its own default (#81 Q1).
        stub_call = self._ok()
        self._dispatch(_FakeClient(Mock()), stub_call, default_timeout=ClientTimeout(3600.0, "set by X"))
        stub_call.assert_called_once_with(self.request, timeout=3600.0)

    def test_per_call_zero_or_negative_raises_naming_none(self):
        client = _FakeClient(Mock())
        for value in (0, 0.0, -1, float("nan"), float("inf")):
            with self.subTest(value=value):
                stub_call = self._ok()
                with self.assertRaisesRegex(ValueError, "timeout=None to disable"):
                    self._dispatch(client, stub_call, timeout=value)
                stub_call.assert_not_called()

    def test_numpy_integer_and_float_timeouts_are_accepted_and_forwarded_as_python_floats(self):
        # numbers.Real, not int | float: np.int64 subclasses neither, and was rejected with a TypeError.
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed (the [analysis] extra)")
        for value in (np.int64(30), np.int32(30), np.float64(30.0), np.float32(30.0)):
            with self.subTest(value=type(value).__name__):
                stub_call = self._ok()
                self._dispatch(_FakeClient(Mock(), call_timeout=value), stub_call, timeout=value)
                stub_call.assert_called_once_with(self.request, timeout=30.0)
                self.assertIs(type(stub_call.call_args.kwargs["timeout"]), float)
        with self.assertRaises(TypeError):
            self._dispatch(_FakeClient(Mock()), self._ok(), timeout=np.bool_(True))

    def test_per_call_non_number_raises_type_error(self):
        for value in ("30", True):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self._dispatch(_FakeClient(Mock()), self._ok(), timeout=value)

    def test_negative_client_timeout_is_rejected(self):
        for kwarg in ("call_timeout", "stream_idle_timeout"):
            with self.subTest(kwarg=kwarg), self.assertRaisesRegex(ValueError, kwarg):
                _FakeClient(Mock(), **{kwarg: -1})

    def test_stream_idle_timeout_defaults_and_disables(self):
        self.assertEqual(DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS, _FakeClient(Mock())._stream_idle_timeout.seconds)
        self.assertIsNone(_FakeClient(Mock(), stream_idle_timeout=0)._stream_idle_timeout.seconds)

    def test_hint_for_the_library_default_names_the_constructor_argument(self):
        result = self._dispatch(_FakeClient(Mock()), Mock(side_effect=_deadline_exceeded()))
        self.assertEqual(
            "gRPC error: Deadline Exceeded (client deadline 300 s, the library default; construct the client with "
            "call_timeout= to change it; pass timeout= to override one call, or timeout=None to disable)",
            result.result_status.message,
        )

    def test_hint_for_a_constructor_argument_says_so(self):
        result = self._dispatch(_FakeClient(Mock(), call_timeout=45), Mock(side_effect=_deadline_exceeded()))
        self.assertIn(
            "(client deadline 45 s, set by the call_timeout= constructor argument; pass timeout=",
            result.result_status.message,
        )

    def test_hint_for_a_configured_default_names_the_setting(self):
        client = _FakeClient(
            Mock(),
            call_timeout=600,
            call_timeout_source="set by MLDP_QUERY_CALL_TIMEOUT_SECONDS / query.call_timeout_seconds",
        )
        result = self._dispatch(client, Mock(side_effect=_deadline_exceeded()))
        self.assertEqual(
            "gRPC error: Deadline Exceeded (client deadline 600 s, set by MLDP_QUERY_CALL_TIMEOUT_SECONDS / "
            "query.call_timeout_seconds; pass timeout= to override one call, or timeout=None to disable)",
            result.result_status.message,
        )

    def test_hint_for_a_per_call_timeout(self):
        result = self._dispatch(_FakeClient(Mock()), Mock(side_effect=_deadline_exceeded()), timeout=2.5)
        self.assertEqual(
            "gRPC error: Deadline Exceeded (deadline 2.5 s, passed as timeout=)", result.result_status.message
        )

    def test_deadline_hint_precedes_an_rpc_error_hint(self):
        result = self._dispatch(
            _FakeClient(Mock()),
            Mock(side_effect=_deadline_exceeded()),
            timeout=2,
            rpc_error_hint=lambda e: " (other hint)",
        )
        self.assertEqual(
            "gRPC error: Deadline Exceeded (deadline 2 s, passed as timeout=) (other hint)",
            result.result_status.message,
        )

    def test_no_deadline_hint_on_other_codes(self):
        error = grpc.RpcError()
        error.details = Mock(return_value="down")
        error.code = Mock(return_value=grpc.StatusCode.UNAVAILABLE)
        result = self._dispatch(_FakeClient(Mock()), Mock(side_effect=error))
        self.assertEqual("gRPC error: down", result.result_status.message)


class TestResolveStreamTimeouts(unittest.TestCase):
    def test_omitted_total_is_none_and_idle_is_the_client_default(self):
        client = _FakeClient(Mock(), stream_idle_timeout=7)
        self.assertEqual((None, 7.0), client._resolve_stream_timeouts(DEFAULT, DEFAULT))

    def test_explicit_values_pass_through(self):
        self.assertEqual((30, None), _FakeClient(Mock())._resolve_stream_timeouts(30, None))

    def test_zero_idle_timeout_raises_naming_idle_timeout(self):
        with self.assertRaisesRegex(ValueError, "idle_timeout=None"):
            _FakeClient(Mock())._resolve_stream_timeouts(DEFAULT, 0)


class TestIterStreamCancel(unittest.TestCase):
    """_iter_stream() cancels the call in its finally, so closing it early does not wait on garbage collection."""

    def _stream(self, count):
        stream = Mock()
        stream.__iter__ = Mock(return_value=iter([_response_with_field("someResult") for _ in range(count)]))
        return stream

    def _iter(self, client, stream):
        return client._iter_stream(Mock(return_value=stream), Mock(), _FakeApiResult, "someResult", "someStream")

    def test_closing_the_generator_early_cancels_the_call(self):
        stream = self._stream(3)
        results = self._iter(_FakeClient(Mock()), stream)
        next(results)
        stream.cancel.assert_not_called()
        results.close()
        stream.cancel.assert_called_once_with()

    def test_a_stream_read_to_the_end_is_cancelled_harmlessly_once(self):
        # Cancelling a finished grpcio call does nothing; the point is that the finally always runs it.
        stream = self._stream(2)
        self.assertEqual(2, len(list(self._iter(_FakeClient(Mock()), stream))))
        stream.cancel.assert_called_once_with()

    def test_a_stream_without_cancel_is_fine(self):
        # A mocked stub may return a plain iterator.
        results = _FakeClient(Mock())._iter_stream(
            Mock(return_value=iter([_response_with_field("someResult")])), Mock(), _FakeApiResult, "someResult", "s"
        )
        self.assertEqual(1, len(list(results)))


def _watchdog_threads():
    # Counted by name rather than threading.active_count(), which also sees threads other tests leave winding down.
    return sum(1 for t in threading.enumerate() if t.name.startswith("dp-idle-watchdog"))


class TestIdleWatchdog(unittest.TestCase):
    """#81 D3 / R2: one thread per call, arm/disarm/expired/stop."""

    BOUND = 0.05

    def _wait_for(self, predicate, limit=2.0):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def test_expiry_cancels_and_sets_expired(self):
        cancel = Mock()
        watchdog = _IdleWatchdog(self.BOUND, cancel)
        try:
            watchdog.arm()
            self.assertTrue(self._wait_for(lambda: cancel.called))
            self.assertTrue(watchdog.expired)
            cancel.assert_called_once_with()
            self.assertEqual("stream idle timeout: no message from the server within 0.05 s", watchdog.message())
        finally:
            watchdog.stop()

    def test_disarm_before_expiry_does_not_cancel(self):
        cancel = Mock()
        watchdog = _IdleWatchdog(self.BOUND, cancel)
        try:
            watchdog.arm()
            watchdog.disarm()
            time.sleep(self.BOUND * 3)
            cancel.assert_not_called()
            self.assertFalse(watchdog.expired)
        finally:
            watchdog.stop()

    def test_unarmed_watchdog_never_fires(self):
        cancel = Mock()
        watchdog = _IdleWatchdog(self.BOUND, cancel)
        time.sleep(self.BOUND * 3)
        watchdog.stop()
        cancel.assert_not_called()

    def test_rearming_restarts_the_bound(self):
        cancel = Mock()
        watchdog = _IdleWatchdog(0.2, cancel)
        try:
            for _ in range(5):  # 5 x 0.1 s armed, each wait well within the bound
                watchdog.arm()
                time.sleep(0.1)
            cancel.assert_not_called()
        finally:
            watchdog.stop()

    def test_many_cycles_use_one_thread(self):
        before = _watchdog_threads()
        watchdog = _IdleWatchdog(10.0, Mock())
        try:
            self.assertEqual(before + 1, _watchdog_threads())
            for _ in range(2000):
                watchdog.arm()
                watchdog.disarm()
            self.assertEqual(before + 1, _watchdog_threads())
        finally:
            watchdog.stop()
        self.assertEqual(before, _watchdog_threads())

    def test_stop_ends_the_thread_while_armed(self):
        cancel = Mock()
        watchdog = _IdleWatchdog(10.0, cancel)
        watchdog.arm()
        watchdog.stop()
        self.assertFalse(watchdog._thread.is_alive())
        cancel.assert_not_called()

    def test_disabled_watchdog_starts_no_thread(self):
        before = _watchdog_threads()
        watchdog = _IdleWatchdog(None, Mock())
        watchdog.arm()
        watchdog.disarm()
        watchdog.stop()
        self.assertEqual(before, _watchdog_threads())
        self.assertFalse(watchdog.expired)

    def test_expiry_before_attach_cancels_on_attach(self):
        # The client-streaming feed arms the watchdog before the call object exists (T1 / D3).
        watchdog = _IdleWatchdog(self.BOUND)
        try:
            watchdog.arm()
            self.assertTrue(self._wait_for(lambda: watchdog.expired))
            cancel = Mock()
            watchdog.attach(cancel)
            cancel.assert_called_once_with()
        finally:
            watchdog.stop()

    def test_arm_after_expiry_is_ignored(self):
        cancel = Mock()
        watchdog = _IdleWatchdog(self.BOUND, cancel)
        try:
            watchdog.arm()
            self.assertTrue(self._wait_for(lambda: cancel.called))
            watchdog.arm()
            time.sleep(self.BOUND * 3)
            cancel.assert_called_once_with()
        finally:
            watchdog.stop()

    def test_stop_from_the_target_does_not_deadlock(self):
        # A cancel can run callbacks on the watchdog's own thread that end the call and so stop the watchdog.
        holder = {}
        stopped = threading.Event()

        def target():
            holder["w"].stop()
            stopped.set()

        holder["w"] = _IdleWatchdog(self.BOUND, target)
        holder["w"].arm()
        self.assertTrue(stopped.wait(2.0))


if __name__ == "__main__":
    unittest.main()
