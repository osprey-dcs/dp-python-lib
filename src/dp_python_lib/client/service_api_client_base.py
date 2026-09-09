import logging
from abc import ABC
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

import grpc

from .result import ApiResultBase

ApiResultT = TypeVar("ApiResultT", bound=ApiResultBase, covariant=True)


class ApiResultFactory(Protocol[ApiResultT]):
    """
    The constructor signature every concrete *ApiResult class shares.  ApiResultBase itself takes only
    (is_error, message); the 'response' keyword is added by each subclass, which declares it with the precise
    response type for its own API method.  _dispatch constructs result objects generically, so it is that shared
    three-argument shape -- not the base class -- that it actually depends on, and this Protocol is what states it.
    Typing result_cls with it keeps the checker honest about the 'response' keyword, which a bare
    type[ApiResultT] would reject.
    """

    def __call__(self, is_error: bool, message: str, response: Any = None) -> ApiResultT: ...


class ServiceApiClientBase(ABC):
    """
    This is the base class for the various service client classes.  It saves the specified channel and creates the
    service's gRPC stub once at construction time, so subclasses reuse a single stub instance across API calls rather
    than creating a new stub for each call.
    """

    def __init__(self, channel: grpc.Channel, stub_class: Callable[[grpc.Channel], Any]) -> None:
        """
        :param channel: gRPC communication channel for the client's backend Service.
        :param stub_class: The generated gRPC stub class for the client's backend Service (e.g. DpIngestionServiceStub).
            It is instantiated once with the supplied channel and stored as self._stub.
        """
        self.logger = logging.getLogger(__name__)
        self._channel = channel
        self._stub = stub_class(channel)
        self.logger.debug("Initialized service client with channel: %s, stub: %s", channel, stub_class.__name__)

    def _dispatch(
        self,
        stub_call: Callable[[Any], Any],
        request: Any,
        result_cls: ApiResultFactory[ApiResultT],
        success_field: str,
        op_name: str,
        request_log: Callable[[], None] | None = None,
        success_log: Callable[[Any], None] | None = None,
    ) -> ApiResultT:
        """
        Invokes a unary gRPC API method and applies the standard three-tier error handling shared by every unary
        _send_* method in the library:

        1. gRPC exceptions (grpc.RpcError) -- network/connection errors.
        2. Business logic errors -- the response carries an 'exceptionalResult'.
        3. General exceptions -- anything else raised while making the call.

        A response carrying neither 'exceptionalResult' nor the expected success field is itself an error, so an
        unrecognized response shape is never mistaken for a success.

        The server-streaming senders deliberately do not use this helper: they yield one result per streamed message
        (error results included, for the public iter_* wrapper to convert), which is a different contract from
        returning a single result.

        :param stub_call: The bound stub method to invoke, e.g. self._stub.savePvMetadata.
        :param request: The request message to pass to stub_call.
        :param result_cls: The *ApiResult class to construct; all of them take (is_error, message, response),
            the shape stated by the ApiResultFactory protocol.
        :param success_field: Name of the response's success oneof field, e.g. "savePvMetadataResult".
        :param op_name: API method name used in log and error messages, e.g. "savePvMetadata".
        :param request_log: Optional callable logging a method-specific message before the call.  When omitted, a
            generic "Calling <op_name> API" is logged instead.
        :param success_log: Optional callable, passed the response, logging a method-specific success message (some
            methods report a record count read off the response).  When omitted, a generic message is logged instead.
        :return: A result_cls instance with the method response and status information.
        """
        if request_log is not None:
            request_log()
        else:
            self.logger.info("Calling %s API", op_name)

        try:
            self.logger.debug("Invoking stub.%s with request", op_name)
            response = stub_call(request)
            self.logger.debug("Received response from %s API", op_name)

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                # op_name is used verbatim here, so all three error tiers name the operation the same way.  The
                # hand-written senders capitalized it on this line only ("SavePvMetadata API returned business
                # error"), while their gRPC-error and unexpected-error logs already used the camelCase name; the
                # inconsistency was not worth preserving.  Log text only -- the returned message is unchanged.
                self.logger.warning("%s API returned business error: %s", op_name, error_msg)
                return result_cls(is_error=True, message=error_msg)

            elif response.HasField(success_field):
                if success_log is not None:
                    success_log(response)
                else:
                    self.logger.info("%s completed successfully", op_name)
                return result_cls(is_error=False, message="", response=response)

            else:
                error_msg = f"Unexpected response format: neither exceptionalResult nor {success_field} found"
                self.logger.error(error_msg)
                return result_cls(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            # Safely get the error code -- it may not be available on a bare RpcError, as raised by test mocks.
            try:
                error_code = e.code()
                self.logger.error("gRPC error during %s: %s (code: %s)", op_name, e.details(), error_code)
            except (AttributeError, TypeError):
                self.logger.error("gRPC error during %s: %s", op_name, e.details())
            return result_cls(is_error=True, message=error_msg)

        except Exception as e:
            error_msg = f"Unexpected error: {e!s}"
            self.logger.exception("Unexpected error during %s: %s", op_name, str(e))
            return result_cls(is_error=True, message=error_msg)
