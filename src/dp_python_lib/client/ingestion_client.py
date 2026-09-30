"""
Ingestion Service client: provider registration, data ingestion (unary, client-streaming, bidirectional), and the
request-status query that says whether an ingestion actually landed.

Plan: plan/tickets/17/plan.md.  Server behaviors this module encodes, all verified against dp-service:

  - **An ack means "passed validation", not "ingested".**  The service validates a request, acks or rejects it, and
    only then queues it for asynchronous handling.  An unknown providerId is acked and then fails asynchronously, as
    do bucket-building errors and Mongo insert failures -- including a re-ingest of the same PV with the same first
    timestamp, which can leave a partial write behind an ERROR status.  The request-status document, written after
    the data, is the only confirmation: see query_request_status() and await_request_statuses().
  - **IngestionRequestStatus's zero value is SUCCESS**, so an unset status field reads as success.  Nothing here
    ever defaults a status; compare against IngestionRequestStatus members, and treat a missing document as unknown.
  - **ingestDataStream reports rejects on an error response**: if any request in the stream was rejected, the
    single response is an ExceptionalResult, and its rejectedRequestIds are the only record of which.  So
    IngestDataStreamApiResult keeps the response on error, unlike every unary result.
  - **grpcio hides exceptions raised by a request iterator**: the caller sees UNKNOWN "Exception iterating
    requests!".  The streaming methods re-raise the original exception instead (_RequestFeed).  Requests handed to
    gRPC before it may or may not have reached the server -- the cancellation races their delivery -- and any that
    did are ingested and stay so.  Request status is the only way to tell which.
"""

import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from datetime import timedelta
from enum import IntEnum

import grpc

from dp_python_lib.client.data_frame import MAX_BUDGETED_ID_CHARS, validate_data_frame
from dp_python_lib.client.result import ApiResultBase
from dp_python_lib.client.service_api_client_base import ServiceApiClientBase
from dp_python_lib.client.time_conversions import (
    NANOS_PER_SECOND,
    TimestampInput,
    from_epoch_nanos,
    to_epoch_nanos,
    to_timestamp,
)
from dp_python_lib.grpc import common_pb2, ingestion_pb2, ingestion_pb2_grpc

# The floor await_request_statuses() puts under its status query is backed off by this much, because the status
# documents' createdAt comes from the server's clock and `since` from the caller's.
REQUEST_STATUS_CLOCK_SKEW = timedelta(seconds=60)

# Status-detail texts that identify a message-size violation, matched case-insensitively within a
# RESOURCE_EXHAUSTED.  RESOURCE_EXHAUSTED alone also covers quotas and other resource limits, where "split your
# frame" would be the wrong advice.  Neither text is a stable API, so both are kept here, with their sources.
_SERVER_INBOUND_SIZE_TEXT = "grpc message exceeds maximum size"  # grpc-java server, over its inbound limit
_CLIENT_RECEIVE_SIZE_TEXT = "received message larger than max"  # grpcio client, over its receive limit

# TimeRangeCriterion.beginTime must be at least 1 s past the epoch, or the server rejects the criterion.
_MIN_STATUS_QUERY_BEGIN_NANOS = NANOS_PER_SECOND


def _resource_exhausted_details(e: grpc.RpcError) -> str | None:
    """
    Returns a RESOURCE_EXHAUSTED error's details, lowercased, or None for any other status.

    A bare grpc.RpcError (as the unit-test mocks raise) has no usable code(), so this is defensive about it.

    :param e: The caught error.
    :return: The lowercased details for RESOURCE_EXHAUSTED, else None.
    """
    try:
        if e.code() != grpc.StatusCode.RESOURCE_EXHAUSTED:
            return None
        return str(e.details() or "").lower()
    except (AttributeError, TypeError):
        return None


def _ingest_size_hint(e: grpc.RpcError) -> str:
    """
    Advice to append to an ingest call's gRPC error when the server refused the message as too large.

    :param e: The caught error.
    :return: The hint, or "" when the error is not the server's inbound message limit.
    """
    details = _resource_exhausted_details(e)
    if details is None or _SERVER_INBOUND_SIZE_TEXT not in details:
        return ""
    return (
        " (the request exceeds the server's inbound message limit, 4,096,000 bytes by default; split the frame "
        "with data_frame.split_data_frame(frame, max_bytes=...) and ingest the chunks, e.g. with "
        "ingest_data_stream())"
    )


def _status_query_size_hint(e: grpc.RpcError) -> str:
    """
    Advice to append to a request-status query's gRPC error when its response exceeded the client's receive limit.

    :param e: The caught error.
    :return: The hint, or "" when the error is not the client's receive limit.
    """
    details = _resource_exhausted_details(e)
    if details is None or _CLIENT_RECEIVE_SIZE_TEXT not in details:
        return ""
    return (
        " (the response exceeds this client's receive limit: queryRequestStatus returns every match in one message, "
        "with no paging yet, so narrow the query -- a later time_range begin, or a status criterion)"
    )


def _require_id(value: str, what: str) -> None:
    """
    Raises unless an id is non-blank and within the length split_data_frame() budgets for.

    :param value: The id to check.
    :param what: Its parameter name, for the message.
    :raises ValueError: if value is blank or longer than MAX_BUDGETED_ID_CHARS characters.
    """
    if not value or not value.strip():
        raise ValueError(f"{what} must be non-blank, got {value!r}")
    if len(value) > MAX_BUDGETED_ID_CHARS:
        raise ValueError(
            f"{what} is {len(value)} characters; at most {MAX_BUDGETED_ID_CHARS} are allowed, the length "
            f"split_data_frame() reserves room for in each request"
        )


# ----------------------------------------------------------------------
# provider registration
# ----------------------------------------------------------------------


class RegisterProviderRequestParams:
    """
    Encapsulates client parameters for call to registerProvider() API method.

    Registering an existing name is not an error: it returns that provider's id with is_new_provider False, and
    updates it in place.  The description is overwritten -- to "" if omitted -- but an empty tag list or attribute
    map does not clear the stored ones.
    """

    def __init__(
        self,
        name: str,
        description: str | None,
        tag_list: list[str] | None,
        attribute_map: dict[str, str] | None,
    ) -> None:
        """
        :param name: Data provider name.
        :param description: Data provider description.
        :param tag_list: List of tags (keywords) describing provider.
        :param attribute_map: Map of key/value attributes describing provider.
        """
        self.name = name
        self.description = description
        self.tag_list = tag_list
        self.attribute_map = attribute_map


class RegisterProviderApiResult(ApiResultBase):
    """
    Wraps the response from registerProvider(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: ingestion_pb2.RegisterProviderResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing error condition.
        :param response: The RegisterProviderResponse object returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def provider_id(self) -> str | None:
        """The provider's id, for use in every ingestion request; None on error."""
        if self.response is None or not self.response.HasField("registrationResult"):
            return None
        return self.response.registrationResult.providerId

    @property
    def is_new_provider(self) -> bool | None:
        """True if this call created the provider, False if it already existed; None on error."""
        if self.response is None or not self.response.HasField("registrationResult"):
            return None
        return self.response.registrationResult.isNewProvider


# ----------------------------------------------------------------------
# data ingestion
# ----------------------------------------------------------------------


class IngestDataRequestParams:
    """
    Encapsulates client parameters for one ingestion request: a provider, a frame, and a request id.

    The frame is a common.DataFrame from data_frame.data_frame(), data_frame_conversions.data_frame_from_pandas(),
    data_frame.split_data_frame(), or built by hand.  It is re-validated here with the same checks data_frame()
    applies, so every path fails with the same client-side messages.  Those checks are stricter than the server's
    in two places -- whitespace-only column names and duplicate timestamps are rejected -- because the library must
    be able to read back what it writes; a caller who needs either can build the request and call the stub directly.

    Validation happens at construction, so a bad request fails where it is made rather than mid-stream.
    """

    def __init__(
        self,
        provider_id: str,
        frame: common_pb2.DataFrame,
        client_request_id: str | None = None,
    ) -> None:
        """
        :param provider_id: The id registerProvider() returned.  Required, non-blank, at most MAX_BUDGETED_ID_CHARS.
        :param frame: The data to ingest.
        :param client_request_id: Identifies this request in its ack and its request-status document.  Defaults to
            a generated uuid4 string: the server does not check uniqueness, and a reused id makes status lookups
            ambiguous.  Pass your own when it should mean something; it must be non-blank and at most
            MAX_BUDGETED_ID_CHARS characters.
        :raises ValueError: if an id is blank or too long, or the frame fails validation.
        """
        _require_id(provider_id, "provider_id")
        if client_request_id is None:
            client_request_id = str(uuid.uuid4())
        else:
            _require_id(client_request_id, "client_request_id")
        validate_data_frame(frame)

        self.provider_id = provider_id
        self.frame = frame
        self.client_request_id = client_request_id


def chunked_request_params(
    provider_id: str,
    frames: Iterable[common_pb2.DataFrame],
    base_request_id: str | None = None,
) -> Iterator[IngestDataRequestParams]:
    """
    Wraps a sequence of frames -- typically split_data_frame()'s chunks -- as request params with correlated ids.

    Request n (from 0) gets client_request_id "<base>-<n>", so the chunks of one frame are recognizable in their
    acks and status documents.  Lazy, like split_data_frame(), so it can feed ingest_data_stream() without holding
    every chunk.

    :param provider_id: The id registerProvider() returned.
    :param frames: The frames to wrap.
    :param base_request_id: The id prefix.  Defaults to a generated uuid4 string.
    :return: An iterator over IngestDataRequestParams.
    :raises ValueError: during iteration, if a frame fails validation or an id would be too long.
    """
    base = base_request_id if base_request_id is not None else str(uuid.uuid4())
    for index, frame in enumerate(frames):
        yield IngestDataRequestParams(provider_id, frame, client_request_id=f"{base}-{index}")


class IngestDataApiResult(ApiResultBase):
    """
    Wraps one ingestion request's ack or reject.

    An ack (is_error False) means only that the request passed validation; see query_request_status() for whether
    it was ingested.  A reject carries the server's message.  provider_id and client_request_id identify the
    request either way.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: ingestion_pb2.IngestDataResponse | None = None,
    ) -> None:
        """
        :param is_error: True for a reject or a failed call.
        :param message: The error message, or "" on an ack.
        :param response: The IngestDataResponse, or None if the call failed or the result was built by the unary
            dispatch path for a reject (which does not keep the response).
        """
        super().__init__(is_error, message)
        self.response = response
        self.provider_id: str | None = response.providerId if response is not None else None
        self.client_request_id: str | None = response.clientRequestId if response is not None else None

    @property
    def num_rows(self) -> int | None:
        """Rows the server counted in the frame, echoed on an ack; None otherwise."""
        if self.response is None or not self.response.HasField("ackResult"):
            return None
        return self.response.ackResult.numRows

    @property
    def num_columns(self) -> int | None:
        """Columns the server counted in the frame, echoed on an ack; None otherwise."""
        if self.response is None or not self.response.HasField("ackResult"):
            return None
        return self.response.ackResult.numColumns


class IngestDataStreamApiResult(ApiResultBase):
    """
    Wraps ingestDataStream()'s single response.

    **An error result keeps its response.**  If any request in the stream was rejected, the server answers with an
    ExceptionalResult ("one or more requests were rejected") -- but every request it did not reject was still
    accepted, and rejected_request_ids is the only record of which failed.  num_requests is None in that case,
    since the server omits it.  A failed call has no response, and all three properties are None.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: ingestion_pb2.IngestDataStreamResponse | None = None,
    ) -> None:
        """
        :param is_error: True if any request was rejected, or the call failed.
        :param message: The error message, or "" on success.
        :param response: The IngestDataStreamResponse, kept on error too; None only if the call failed.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def client_request_ids(self) -> list[str] | None:
        """Every request id the server received, rejected ones included; None if the call failed."""
        return list(self.response.clientRequestIds) if self.response is not None else None

    @property
    def rejected_request_ids(self) -> list[str] | None:
        """The ids of the requests the server rejected; None if the call failed."""
        return list(self.response.rejectedRequestIds) if self.response is not None else None

    @property
    def num_requests(self) -> int | None:
        """How many requests were accepted, when none was rejected; None otherwise."""
        if self.response is None or not self.response.HasField("ingestDataStreamResult"):
            return None
        return self.response.ingestDataStreamResult.numRequests


class _RequestFeed:
    """
    The request iterator handed to grpcio for a streaming ingest, which remembers why it stopped.

    grpcio consumes a request iterator on its own thread, and if the iterator raises, it cancels the call and
    reports UNKNOWN "Exception iterating requests!" -- the original exception is neither chained nor re-raised.
    The caller's iterable (a generator over split_data_frame(), say) is exactly where a real failure happens, so
    this records the exception for the sender to re-raise in its place, with a count of the requests already handed
    to gRPC.  That count is an upper bound, not a delivery receipt: the cancellation races the sends, so some of
    those requests may never reach the server.  Any that did are ingested and are not undone.
    """

    def __init__(self, requests: Iterable[IngestDataRequestParams], build: Callable, logger: logging.Logger) -> None:
        """
        :param requests: The caller's request params, consumed lazily.
        :param build: Turns one IngestDataRequestParams into an IngestDataRequest.
        :param logger: The client's logger.
        """
        self._requests = requests
        self._build = build
        self._logger = logger
        self.sent = 0
        self.error: Exception | None = None

    def __iter__(self) -> Iterator[ingestion_pb2.IngestDataRequest]:
        try:
            for params in self._requests:
                request = self._build(params)
                self.sent += 1
                yield request
        except Exception as e:
            self.error = e
            raise

    def reraise(self, rpc_error: grpc.RpcError, op_name: str) -> None:
        """
        Re-raises the recorded exception, chained from the RpcError it caused, if there is one.

        The exception is raised as itself, type and traceback intact, so the caller can catch what their own code
        raised.  The sent count travels as an exception note where the interpreter supports them (3.11+) and is
        always logged.

        :param rpc_error: The error grpcio reported.
        :param op_name: The API method name, for the message.
        :raises Exception: the recorded exception, if any.
        """
        if self.error is None:
            return
        note = (
            f"{op_name}: raised while producing request {self.sent + 1}; {self.sent} request(s) had been handed to "
            f"gRPC, and any that reached the server are ingested and not rolled back -- check request status"
        )
        self._logger.error("%s (%s: %s)", note, type(self.error).__name__, self.error)
        if hasattr(self.error, "add_note"):
            self.error.add_note(note)
        raise self.error from rpc_error


# ----------------------------------------------------------------------
# request status
# ----------------------------------------------------------------------


class IngestionRequestStatus(IntEnum):
    """
    The outcome recorded in a request-status document, mirroring the proto enum so code compares names.

    Beware the proto's zero value is SUCCESS: an unset or defaulted status field reads as success.  Only a status
    read from an actual document means anything.
    """

    SUCCESS = ingestion_pb2.INGESTION_REQUEST_STATUS_SUCCESS
    REJECTED = ingestion_pb2.INGESTION_REQUEST_STATUS_REJECTED
    ERROR = ingestion_pb2.INGESTION_REQUEST_STATUS_ERROR


class RequestStatusQuery:
    """
    Factory of helpers building QueryRequestStatusRequest criteria for IngestionClient.query_request_status().
    Criteria are ANDed.  Each helper rejects the inputs the server would reject, naming the argument.

    Note a RequestIdCriterion holds ONE id, and two of them AND to nothing: to check several requests, query by
    provider and time range and match the ids yourself -- which is what await_request_statuses() does.

    Example:
        from dp_python_lib.client import RequestStatusQuery as RS, IngestionRequestStatus
        criteria = [RS.provider_id(pid), RS.status([IngestionRequestStatus.ERROR]), RS.time_range(since)]
    """

    _Criterion = ingestion_pb2.QueryRequestStatusRequest.QueryRequestStatusCriterion

    @staticmethod
    def provider_id(provider_id: str) -> "ingestion_pb2.QueryRequestStatusRequest.QueryRequestStatusCriterion":
        """
        Matches documents for one provider, by the id registerProvider() returned.
        :param provider_id: The provider id.
        :return: A criterion with providerIdCriterion set.
        :raises ValueError: if provider_id is blank.
        """
        if not provider_id or not provider_id.strip():
            raise ValueError("provider_id() requires a non-blank provider_id")
        criterion = RequestStatusQuery._Criterion()
        criterion.providerIdCriterion.providerId = provider_id
        return criterion

    @staticmethod
    def provider_name(provider_name: str) -> "ingestion_pb2.QueryRequestStatusRequest.QueryRequestStatusCriterion":
        """
        Matches documents for one provider, by the name it registered with.
        :param provider_name: The provider name.
        :return: A criterion with providerNameCriterion set.
        :raises ValueError: if provider_name is blank.
        """
        if not provider_name or not provider_name.strip():
            raise ValueError("provider_name() requires a non-blank provider_name")
        criterion = RequestStatusQuery._Criterion()
        criterion.providerNameCriterion.providerName = provider_name
        return criterion

    @staticmethod
    def request_id(client_request_id: str) -> "ingestion_pb2.QueryRequestStatusRequest.QueryRequestStatusCriterion":
        """
        Matches documents for one clientRequestId.  Ids are not unique server-side, so several may match.
        :param client_request_id: The request's clientRequestId.
        :return: A criterion with requestIdCriterion set.
        :raises ValueError: if client_request_id is blank.
        """
        if not client_request_id or not client_request_id.strip():
            raise ValueError("request_id() requires a non-blank client_request_id")
        criterion = RequestStatusQuery._Criterion()
        criterion.requestIdCriterion.requestId = client_request_id
        return criterion

    @staticmethod
    def status(
        statuses: Sequence[IngestionRequestStatus | int],
    ) -> "ingestion_pb2.QueryRequestStatusRequest.QueryRequestStatusCriterion":
        """
        Matches documents whose status is any of the given ones.
        :param statuses: IngestionRequestStatus members (or their int values).
        :return: A criterion with statusCriterion set.
        :raises ValueError: if statuses is empty or holds a value that is not a known status.
        """
        if not statuses:
            raise ValueError("status() requires at least one status")
        values: list[ingestion_pb2.IngestionRequestStatus.ValueType] = []
        for status in statuses:
            try:
                # ValueType is int at runtime and a NewType under typed stubs, where a bare int would not check.
                values.append(ingestion_pb2.IngestionRequestStatus.ValueType(int(IngestionRequestStatus(status))))
            except ValueError:
                raise ValueError(f"status() received an unknown status {status!r}") from None
        criterion = RequestStatusQuery._Criterion()
        criterion.statusCriterion.status[:] = values
        return criterion

    @staticmethod
    def time_range(
        begin: TimestampInput, end: TimestampInput | None = None
    ) -> "ingestion_pb2.QueryRequestStatusRequest.QueryRequestStatusCriterion":
        """
        Matches documents created in [begin, end], both ends inclusive, at millisecond resolution.

        The time is the status document's creation -- when the ingestion job FINISHED -- not when the request was
        sent or the time of its data.
        :param begin: Earliest creation time.  Must be at least 1 s after the epoch.
        :param end: Latest creation time; omit for "now" on the server's clock.
        :return: A criterion with timeRangeCriterion set.
        :raises ValueError: if begin is under 1 s after the epoch, or end is before begin.
        """
        begin_ts = to_timestamp(begin)
        if to_epoch_nanos(begin_ts) < _MIN_STATUS_QUERY_BEGIN_NANOS:
            raise ValueError("time_range() requires begin at least 1 second after the epoch")
        criterion = RequestStatusQuery._Criterion()
        criterion.timeRangeCriterion.beginTime.CopyFrom(begin_ts)
        if end is not None:
            end_ts = to_timestamp(end)
            if to_epoch_nanos(end_ts) < to_epoch_nanos(begin_ts):
                raise ValueError("time_range() requires end at or after begin")
            criterion.timeRangeCriterion.endTime.CopyFrom(end_ts)
        return criterion


class QueryRequestStatusApiResult(ApiResultBase):
    """
    Wraps queryRequestStatus(): the matching request-status documents, in document-id (roughly creation) order.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: ingestion_pb2.QueryRequestStatusResponse | None = None,
    ) -> None:
        """
        :param is_error: True if the query was rejected or the call failed.
        :param message: The error message, or "" on success.
        :param response: The QueryRequestStatusResponse, or None on error.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def request_statuses(
        self,
    ) -> "list[ingestion_pb2.QueryRequestStatusResponse.RequestStatusResult.RequestStatus] | None":
        """The matching documents; None on error."""
        if self.response is None or not self.response.HasField("requestStatusResult"):
            return None
        return list(self.response.requestStatusResult.requestStatus)


# ----------------------------------------------------------------------
# client
# ----------------------------------------------------------------------


class IngestionClient(ServiceApiClientBase):
    """
    This is the user-facing Ingestion Service API class.  It provides methods and utility classes for calling
    Ingestion Service methods.

    The typical sequence: register_provider() once for the provider id; ingest with ingest_data(),
    ingest_data_stream(), or iter_ingest_data_bidi_stream(); then confirm with await_request_statuses(), because
    an ack means only that a request passed validation.
    """

    def __init__(self, channel: grpc.Channel) -> None:
        """
        :param channel: gRPC communication channel for Ingestion Service.
        """
        super().__init__(channel, ingestion_pb2_grpc.DpIngestionServiceStub)
        self.logger = logging.getLogger(__name__)
        self.logger.debug("IngestionClient initialized with channel: %s", channel)

    # ---- registerProvider

    def _build_register_provider_request(
        self, request_params: RegisterProviderRequestParams
    ) -> ingestion_pb2.RegisterProviderRequest:
        """
        Builds a RegisterProviderRequest API object from the supplied RegisterProviderRequestParams object.
        :param request_params: A RegisterProviderRequestParams object containing the user parameters for
            call to registerProvider() API method.
        :return: Returns a RegisterProviderRequest API object for the specified params.
        """
        self.logger.debug("Building RegisterProviderRequest for provider: %s", request_params.name)

        request = ingestion_pb2.RegisterProviderRequest()
        request.providerName = request_params.name

        if request_params.description:
            self.logger.debug("Adding description: %s", request_params.description)
            request.description = request_params.description

        if request_params.tag_list:
            self.logger.debug("Adding %d tags: %s", len(request_params.tag_list), request_params.tag_list)
            request.tags[:] = request_params.tag_list

        if request_params.attribute_map:
            self.logger.debug(
                "Adding %d attributes: %s",
                len(request_params.attribute_map),
                list(request_params.attribute_map.keys()),
            )
            for name, value in request_params.attribute_map.items():
                attribute = common_pb2.Attribute()
                attribute.name = name
                attribute.value = value
                request.attributes.append(attribute)

        self.logger.debug("RegisterProviderRequest built successfully")
        return request

    def _send_register_provider(self, request: ingestion_pb2.RegisterProviderRequest) -> RegisterProviderApiResult:
        """
        Invokes the registerProvider() API method with the supplied request object.
        :param request: RegisterProviderRequest object with parameters for call to registerProvider().
        :return: Returns a RegisterProviderApiResult with the method response and status information.
        """
        return self._dispatch(
            self._stub.registerProvider,
            request,
            RegisterProviderApiResult,
            "registrationResult",
            "registerProvider",
            request_log=lambda: self.logger.info("Calling registerProvider API for provider: %s", request.providerName),
            success_log=lambda _response: self.logger.info(
                "Successfully registered provider: %s", request.providerName
            ),
        )

    def register_provider(self, request_params: RegisterProviderRequestParams) -> RegisterProviderApiResult:
        """
        User facing method for invoking the registerProvider() API method.
        :param request_params: Contains user parameters for call to registerProvider() API method.
        :return: Returns RegisterProviderApiResult with the method response and status information; its
            provider_id is what every ingestion request needs.
        """
        self.logger.info("Starting registerProvider operation for provider: %s", request_params.name)

        request = self._build_register_provider_request(request_params)
        result = self._send_register_provider(request)

        if result.result_status.is_error:
            self.logger.error("RegisterProvider operation failed: %s", result.result_status.message)
        else:
            self.logger.info(
                "RegisterProvider operation completed successfully for provider: %s",
                request_params.name,
            )

        return result

    # ---- ingestData

    def _build_ingest_data_request(self, request_params: IngestDataRequestParams) -> ingestion_pb2.IngestDataRequest:
        """
        Builds an IngestDataRequest from validated params.
        :param request_params: The request's params.
        :return: The IngestDataRequest.
        """
        request = ingestion_pb2.IngestDataRequest()
        request.providerId = request_params.provider_id
        request.clientRequestId = request_params.client_request_id
        request.ingestionDataFrame.CopyFrom(request_params.frame)
        return request

    def _send_ingest_data(self, request: ingestion_pb2.IngestDataRequest) -> IngestDataApiResult:
        """
        Invokes the unary ingestData() API method.
        :param request: The IngestDataRequest.
        :return: An IngestDataApiResult; the ids are filled in by ingest_data() when the response is not kept.
        """
        return self._dispatch(
            self._stub.ingestData,
            request,
            IngestDataApiResult,
            "ackResult",
            "ingestData",
            request_log=lambda: self.logger.info(
                "Calling ingestData API for request %s (provider %s)", request.clientRequestId, request.providerId
            ),
            success_log=lambda response: self.logger.info(
                "ingestData acked request %s: %d rows x %d columns",
                response.clientRequestId,
                response.ackResult.numRows,
                response.ackResult.numColumns,
            ),
            rpc_error_hint=_ingest_size_hint,
        )

    def ingest_data(self, request_params: IngestDataRequestParams) -> IngestDataApiResult:
        """
        Sends one ingestion request and returns its ack or reject.

        An ack means the request passed validation and was queued -- NOT that it was ingested.  Use
        await_request_statuses() (or query_request_status()) to learn that.

        A request over the server's inbound message limit (4,096,000 bytes by default) fails as a gRPC error; the
        message then says so and points at split_data_frame().

        :param request_params: The request (see IngestDataRequestParams).
        :return: An IngestDataApiResult whose provider_id and client_request_id identify the request.
        """
        request = self._build_ingest_data_request(request_params)
        result = self._send_ingest_data(request)
        # _dispatch keeps no response on an error, so name the request from what was sent.
        result.provider_id = request_params.provider_id
        result.client_request_id = request_params.client_request_id
        if result.result_status.is_error:
            self.logger.error(
                "ingestData request %s failed: %s", request_params.client_request_id, result.result_status.message
            )
        return result

    # ---- ingestDataStream

    def _send_ingest_data_stream(self, feed: _RequestFeed) -> IngestDataStreamApiResult:
        """
        Invokes the client-streaming ingestDataStream() API method.

        Hand-written rather than _dispatch, because an error result must KEEP its response: rejectedRequestIds
        arrives on the ExceptionalResult response and is the only record of which requests failed.  And the feed's
        own exception, if any, is re-raised in place of grpcio's generic error.

        :param feed: The request iterator.
        :return: An IngestDataStreamApiResult.
        :raises Exception: whatever the caller's request iterable raised.
        """
        self.logger.info("Calling ingestDataStream API")
        try:
            response = self._stub.ingestDataStream(iter(feed))
        except grpc.RpcError as e:
            feed.reraise(e, "ingestDataStream")
            error_msg = f"gRPC error: {e.details()}{_ingest_size_hint(e)}"
            self.logger.error("gRPC error during ingestDataStream after %d request(s): %s", feed.sent, e.details())
            return IngestDataStreamApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {e!s}"
            self.logger.exception("Unexpected error during ingestDataStream: %s", str(e))
            return IngestDataStreamApiResult(is_error=True, message=error_msg)

        if response.HasField("exceptionalResult"):
            error_msg = response.exceptionalResult.message
            self.logger.warning(
                "ingestDataStream rejected %d of %d request(s): %s",
                len(response.rejectedRequestIds),
                len(response.clientRequestIds),
                error_msg,
            )
            return IngestDataStreamApiResult(is_error=True, message=error_msg, response=response)
        if response.HasField("ingestDataStreamResult"):
            self.logger.info("ingestDataStream accepted %d request(s)", response.ingestDataStreamResult.numRequests)
            return IngestDataStreamApiResult(is_error=False, message="", response=response)
        error_msg = "Unexpected response format: neither exceptionalResult nor ingestDataStreamResult found"
        self.logger.error(error_msg)
        return IngestDataStreamApiResult(is_error=True, message=error_msg, response=response)

    def ingest_data_stream(self, requests: Iterable[IngestDataRequestParams]) -> IngestDataStreamApiResult:
        """
        Sends many ingestion requests on one client-streaming call and returns the server's single summary.

        The iterable is consumed lazily, so a generator over split_data_frame() (see chunked_request_params())
        never holds every request at once.  The server validates each request as it arrives; a reject does not end
        the stream, and every valid request is queued for ingestion.

        - All accepted: is_error False, num_requests set.
        - Some rejected: is_error True, rejected_request_ids naming them.  This does NOT raise, because the other
          requests were accepted and raising would hide which.
        - If the iterable itself raises, that exception is re-raised as itself (grpcio would otherwise report only
          "Exception iterating requests!").  Requests sent before it may or may not have reached the server; any
          that did stay ingested, so check their status.

        As with ingest_data(), acceptance is not ingestion; confirm with await_request_statuses().

        :param requests: The requests to send.
        :return: An IngestDataStreamApiResult.
        :raises Exception: whatever the iterable raised.
        """
        self.logger.info("Starting ingestDataStream operation")
        feed = _RequestFeed(requests, self._build_ingest_data_request, self.logger)
        return self._send_ingest_data_stream(feed)

    # ---- ingestDataBidiStream

    def _send_ingest_data_bidi_stream(self, feed: _RequestFeed) -> Generator[IngestDataApiResult, None, None]:
        """
        Invokes the bidirectional ingestDataBidiStream() API method, yielding one result per response.

        A per-request reject is yielded with its response, which names the request.  A transport or unexpected
        error is yielded WITHOUT a response and ends the generator; that absence is how the public wrapper tells
        the two apart.  The feed's own exception, if any, is re-raised instead.

        :param feed: The request iterator.
        :return: An iterator over results.
        :raises Exception: whatever the caller's request iterable raised.
        """
        self.logger.info("Calling ingestDataBidiStream API")
        call = None
        try:
            call = self._stub.ingestDataBidiStream(iter(feed))
            for response in call:
                if response.HasField("exceptionalResult"):
                    error_msg = response.exceptionalResult.message
                    self.logger.warning(
                        "ingestDataBidiStream rejected request %s: %s", response.clientRequestId, error_msg
                    )
                    yield IngestDataApiResult(is_error=True, message=error_msg, response=response)
                elif response.HasField("ackResult"):
                    yield IngestDataApiResult(is_error=False, message="", response=response)
                else:
                    error_msg = "Unexpected response format: neither exceptionalResult nor ackResult found"
                    self.logger.error("%s (request %s)", error_msg, response.clientRequestId)
                    yield IngestDataApiResult(is_error=True, message=error_msg, response=response)
        except grpc.RpcError as e:
            feed.reraise(e, "ingestDataBidiStream")
            error_msg = f"gRPC error: {e.details()}{_ingest_size_hint(e)}"
            self.logger.error("gRPC error during ingestDataBidiStream after %d request(s): %s", feed.sent, e.details())
            yield IngestDataApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {e!s}"
            self.logger.exception("Unexpected error during ingestDataBidiStream: %s", str(e))
            yield IngestDataApiResult(is_error=True, message=error_msg)
        finally:
            # A caller that stops reading early (break, or an exception in its loop) closes this generator, but
            # grpcio would keep pulling and SENDING requests on its own thread -- ingesting data the caller believes
            # it abandoned.  grpcio's call object does cancel itself when garbage-collected, but that ties the
            # behavior to collection timing; cancel explicitly.  On a call that already finished it does nothing.
            if call is not None and hasattr(call, "cancel"):
                call.cancel()

    def iter_ingest_data_bidi_stream(
        self, requests: Iterable[IngestDataRequestParams]
    ) -> Iterator[IngestDataApiResult]:
        """
        Sends ingestion requests on a bidirectional stream, yielding each one's ack or reject as it arrives.

        One result per request, in order.  **A reject is yielded as an is_error result, not raised**: it is a fact
        about one request, and the server keeps processing the rest.  This deliberately differs from
        iter_query_samples_stream(), where an error ends the stream.  A transport error does end it, as a
        RuntimeError.  If the request iterable itself raises, that exception is re-raised as itself; as with
        ingest_data_stream(), requests sent before it may have been ingested.

        grpcio consumes the iterable on its own thread, so a producer generator runs concurrently with the
        responses being read.  The server applies backpressure through HTTP/2 flow control.  Stopping early (a
        break, or closing this iterator) cancels the call, so no further requests are sent; requests already sent
        may still be ingested.

        :param requests: The requests to send.
        :return: A lazy iterator over per-request results.
        :raises RuntimeError: on a transport or unexpected error.
        :raises Exception: whatever the iterable raised.
        """
        self.logger.info("Starting ingestDataBidiStream operation")
        feed = _RequestFeed(requests, self._build_ingest_data_request, self.logger)
        # closing() so that abandoning THIS generator closes the sender's at once, running the finally that cancels
        # the call, instead of whenever the garbage collector gets to it.
        with contextlib.closing(self._send_ingest_data_bidi_stream(feed)) as results:
            for result in results:
                if result.result_status.is_error and result.response is None:
                    raise RuntimeError(f"ingestDataBidiStream failed: {result.result_status.message}")
                yield result

    # ---- queryRequestStatus

    def _send_query_request_status(
        self, request: ingestion_pb2.QueryRequestStatusRequest
    ) -> QueryRequestStatusApiResult:
        """
        Invokes the queryRequestStatus() API method.
        :param request: The QueryRequestStatusRequest.
        :return: A QueryRequestStatusApiResult.
        """
        return self._dispatch(
            self._stub.queryRequestStatus,
            request,
            QueryRequestStatusApiResult,
            "requestStatusResult",
            "queryRequestStatus",
            request_log=lambda: self.logger.info(
                "Calling queryRequestStatus API with %d criteria", len(request.criteria)
            ),
            success_log=lambda response: self.logger.info(
                "queryRequestStatus returned %d documents", len(response.requestStatusResult.requestStatus)
            ),
            rpc_error_hint=_status_query_size_hint,
        )

    def query_request_status(
        self,
        criteria: "Sequence[ingestion_pb2.QueryRequestStatusRequest.QueryRequestStatusCriterion]",
    ) -> QueryRequestStatusApiResult:
        """
        Queries request-status documents: the only record of whether an acked request was actually ingested.

        Each handled request gets one document, written after its data (so SUCCESS means persisted and queryable),
        or recording REJECTED / ERROR.  A few paths write none -- a reject while the service is shutting down, and
        a handler that fails outright -- so absence is "unknown", never success.

        The server returns every match in one message, with no paging yet, so keep queries narrow: a provider plus
        a time range, not a bare status.

        :param criteria: At least one criterion (see RequestStatusQuery); they are ANDed.
        :return: A QueryRequestStatusApiResult.
        :raises ValueError: if criteria is empty (the server rejects it).
        """
        if not criteria:
            raise ValueError("query_request_status() requires at least one criterion")
        request = ingestion_pb2.QueryRequestStatusRequest()
        request.criteria.extend(criteria)
        return self._send_query_request_status(request)

    def await_request_statuses(
        self,
        provider_id: str,
        client_request_ids: Iterable[str],
        *,
        since: TimestampInput,
        timeout: float = 30.0,
        poll_interval: float = 0.25,
    ) -> "dict[str, list[ingestion_pb2.QueryRequestStatusResponse.RequestStatusResult.RequestStatus]]":
        """
        Polls until every given request has a status document, and returns them.

        This does not judge success: check each document's ingestionRequestStatus against IngestionRequestStatus.
        A request can be acked and still end in ERROR (an unknown providerId, a duplicate PV + first timestamp).

        Each poll is ONE query -- this provider, documents created since `since` -- with the ids matched here, since
        the API cannot ask about several request ids at once.  `since` is required, and should be captured before
        the first request is sent:

            since = datetime.now(timezone.utc)
            result = ingestion.ingest_data(params)
            statuses = ingestion.await_request_statuses(provider_id, [result.client_request_id], since=since)

        It bounds the query (the method has no paging, and a provider's whole history can exceed the receive limit),
        and it keeps out documents from earlier runs that reused an id.  The floor is backed off by
        REQUEST_STATUS_CLOCK_SKEW for the server's clock, so a reused id can still match a document from within that
        margin; the default generated ids avoid that.

        :param provider_id: The provider the requests were sent as.
        :param client_request_ids: The requests to wait for.
        :param since: A time at or before the first request was sent.
        :param timeout: Seconds to wait before giving up.
        :param poll_interval: Seconds between polls.
        :return: {client_request_id: [documents]}, one entry per distinct id; a list because ids are not unique.
        :raises ValueError: on blank or empty arguments, or a non-positive timeout or poll_interval.
        :raises TimeoutError: if some ids have no document by the timeout; the message names them.
        :raises RuntimeError: if a status query fails.
        """
        if not provider_id or not provider_id.strip():
            raise ValueError("await_request_statuses() requires a non-blank provider_id")
        wanted = list(dict.fromkeys(client_request_ids))
        if not wanted:
            raise ValueError("await_request_statuses() requires at least one client_request_id")
        for request_id in wanted:
            if not request_id or not request_id.strip():
                raise ValueError(f"await_request_statuses() received a blank client_request_id: {request_id!r}")
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("await_request_statuses() requires a positive timeout and poll_interval")

        # Floor division of two timedeltas is exact integer arithmetic.
        skew_nanos = (REQUEST_STATUS_CLOCK_SKEW // timedelta(microseconds=1)) * 1_000
        floor_nanos = max(to_epoch_nanos(to_timestamp(since)) - skew_nanos, _MIN_STATUS_QUERY_BEGIN_NANOS)
        criteria = [
            RequestStatusQuery.provider_id(provider_id),
            RequestStatusQuery.time_range(from_epoch_nanos(floor_nanos)),
        ]

        wanted_set = set(wanted)
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            attempt += 1
            result = self.query_request_status(criteria)
            if result.result_status.is_error:
                raise RuntimeError(f"await_request_statuses() status query failed: {result.result_status.message}")
            found: dict[str, list] = {}
            for document in result.request_statuses or []:
                if document.requestId in wanted_set:
                    found.setdefault(document.requestId, []).append(document)
            missing = [request_id for request_id in wanted if request_id not in found]
            if not missing:
                self.logger.info(
                    "await_request_statuses: all %d request(s) have a status (poll %d)", len(wanted), attempt
                )
                return {request_id: found[request_id] for request_id in wanted}

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                shown = ", ".join(missing[:10]) + (f", ... ({len(missing) - 10} more)" if len(missing) > 10 else "")
                raise TimeoutError(
                    f"await_request_statuses() timed out after {timeout} s with {len(missing)} of {len(wanted)} "
                    f"request(s) still lacking a status document: {shown}"
                )
            time.sleep(min(poll_interval, remaining))
