import logging
import os
import sys
import unittest
from unittest.mock import Mock

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client.result import ApiResultBase
from dp_python_lib.client.service_api_client_base import ServiceApiClientBase


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

    def __init__(self, channel) -> None:
        # A factory that ignores the channel: _dispatch is handed the bound stub method by the caller, so the
        # stub instance this creates is never exercised.
        super().__init__(channel, lambda _channel: Mock())


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

        stub_call.assert_called_once_with(self.request)
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

    def test_default_logs_used_when_callables_omitted(self):
        stub_call = Mock(return_value=_response_with_field("someResult"))

        with self.assertLogs("dp_python_lib.client.service_api_client_base", level=logging.INFO) as captured:
            self._dispatch(stub_call)

        self.assertIn("Calling someOperation API", "\n".join(captured.output))
        self.assertIn("someOperation completed successfully", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
