import enum
import logging
import math
import numbers
import threading
import time
from abc import ABC
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, TypeVar

import grpc

from .result import ApiResultBase

ApiResultT = TypeVar("ApiResultT", bound=ApiResultBase, covariant=True)
_StreamResultT = TypeVar("_StreamResultT", bound=ApiResultBase)

DEFAULT_CALL_TIMEOUT_SECONDS = 300.0
"""Default deadline for a unary call (#81 D1).  A hang guard, not a performance budget."""

DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 300.0
"""Default stream idle timeout (#81 D3): the longest the client waits on the server for a stream's next step."""


class _Default(enum.Enum):
    """
    Type of the sentinel that keeps three per-call timeout cases apart: omitted (the client's default), a
    positive number, and None (no deadline at all).  An enum, so mypy can narrow on it.
    """

    DEFAULT = "DEFAULT"

    def __repr__(self) -> str:
        return "DEFAULT"


DEFAULT = _Default.DEFAULT
"""The default value of every per-call ``timeout`` / ``idle_timeout`` keyword: "use the client's default"."""

TimeoutArg: TypeAlias = float | _Default | None
"""What a per-call ``timeout`` / ``idle_timeout`` keyword accepts."""


def _is_seconds(value: object) -> bool:
    """
    Whether a value is a number of seconds: any real number but a bool.  numbers.Real rather than int | float, so a
    NumPy integer (np.int64 subclasses neither) is accepted like a Python int, as np.float64 already was.
    """
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _per_call_timeout(value: TimeoutArg, name: str = "timeout") -> TimeoutArg:
    """
    Validate a per-call timeout and return it with a number normalized to a Python float, for grpcio.  Rejects
    anything that is neither the sentinel, None, nor a positive finite number.  Zero is rejected rather than read
    as "disabled", because grpcio reads 0 as a deadline already passed while 0 in config means disabled, and
    accepting it here would leave one of those readings silently wrong (#81 D2, R4).
    """
    if value is DEFAULT or value is None:
        return value
    if not _is_seconds(value):
        raise TypeError(f"{name} must be a number of seconds or None, got {type(value).__name__}")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number of seconds, got {value!r}; pass {name}=None to disable it")
    return float(value)


def _client_timeout_seconds(value: float | None, name: str) -> float | None:
    """
    Normalize a client-level timeout (constructor argument or config value): None or 0 disables it, a negative or
    non-finite value is an error.
    """
    if value is None:
        return None
    if not _is_seconds(value):
        raise TypeError(f"{name} must be a number of seconds or None, got {type(value).__name__}")
    if math.isnan(value) or math.isinf(value) or value < 0:
        raise ValueError(f"{name} must be a non-negative number of seconds (0 disables it), got {value!r}")
    return float(value) if value > 0 else None


def _format_seconds(seconds: float) -> str:
    return f"{seconds:g} s"


@dataclass(frozen=True)
class ClientTimeout:
    """
    A client-level default timeout together with where it was set, so an error can tell the caller what to change
    (#81 R7).  ``source`` completes the sentence "client deadline 300 s, <source>", e.g.
    "set by MLDP_QUERY_CALL_TIMEOUT_SECONDS / query.call_timeout_seconds".  ``seconds`` None means disabled.
    """

    seconds: float | None
    source: str


@dataclass(frozen=True)
class _AppliedTimeout:
    """The deadline a call actually ran with; per_call is True when it came from the call's own timeout=."""

    seconds: float | None
    per_call: bool
    source: str = ""

    def deadline_hint(self) -> str:
        if self.seconds is None:
            return ""
        if self.per_call:
            return f" (deadline {_format_seconds(self.seconds)}, passed as timeout=)"
        return (
            f" (client deadline {_format_seconds(self.seconds)}, {self.source}; "
            "pass timeout= to override one call, or timeout=None to disable)"
        )


_LIBRARY_DEFAULT_SOURCE = "the library default; construct the client with {arg}= to change it"
_CONSTRUCTOR_SOURCE = "set by the {arg}= constructor argument"


def _client_timeout(value: TimeoutArg, library_default: float, arg: str, source: str | None) -> ClientTimeout:
    """
    Resolve a constructor's timeout argument into a ClientTimeout.  ``source`` overrides the description; it is how
    MldpClient names the config setting a value came from.  Otherwise the description says whether the value is
    the library default or the constructor argument.
    """
    if value is DEFAULT:
        seconds = _client_timeout_seconds(library_default, arg)
        described = source if source is not None else _LIBRARY_DEFAULT_SOURCE.format(arg=arg)
    else:
        seconds = _client_timeout_seconds(value, arg)
        described = source if source is not None else _CONSTRUCTOR_SOURCE.format(arg=arg)
    return ClientTimeout(seconds, described)


def idle_timeout_message(seconds: float) -> str:
    """The text an idle-timed-out stream reports after "gRPC error: " (#81 D3)."""
    return f"stream idle timeout: no message from the server within {_format_seconds(seconds)}"


class _IdleWatchdog:
    """
    Cancels a call when the client spends longer than ``bound`` seconds waiting on the server (#81 D3).

    One daemon thread per call, waiting on a Condition for a deadline that arm() sets to now + bound and disarm()
    clears, rather than a threading.Timer per wait, which would start a thread per streamed message (R2).  It fires
    at most once: on expiry it sets ``expired`` and calls the target, outside its lock, since a cancel can run
    callbacks that come back to disarm().  The target can be attached after construction (attach()), because a
    client-streaming call's request feed has to arm the watchdog before the call object exists; an expiry before
    attach() cancels on attach.

    With ``bound`` None the watchdog is disabled: no thread is started and every method is a no-op.  stop() must be
    called in the sender's finally; it ends the thread.
    """

    def __init__(self, bound: float | None, target: Callable[[], Any] | None = None, name: str = "call") -> None:
        self.bound = bound
        self._cond = threading.Condition()
        self._deadline: float | None = None
        self._expired = False
        self._stopped = False
        self._target = target
        self._thread: threading.Thread | None = None
        if bound is not None:
            self._thread = threading.Thread(target=self._run, name=f"dp-idle-watchdog-{name}", daemon=True)
            self._thread.start()

    @property
    def expired(self) -> bool:
        with self._cond:
            return self._expired

    def attach(self, target: Callable[[], Any]) -> None:
        with self._cond:
            self._target = target
            fire = self._expired
        if fire:
            target()

    def arm(self) -> None:
        if self.bound is None:
            return
        with self._cond:
            if self._expired or self._stopped:
                return
            self._deadline = time.monotonic() + self.bound
            self._cond.notify()

    def disarm(self) -> None:
        if self.bound is None:
            return
        with self._cond:
            self._deadline = None
            self._cond.notify()

    def stop(self) -> None:
        if self._thread is None:
            return
        with self._cond:
            self._stopped = True
            self._deadline = None
            self._cond.notify()
        if threading.current_thread() is not self._thread:
            self._thread.join()

    def message(self) -> str:
        """The idle-timeout error text, for a sender that has seen the call end with ``expired`` set."""
        assert self.bound is not None
        return idle_timeout_message(self.bound)

    def _run(self) -> None:
        with self._cond:
            while not self._stopped:
                if self._deadline is None:
                    self._cond.wait()
                    continue
                remaining = self._deadline - time.monotonic()
                if remaining > 0:
                    self._cond.wait(remaining)
                    continue
                self._expired = True
                self._deadline = None
                target = self._target
                break
            else:
                return
        if target is not None:
            target()


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

    def __init__(
        self,
        channel: grpc.Channel,
        stub_class: Callable[[grpc.Channel], Any],
        *,
        call_timeout: TimeoutArg = DEFAULT,
        stream_idle_timeout: TimeoutArg = DEFAULT,
        call_timeout_source: str | None = None,
        stream_idle_timeout_source: str | None = None,
    ) -> None:
        """
        :param channel: gRPC communication channel for the client's backend Service.
        :param stub_class: The generated gRPC stub class for the client's backend Service (e.g. DpIngestionServiceStub).
            It is instantiated once with the supplied channel and stored as self._stub.
        :param call_timeout: Default deadline, in seconds, for each unary call (and each page or chunk of a paging
            method).  Omitted: DEFAULT_CALL_TIMEOUT_SECONDS.  None or 0 disables it.  A call's own ``timeout=``
            overrides it.
        :param stream_idle_timeout: Default idle timeout, in seconds, for streaming calls: the longest the client
            waits on the server for a stream's next step.  Omitted: DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS.  None or 0
            disables it.  A call's own ``idle_timeout=`` overrides it.
        :param call_timeout_source: How a DEADLINE_EXCEEDED error describes where ``call_timeout`` was set, e.g.
            "set by MLDP_QUERY_CALL_TIMEOUT_SECONDS / query.call_timeout_seconds".  MldpClient passes it; a client
            built directly need not, and is described by whether the argument was given.
        :param stream_idle_timeout_source: The same, for ``stream_idle_timeout``.
        """
        self.logger = logging.getLogger(__name__)
        self._channel = channel
        self._stub = stub_class(channel)
        self._call_timeout = _client_timeout(
            call_timeout, DEFAULT_CALL_TIMEOUT_SECONDS, "call_timeout", call_timeout_source
        )
        self._stream_idle_timeout = _client_timeout(
            stream_idle_timeout, DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS, "stream_idle_timeout", stream_idle_timeout_source
        )
        self.logger.debug("Initialized service client with channel: %s, stub: %s", channel, stub_class.__name__)

    def _dispatch(
        self,
        stub_call: Callable[..., Any],
        request: Any,
        result_cls: ApiResultFactory[ApiResultT],
        success_field: str,
        op_name: str,
        request_log: Callable[[], None] | None = None,
        success_log: Callable[[Any], None] | None = None,
        rpc_error_hint: Callable[[grpc.RpcError], str] | None = None,
        timeout: TimeoutArg = DEFAULT,
        default_timeout: ClientTimeout | None = None,
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
        :param rpc_error_hint: Optional callable, passed a caught grpc.RpcError, returning text to APPEND to the
            "gRPC error: <details>" message -- advice that grpcio's bare details cannot give, such as pointing an
            oversized ingest at split_data_frame().  Return "" to add nothing.  It is appended, never substituted,
            because the message prefix is part of the result contract.
        :param timeout: The caller's per-call ``timeout=``, passed through unresolved: DEFAULT (the client's
            default), a positive number of seconds, or None for no deadline.  A DEADLINE_EXCEEDED error gets a hint,
            appended before any rpc_error_hint text, naming the deadline applied and where it came from.
        :param default_timeout: The default to resolve DEFAULT against, when not the client's call_timeout (export
            has its own; #81 Q1).
        :return: A result_cls instance with the method response and status information.
        :raises ValueError: if ``timeout`` is zero or negative (pass None to disable the deadline).
        """
        applied = self._resolve_call_timeout(timeout, default_timeout)

        if request_log is not None:
            request_log()
        else:
            self.logger.info("Calling %s API", op_name)

        try:
            self.logger.debug("Invoking stub.%s with request", op_name)
            # No timeout keyword at all when disabled, rather than timeout=None, so a caller who opts out makes
            # exactly the call the library made before #81.
            response = stub_call(request) if applied.seconds is None else stub_call(request, timeout=applied.seconds)
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
            if _rpc_code(e) == grpc.StatusCode.DEADLINE_EXCEEDED:
                error_msg += applied.deadline_hint()
            if rpc_error_hint is not None:
                error_msg += rpc_error_hint(e)
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

    def _iter_stream(
        self,
        stub_call: Callable[..., Iterable[Any]],
        request: Any,
        result_cls: Callable[..., _StreamResultT],
        success_field: str,
        op_name: str,
        *,
        timeout: float | None = None,
        idle_timeout: float | None = None,
    ) -> Generator[_StreamResultT, None, None]:
        """
        Invokes a server-streaming method, yielding one result per streamed message.  Shared by the query and sample
        status stream senders (#81 moved it here from QueryClient so both get the idle watchdog).

        Errors are yielded, not raised: a business error on a message, an unrecognized response, or a gRPC/unexpected
        error while iterating the stream each yield an is_error result, and a transport error terminates the
        generator after that final error item.  This is the internal contract -- the public iter_*_stream() wrappers
        consume these and convert the first error result into a RuntimeError, so callers of the public methods never
        see an error result yielded.

        Deliberately not routed through _dispatch(), which returns a single result rather than yielding many.

        The idle watchdog (#81 D3) is armed only while waiting in next() on the stream, so time the caller spends
        on a yielded result never counts.  On expiry it cancels the call, and the CANCELLED that follows is reported
        as "gRPC error: stream idle timeout: ..." rather than grpcio's "Locally cancelled by application!".

        Closing the generator before the stream ends (the public wrappers do, via contextlib.closing) cancels the
        call in the finally, rather than leaving it to grpcio's cancel on garbage collection.

        :param stub_call: The bound stub method (e.g. self._stub.querySamplesStream).
        :param request: The request for the call (must carry no page token).
        :param result_cls: The result class; called as result_cls(is_error=, message=, response=).
        :param success_field: The success oneof field of each response (e.g. "sampleQueryResult").
        :param op_name: The camelCase method name, for log and error messages.
        :param timeout: Resolved total deadline in seconds, or None for none.
        :param idle_timeout: Resolved idle bound in seconds, or None for none.
        :return: An iterator over the streamed result messages, possibly ending in an error result.
        """
        display_name = op_name[0].upper() + op_name[1:]
        self.logger.info("Calling %s API", op_name)

        watchdog = _IdleWatchdog(None)
        cancel: Callable[[], Any] | None = None
        try:
            self.logger.debug("Invoking stub.%s with request", op_name)
            stream = stub_call(request) if timeout is None else stub_call(request, timeout=timeout)
            # getattr: a mocked stub may return a plain iterator, which has nothing to cancel.
            cancel = getattr(stream, "cancel", None)
            watchdog = _IdleWatchdog(idle_timeout, cancel, name=op_name)
            responses = iter(stream)
            while True:
                watchdog.arm()
                try:
                    response = next(responses)
                except StopIteration:
                    break
                finally:
                    watchdog.disarm()
                if response.HasField("exceptionalResult"):
                    error_msg = response.exceptionalResult.message
                    self.logger.warning("%s returned business error: %s", display_name, error_msg)
                    yield result_cls(is_error=True, message=error_msg)
                elif response.HasField(success_field):
                    yield result_cls(is_error=False, message="", response=response)
                else:
                    error_msg = f"Unexpected response format: neither exceptionalResult nor {success_field} found"
                    self.logger.error(error_msg)
                    yield result_cls(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            if watchdog.expired:
                error_msg = f"gRPC error: {watchdog.message()}"
                self.logger.error("%s cancelled: %s", op_name, watchdog.message())
            else:
                error_msg = f"gRPC error: {e.details()}"
                if timeout is not None and _rpc_code(e) == grpc.StatusCode.DEADLINE_EXCEEDED:
                    error_msg += _AppliedTimeout(timeout, per_call=True).deadline_hint()
                self.logger.error("gRPC error during %s: %s", op_name, e.details())
            yield result_cls(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {e!s}"
            self.logger.exception("Unexpected error during %s: %s", op_name, str(e))
            yield result_cls(is_error=True, message=error_msg)
        finally:
            # A consumer that stops early closes this generator; cancel explicitly rather than leave the call to
            # grpcio's "Cancelled upon garbage collection!", which waits on the call object being collected.  On a
            # call that already finished it does nothing.
            if cancel is not None:
                cancel()
            watchdog.stop()

    def _resolve_call_timeout(self, timeout: TimeoutArg, default: ClientTimeout | None = None) -> _AppliedTimeout:
        """
        Resolve a unary call's ``timeout=`` against the client default (#81 D2).  Raises ValueError on a per-call
        value <= 0.
        """
        timeout = _per_call_timeout(timeout)
        if timeout is DEFAULT:
            client_default = default if default is not None else self._call_timeout
            return _AppliedTimeout(client_default.seconds, per_call=False, source=client_default.source)
        return _AppliedTimeout(timeout, per_call=True)

    def _resolve_stream_timeouts(
        self, timeout: TimeoutArg, idle_timeout: TimeoutArg
    ) -> tuple[float | None, float | None]:
        """
        Resolve a stream method's ``timeout=`` and ``idle_timeout=`` into (total deadline, idle bound).  Omitted, the
        total deadline is none -- deliberately unlike a unary call, since a healthy stream can run arbitrarily long
        and its default bound is the idle timeout (#81 D2, D3).
        """
        timeout = _per_call_timeout(timeout)
        idle_timeout = _per_call_timeout(idle_timeout, "idle_timeout")
        total = None if timeout is DEFAULT else timeout
        idle = self._stream_idle_timeout.seconds if idle_timeout is DEFAULT else idle_timeout
        return total, idle


def _rpc_code(e: grpc.RpcError) -> grpc.StatusCode | None:
    """e.code(), or None when it does not resolve: a bare grpc.RpcError, as the test mocks raise, has none."""
    try:
        code = e.code()
    except (AttributeError, TypeError):
        return None
    return code if isinstance(code, grpc.StatusCode) else None
