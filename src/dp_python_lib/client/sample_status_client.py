import logging
from collections.abc import Iterator

import grpc

from dp_python_lib.client.machine_config_client import TimestampInput, to_timestamp
from dp_python_lib.client.result import ApiResultBase
from dp_python_lib.client.service_api_client_base import ServiceApiClientBase
from dp_python_lib.grpc import annotation_pb2, annotation_pb2_grpc, common_pb2


def sampling_clock(
    start_time: TimestampInput,
    period_nanos: int,
    count: int,
) -> common_pb2.DataTimestamps:
    """
    Builds a DataTimestamps with a SamplingClock time axis, the compact form for dense labeling of regularly-sampled
    data.  The clock must match the archived data's clock exactly -- status-to-sample matching is by exact timestamp
    at nanosecond precision, so an off-by-one-nanosecond period misses every sample after the first.

    :param start_time: Time of the first sample (tz-aware datetime, epoch seconds, or common.Timestamp).
    :param period_nanos: Period between samples, in nanoseconds.  Must be > 0.
    :param count: Number of samples in the interval.  Must be >= 1.
    :return: A DataTimestamps carrying a SamplingClock.
    :raises ValueError: if period_nanos is not positive or count is less than 1.
    """
    if period_nanos <= 0:
        raise ValueError(f"sampling_clock() requires period_nanos > 0, got {period_nanos}")
    if count < 1:
        raise ValueError(f"sampling_clock() requires count >= 1, got {count}")

    timestamps = common_pb2.DataTimestamps()
    timestamps.samplingClock.startTime.CopyFrom(to_timestamp(start_time))
    timestamps.samplingClock.periodNanos = period_nanos
    timestamps.samplingClock.count = count
    return timestamps


def timestamp_list(values: list[TimestampInput]) -> common_pb2.DataTimestamps:
    """
    Builds a DataTimestamps with an explicit TimestampList time axis, the form for sparse labeling -- naming only the
    samples being labeled.  The unlabeled samples carry no assertion; there is no need to mark the rest "good".

    Timestamps must be strictly increasing, which is validated here rather than deferred to the server.  Supply
    timestamps taken from data query results (or exact SamplingClock arithmetic); a recomputed or rounded timestamp
    will silently fail to match its sample.

    :param values: The timestamps to label (tz-aware datetimes, epoch seconds, or common.Timestamp objects).
    :return: A DataTimestamps carrying a TimestampList.
    :raises ValueError: if values is empty or the timestamps are not strictly increasing.
    """
    if not values:
        raise ValueError("timestamp_list() requires a non-empty values list")

    converted = [to_timestamp(value) for value in values]

    previous = converted[0]
    for index, current in enumerate(converted[1:], start=1):
        if (current.epochSeconds, current.nanoseconds) <= (previous.epochSeconds, previous.nanoseconds):
            raise ValueError(
                f"timestamp_list() requires strictly increasing timestamps; entry {index} "
                f"({current.epochSeconds}.{current.nanoseconds:09d}) does not follow entry {index - 1} "
                f"({previous.epochSeconds}.{previous.nanoseconds:09d})"
            )
        previous = current

    timestamps = common_pb2.DataTimestamps()
    timestamps.timestampList.timestamps.extend(converted)
    return timestamps


def _timestamp_count(timestamps: common_pb2.DataTimestamps) -> int:
    """
    Returns the number of timestamps a DataTimestamps describes, for validating parallel-array lengths.
    :param timestamps: The time axis to measure.
    :return: The number of timestamps on the axis.
    :raises ValueError: if neither axis form is set.
    """
    axis = timestamps.WhichOneof("value")
    if axis == "samplingClock":
        return timestamps.samplingClock.count
    if axis == "timestampList":
        return len(timestamps.timestampList.timestamps)
    raise ValueError("DataTimestamps must specify either a samplingClock or a timestampList")


class SampleStatusColumn:
    """
    Status codes for a single PV, aligned one-per-timestamp with the enclosing frame's time axis.

    Status codes are int32 values whose meaning is defined by the enclosing frame's domain, in the style of
    EnumColumn: the (domain, code) mapping is a contract between status producers and consumers and is neither
    validated nor interpreted by the MLDP.

    confidence and reasons are optional parallel arrays.  Each must be either omitted entirely or supply exactly one
    entry per timestamp; the length rules are enforced when the frame is built.  An all-empty reasons list is dropped
    rather than sent as empty strings, per the API's guidance.
    """

    def __init__(
        self,
        pv_name: str,
        status_codes: list[int],
        confidence: list[float] | None = None,
        reasons: list[str] | None = None,
    ) -> None:
        """
        :param pv_name: Name of the PV these statuses apply to.
        :param status_codes: One int32 status code per timestamp on the frame's time axis.
        :param confidence: Optional confidence value per timestamp; omit, or supply one entry per timestamp.
        :param reasons: Optional reason string per timestamp; omit, or supply one entry per timestamp.  An empty
            string means "no reason for this sample".
        :raises ValueError: if pv_name is empty or status_codes is empty.
        """
        if not pv_name:
            raise ValueError("SampleStatusColumn requires a non-empty pv_name")
        if not status_codes:
            raise ValueError(f"SampleStatusColumn for PV '{pv_name}' requires a non-empty status_codes list")

        self.pv_name = pv_name
        self.status_codes = status_codes
        self.confidence = confidence
        self.reasons = reasons

    def _validate(self, n_timestamps: int) -> None:
        """
        Validates this column's parallel arrays against the enclosing frame's timestamp count.  Checked client-side so
        a length mistake surfaces as a Python ValueError naming the offending PV, rather than as a server rejection of
        the whole batch.
        :param n_timestamps: The number of timestamps on the enclosing frame's time axis.
        :raises ValueError: if any populated array does not have exactly one entry per timestamp.
        """
        if len(self.status_codes) != n_timestamps:
            raise ValueError(
                f"SampleStatusColumn for PV '{self.pv_name}' has {len(self.status_codes)} status_codes "
                f"but the frame's time axis has {n_timestamps} timestamps; status_codes must supply "
                f"exactly one entry per timestamp"
            )

        if self.confidence and len(self.confidence) != n_timestamps:
            raise ValueError(
                f"SampleStatusColumn for PV '{self.pv_name}' has {len(self.confidence)} confidence values "
                f"but the frame's time axis has {n_timestamps} timestamps; confidence must be omitted or "
                f"supply exactly one entry per timestamp"
            )

        if self.reasons and len(self.reasons) != n_timestamps:
            raise ValueError(
                f"SampleStatusColumn for PV '{self.pv_name}' has {len(self.reasons)} reasons "
                f"but the frame's time axis has {n_timestamps} timestamps; reasons must be omitted or "
                f"supply exactly one entry per timestamp"
            )

    def to_proto(self) -> common_pb2.SampleStatusColumn:
        """
        Converts this column into its protobuf form.  An all-empty reasons list is omitted entirely rather than sent
        as empty strings, as the API directs.
        :return: The equivalent common.SampleStatusColumn.
        """
        column = common_pb2.SampleStatusColumn()
        column.pvName = self.pv_name
        column.statusCodes[:] = self.status_codes

        if self.confidence:
            column.confidence[:] = self.confidence

        if self.reasons and any(reason for reason in self.reasons):
            column.reasons[:] = self.reasons

        return column


class SampleStatusFrame:
    """
    The unit of sample status ingestion: statuses in a single (domain, layer) for one or more PVs sharing one time
    axis.  Parallels DataFrame for time-series data.

    Build the time axis with sampling_clock() for dense labeling of regularly-sampled data, or timestamp_list() for
    sparse labeling of individually chosen samples.

    Together with PV name and timestamp, domain and layer form the identity key of an individual sample status:
    (pvName, timestamp, domain, layer).  Saving the same key again replaces that status and no other.
    """

    def __init__(
        self,
        domain: str,
        layer: str,
        data_timestamps: common_pb2.DataTimestamps,
        columns: list[SampleStatusColumn],
    ) -> None:
        """
        :param domain: Names the status-code semantics contract (e.g. "data_quality", "ml_anomaly").
        :param layer: Names the producer stream assigning the statuses (e.g. "ml_model_v1", "operator_override").
        :param data_timestamps: The frame's time axis (see sampling_clock() / timestamp_list()).
        :param columns: One SampleStatusColumn per PV; a PV may appear at most once.
        :raises ValueError: if domain or layer is empty, if columns is empty, if a PV appears in more than one
            column, or if any column's parallel arrays do not match the timestamp count.
        """
        if not domain:
            raise ValueError("SampleStatusFrame requires a non-empty domain")
        if not layer:
            raise ValueError("SampleStatusFrame requires a non-empty layer")
        if not columns:
            raise ValueError("SampleStatusFrame requires at least one column")

        n_timestamps = _timestamp_count(data_timestamps)

        seen: set[str] = set()
        for column in columns:
            if column.pv_name in seen:
                raise ValueError(
                    f"SampleStatusFrame has more than one column for PV '{column.pv_name}'; "
                    f"a PV may appear in at most one column per frame"
                )
            seen.add(column.pv_name)
            column._validate(n_timestamps)

        self.domain = domain
        self.layer = layer
        self.data_timestamps = data_timestamps
        self.columns = columns

    def to_proto(self) -> common_pb2.SampleStatusFrame:
        """
        Converts this frame into its protobuf form.
        :return: The equivalent common.SampleStatusFrame.
        """
        frame = common_pb2.SampleStatusFrame()
        frame.domain = self.domain
        frame.layer = self.layer
        frame.dataTimestamps.CopyFrom(self.data_timestamps)
        frame.statusColumns.extend(column.to_proto() for column in self.columns)
        return frame


class SaveSampleStatusesRequestParams:
    """
    Encapsulates client parameters for a call to the saveSampleStatuses() API method.

    Saving is a per-status upsert keyed by (pvName, timestamp, domain, layer), and it replaces the matched status in
    full: re-saving a key with confidence or reasons omitted clears any previously stored values for that key.  Supply
    the complete desired state each time.

    source and modifiedBy apply to every frame in the request, so batch frames from a single producer per request --
    mixing producers records the same provenance for all of them.
    """

    def __init__(
        self,
        frames: list[SampleStatusFrame],
        source: str | None = None,
        modified_by: str | None = None,
    ) -> None:
        """
        :param frames: One or more SampleStatusFrame objects to save.
        :param source: Optional free-form provenance describing the producer, applied to all frames.
        :param modified_by: Optional actor / user / service identity, applied to all frames.
        :raises ValueError: if frames is empty.
        """
        if not frames:
            raise ValueError("SaveSampleStatusesRequestParams requires at least one frame")

        self.frames = frames
        self.source = source
        self.modified_by = modified_by


class QuerySampleStatusesRequestParams:
    """
    Encapsulates client parameters for a sample status query, shared by the unary and streaming query methods.

    The three filters are combined with logical AND; multiple values within one filter are combined with logical OR
    (exact match).  All three are optional -- an empty filter matches all values.  In particular an omitted pv_names
    matches every PV with statuses in the range, which is how you discover what a (domain, layer) has labeled.

    Note there is deliberately no criterion-builder class here (unlike PvMetadataQuery or ConfigurationQuery): the
    underlying request takes plain repeated string filters rather than a criterion oneof, so plain lists are the
    honest representation.
    """

    def __init__(
        self,
        begin_time: TimestampInput,
        end_time: TimestampInput,
        pv_names: list[str] | None = None,
        domains: list[str] | None = None,
        layers: list[str] | None = None,
        limit: int | None = None,
    ) -> None:
        """
        :param begin_time: Inclusive start of the query range (tz-aware datetime, epoch seconds, or common.Timestamp).
        :param end_time: Exclusive end of the query range.  The range is half-open [begin_time, end_time).
        :param pv_names: Optional PV name filter; omit to match all PVs with statuses in the range.
        :param domains: Optional domain filter; omit to match all domains.
        :param layers: Optional layer filter; omit to match all layers.
        :param limit: Maximum number of buckets per page (unary) or per message (streaming).  0 is meaningful and
            means "let the server pick a default"; a negative value raises.
        :raises ValueError: if begin_time is not strictly before end_time, or if limit is negative.
        """
        begin_ts = to_timestamp(begin_time)
        end_ts = to_timestamp(end_time)
        if (begin_ts.epochSeconds, begin_ts.nanoseconds) >= (end_ts.epochSeconds, end_ts.nanoseconds):
            raise ValueError(
                "QuerySampleStatusesRequestParams requires begin_time strictly before end_time (half-open [begin, end))"
            )

        # limit=0 is explicitly meaningful ("server selects a default"), so only reject negatives -- and reject them
        # here rather than letting a negative surface as a raw protobuf uint32 range error during request building.
        if limit is not None and limit < 0:
            raise ValueError(f"QuerySampleStatusesRequestParams limit must be non-negative, got {limit}")

        self.begin_time = begin_time
        self.end_time = end_time
        self._begin_ts = begin_ts
        self._end_ts = end_ts
        self.pv_names = pv_names
        self.domains = domains
        self.layers = layers
        self.limit = limit

    @property
    def begin_timestamp(self) -> common_pb2.Timestamp:
        """The validated common.Timestamp for begin_time, converted once at construction."""
        return self._begin_ts

    @property
    def end_timestamp(self) -> common_pb2.Timestamp:
        """The validated common.Timestamp for end_time, converted once at construction."""
        return self._end_ts


class SaveSampleStatusesApiResult(ApiResultBase):
    """
    Wraps the response from saveSampleStatuses(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.SaveSampleStatusesResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The SaveSampleStatusesResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def saved_count(self) -> int | None:
        """Total individual sample statuses upserted across all frames and columns, or None on error."""
        if self.response is not None and self.response.HasField("saveSampleStatusesResult"):
            return self.response.saveSampleStatusesResult.savedCount
        return None


class QuerySampleStatusesApiResult(ApiResultBase):
    """
    Wraps a single page (unary) or a single streamed message (streaming) of a querySampleStatuses() /
    querySampleStatusesStream() response, with a status object including an error flag and message.

    Use SampleStatusClient.iter_sample_statuses() to page through all results transparently.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.QuerySampleStatusesResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The QuerySampleStatusesResponse for this page/message, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def sample_status_buckets(self) -> list[common_pb2.SampleStatusBucket]:
        """The SampleStatusBucket objects in this page/message, or an empty list on error."""
        if self.response is not None and self.response.HasField("querySampleStatusesResult"):
            return list(self.response.querySampleStatusesResult.sampleStatusBuckets)
        return []

    @property
    def next_page_token(self) -> str:
        """
        Token for retrieving the next page (unary querySampleStatuses() only), or empty string if there are no more
        pages.  Always empty for streamed messages (querySampleStatusesStream() is fire-and-consume).
        """
        if self.response is not None and self.response.HasField("querySampleStatusesResult"):
            return self.response.querySampleStatusesResult.nextPageToken
        return ""


class DeleteSampleStatusesApiResult(ApiResultBase):
    """
    Wraps the response from deleteSampleStatuses(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.DeleteSampleStatusesResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The DeleteSampleStatusesResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def deleted_count(self) -> int | None:
        """
        Total individual sample statuses deleted, or None on error.  A delete matching nothing is a success with
        deleted_count == 0, not an error.
        """
        if self.response is not None and self.response.HasField("deleteSampleStatusesResult"):
            return self.response.deleteSampleStatusesResult.deletedCount
        return None


class SampleStatusClient(ServiceApiClientBase):
    """
    User-facing client for the sample status methods of the MLDP Annotation Service.  A sample status assigns an int32
    status code to one PV sample at one timestamp; the identity key is (pvName, timestamp, domain, layer).

    Provides wrappers for saveSampleStatuses(), querySampleStatuses(), querySampleStatusesStream(), and
    deleteSampleStatuses(), plus an iter_sample_statuses() paging iterator.

    Two properties of the model drive its use:
      - Absence means "no assertion".  There is no implicit default status; labeling three samples says nothing
        about the rest.
      - Matching is by exact timestamp at nanosecond precision.  Label using timestamps taken from data query
        results (or exact SamplingClock arithmetic); a recomputed or rounded timestamp silently fails to match.

    The deferred domain-registry methods (saveSampleStatusDomain / querySampleStatusDomains) are not wrapped; they
    are reserved placeholders that return a "not implemented" error.
    """

    def __init__(self, channel: grpc.Channel) -> None:
        """
        :param channel: gRPC communication channel for the Annotation Service.
        """
        super().__init__(channel, annotation_pb2_grpc.DpAnnotationServiceStub)
        self.logger = logging.getLogger(__name__)
        self.logger.debug("SampleStatusClient initialized with channel: %s", channel)

    # ------------------------------------------------------------------
    # saveSampleStatuses
    # ------------------------------------------------------------------

    def _build_save_sample_statuses_request(
        self, request_params: SaveSampleStatusesRequestParams
    ) -> annotation_pb2.SaveSampleStatusesRequest:
        """
        Builds a SaveSampleStatusesRequest from the supplied SaveSampleStatusesRequestParams.
        :param request_params: User parameters for the call to saveSampleStatuses().
        :return: A SaveSampleStatusesRequest for the specified params.
        """
        self.logger.debug("Building SaveSampleStatusesRequest with %d frame(s)", len(request_params.frames))

        request = annotation_pb2.SaveSampleStatusesRequest()
        request.frames.extend(frame.to_proto() for frame in request_params.frames)

        if request_params.source:
            request.source = request_params.source

        if request_params.modified_by:
            request.modifiedBy = request_params.modified_by

        self.logger.debug("SaveSampleStatusesRequest built successfully")
        return request

    def _send_save_sample_statuses(
        self, request: annotation_pb2.SaveSampleStatusesRequest
    ) -> SaveSampleStatusesApiResult:
        """
        Invokes the saveSampleStatuses() API method with the supplied request.
        :param request: SaveSampleStatusesRequest with parameters for the call.
        :return: A SaveSampleStatusesApiResult with the method response and status information.
        """
        self.logger.info("Calling saveSampleStatuses API with %d frame(s)", len(request.frames))

        try:
            self.logger.debug("Invoking stub.saveSampleStatuses with request")
            response = self._stub.saveSampleStatuses(request)
            self.logger.debug("Received response from saveSampleStatuses API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning("SaveSampleStatuses API returned business error: %s", error_msg)
                return SaveSampleStatusesApiResult(is_error=True, message=error_msg)

            elif response.HasField("saveSampleStatusesResult"):
                self.logger.info(
                    "Successfully saved %d sample status(es)", response.saveSampleStatusesResult.savedCount
                )
                return SaveSampleStatusesApiResult(is_error=False, message="", response=response)

            else:
                error_msg = "Unexpected response format: neither exceptionalResult nor saveSampleStatusesResult found"
                self.logger.error(error_msg)
                return SaveSampleStatusesApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during saveSampleStatuses: %s", e.details())
            return SaveSampleStatusesApiResult(is_error=True, message=error_msg)

        except Exception as e:
            error_msg = f"Unexpected error: {e!s}"
            self.logger.exception("Unexpected error during saveSampleStatuses: %s", str(e))
            return SaveSampleStatusesApiResult(is_error=True, message=error_msg)

    def save_sample_statuses(self, request_params: SaveSampleStatusesRequestParams) -> SaveSampleStatusesApiResult:
        """
        User-facing method for invoking the saveSampleStatuses() API method.  Saving is a per-status upsert keyed by
        (pvName, timestamp, domain, layer), replacing the matched status in full.
        :param request_params: User parameters for the call (see SaveSampleStatusesRequestParams).
        :return: A SaveSampleStatusesApiResult with the method response and status information.
        """
        self.logger.info("Starting saveSampleStatuses operation")

        request = self._build_save_sample_statuses_request(request_params)
        result = self._send_save_sample_statuses(request)

        if result.result_status.is_error:
            self.logger.error("SaveSampleStatuses operation failed: %s", result.result_status.message)
        else:
            self.logger.info("SaveSampleStatuses operation completed successfully")

        return result

    # ------------------------------------------------------------------
    # querySampleStatuses (unary) / querySampleStatusesStream (streaming)
    # ------------------------------------------------------------------

    def _build_query_sample_statuses_request(
        self,
        request_params: QuerySampleStatusesRequestParams,
        page_token: str | None = None,
    ) -> annotation_pb2.QuerySampleStatusesRequest:
        """
        Builds a QuerySampleStatusesRequest from the supplied params and optional page token.  Used by both the unary
        and streaming RPCs (they share the request type); the streaming path must never supply a page_token.
        :param request_params: User parameters for the query.
        :param page_token: Token for retrieving a subsequent page (unary paging only).
        :return: A QuerySampleStatusesRequest for the specified params.
        """
        self.logger.debug("Building QuerySampleStatusesRequest")

        request = annotation_pb2.QuerySampleStatusesRequest()
        # Reuse the timestamps the params converted and validated at construction rather than re-converting, so the
        # request always carries exactly the values the [begin, end) ordering check was applied to.
        request.timeRange.beginTime.CopyFrom(request_params.begin_timestamp)
        request.timeRange.endTime.CopyFrom(request_params.end_timestamp)

        if request_params.pv_names:
            request.pvNames[:] = request_params.pv_names

        if request_params.domains:
            request.domains[:] = request_params.domains

        if request_params.layers:
            request.layers[:] = request_params.layers

        # limit=0 is meaningful (server picks a default), so guard on None rather than truthiness -- cf. issue #13.
        if request_params.limit is not None:
            request.limit = request_params.limit

        if page_token:
            request.pageToken = page_token

        self.logger.debug("QuerySampleStatusesRequest built successfully")
        return request

    def _send_query_sample_statuses(
        self, request: annotation_pb2.QuerySampleStatusesRequest
    ) -> QuerySampleStatusesApiResult:
        """
        Invokes the querySampleStatuses() unary API method with the supplied request.
        :param request: QuerySampleStatusesRequest with parameters for the call.
        :return: A QuerySampleStatusesApiResult with the method response and status information.
        """
        self.logger.info("Calling querySampleStatuses API")

        try:
            self.logger.debug("Invoking stub.querySampleStatuses with request")
            response = self._stub.querySampleStatuses(request)
            self.logger.debug("Received response from querySampleStatuses API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning("QuerySampleStatuses API returned business error: %s", error_msg)
                return QuerySampleStatusesApiResult(is_error=True, message=error_msg)

            elif response.HasField("querySampleStatusesResult"):
                self.logger.info(
                    "Successfully queried %d sample status bucket(s)",
                    len(response.querySampleStatusesResult.sampleStatusBuckets),
                )
                return QuerySampleStatusesApiResult(is_error=False, message="", response=response)

            else:
                error_msg = "Unexpected response format: neither exceptionalResult nor querySampleStatusesResult found"
                self.logger.error(error_msg)
                return QuerySampleStatusesApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during querySampleStatuses: %s", e.details())
            return QuerySampleStatusesApiResult(is_error=True, message=error_msg)

        except Exception as e:
            error_msg = f"Unexpected error: {e!s}"
            self.logger.exception("Unexpected error during querySampleStatuses: %s", str(e))
            return QuerySampleStatusesApiResult(is_error=True, message=error_msg)

    def query_sample_statuses(
        self,
        request_params: QuerySampleStatusesRequestParams,
        page_token: str | None = None,
    ) -> QuerySampleStatusesApiResult:
        """
        User-facing method for invoking the unary querySampleStatuses() API method.  Returns a single page of results;
        use iter_sample_statuses() to page through all results transparently.
        :param request_params: User parameters for the query (see QuerySampleStatusesRequestParams).
        :param page_token: Token for retrieving a subsequent page (optional).
        :return: A QuerySampleStatusesApiResult with a single page of results and status information.
        """
        self.logger.info("Starting querySampleStatuses operation")

        request = self._build_query_sample_statuses_request(request_params, page_token=page_token)
        result = self._send_query_sample_statuses(request)

        if result.result_status.is_error:
            self.logger.error("QuerySampleStatuses operation failed: %s", result.result_status.message)
        else:
            self.logger.info("QuerySampleStatuses operation completed successfully")

        return result

    def iter_sample_statuses(
        self, request_params: QuerySampleStatusesRequestParams
    ) -> Iterator[QuerySampleStatusesApiResult]:
        """
        Convenience generator that transparently pages through all unary querySampleStatuses() results, following the
        nextPageToken until the results are exhausted.  Yields one QuerySampleStatusesApiResult per page.

        Raises RuntimeError if any page returns an error, so callers can distinguish failure from an empty result set.

        :param request_params: User parameters for the query (see QuerySampleStatusesRequestParams).
        :return: An iterator over the result pages.
        """
        page_token: str | None = None
        while True:
            result = self.query_sample_statuses(request_params, page_token=page_token)
            if result.result_status.is_error:
                raise RuntimeError(f"querySampleStatuses failed during paging: {result.result_status.message}")

            yield result

            page_token = result.next_page_token
            if not page_token:
                break

    def _send_query_sample_statuses_stream(
        self, request: annotation_pb2.QuerySampleStatusesRequest
    ) -> Iterator[QuerySampleStatusesApiResult]:
        """
        Invokes the querySampleStatusesStream() server-streaming API method with the supplied request, yielding one
        QuerySampleStatusesApiResult per streamed message.

        Errors are yielded, not raised: a business error on a message, an unrecognized response, or a gRPC/unexpected
        error while iterating the stream each yield an is_error result, and a transport error terminates the generator
        after that final error item.  This is the internal contract -- the public iter_sample_statuses_stream() wrapper
        consumes these and converts the first error result into a RuntimeError, so callers of the public method never
        see an error result yielded.

        :param request: QuerySampleStatusesRequest with parameters for the call (must carry no page token).
        :return: An iterator over the streamed result messages, possibly ending in an error result.
        """
        self.logger.info("Calling querySampleStatusesStream API")

        try:
            self.logger.debug("Invoking stub.querySampleStatusesStream with request")
            stream = self._stub.querySampleStatusesStream(request)
            for response in stream:
                if response.HasField("exceptionalResult"):
                    error_msg = response.exceptionalResult.message
                    self.logger.warning("QuerySampleStatusesStream returned business error: %s", error_msg)
                    yield QuerySampleStatusesApiResult(is_error=True, message=error_msg)
                elif response.HasField("querySampleStatusesResult"):
                    yield QuerySampleStatusesApiResult(is_error=False, message="", response=response)
                else:
                    error_msg = (
                        "Unexpected response format: neither exceptionalResult nor querySampleStatusesResult found"
                    )
                    self.logger.error(error_msg)
                    yield QuerySampleStatusesApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during querySampleStatusesStream: %s", e.details())
            yield QuerySampleStatusesApiResult(is_error=True, message=error_msg)

        except Exception as e:
            error_msg = f"Unexpected error: {e!s}"
            self.logger.exception("Unexpected error during querySampleStatusesStream: %s", str(e))
            yield QuerySampleStatusesApiResult(is_error=True, message=error_msg)

    def iter_sample_statuses_stream(
        self, request_params: QuerySampleStatusesRequestParams
    ) -> Iterator[QuerySampleStatusesApiResult]:
        """
        User-facing lazy generator for the server-streaming querySampleStatusesStream() API method.  Yields one
        QuerySampleStatusesApiResult per streamed message, symmetric with iter_sample_statuses() but fire-and-consume:
        the server pushes messages and there are no page tokens.

        Streaming does not support paging: this method never sets a page token.  (Sending a page token on the
        streaming RPC is a server-side client error.)

        Raises RuntimeError on a mid-stream error, matching the iter_* page-error behavior, so callers can distinguish
        failure from an empty stream.

        :param request_params: User parameters for the query (see QuerySampleStatusesRequestParams).
        :return: A lazy iterator over the streamed result messages.
        """
        self.logger.info("Starting querySampleStatusesStream operation")

        request = self._build_query_sample_statuses_request(request_params, page_token=None)
        for result in self._send_query_sample_statuses_stream(request):
            if result.result_status.is_error:
                raise RuntimeError(f"querySampleStatusesStream failed during streaming: {result.result_status.message}")
            yield result

    # ------------------------------------------------------------------
    # deleteSampleStatuses
    # ------------------------------------------------------------------

    def _build_delete_sample_statuses_request(
        self,
        begin_time: TimestampInput,
        end_time: TimestampInput,
        domain: str,
        layer: str,
        pv_names: list[str] | None = None,
        all_pvs: bool = False,
    ) -> annotation_pb2.DeleteSampleStatusesRequest:
        """
        Builds a DeleteSampleStatusesRequest, validating the required scope and the explicit all-PVs opt-in.
        :param begin_time: Inclusive start of the deletion range.
        :param end_time: Exclusive end of the deletion range.
        :param domain: Required domain scoping the delete.
        :param layer: Required layer scoping the delete.
        :param pv_names: PVs to delete statuses for; mutually exclusive with all_pvs.
        :param all_pvs: Explicit opt-in to the wildcard covering every PV in the (domain, layer).
        :return: A DeleteSampleStatusesRequest for the specified parameters.
        :raises ValueError: if domain or layer is empty, if begin_time is not strictly before end_time, or if the
            pv_names / all_pvs opt-in rule is violated.
        """
        if not domain:
            raise ValueError("delete_sample_statuses() requires a non-empty domain")
        if not layer:
            raise ValueError("delete_sample_statuses() requires a non-empty layer")

        # An omitted pv_names is a wildcard deleting the (domain, layer)'s statuses for EVERY PV in the range, so it
        # must be opted into explicitly rather than reached by leaving an argument off a destructive call.
        if all_pvs and pv_names:
            raise ValueError(
                "delete_sample_statuses() accepts either pv_names or all_pvs=True, not both; "
                "all_pvs=True deletes statuses for every PV in the (domain, layer)"
            )
        if not all_pvs and not pv_names:
            raise ValueError(
                "delete_sample_statuses() requires either a non-empty pv_names list, or all_pvs=True to "
                "explicitly delete statuses for every PV in the (domain, layer) over the time range"
            )

        begin_ts = to_timestamp(begin_time)
        end_ts = to_timestamp(end_time)
        if (begin_ts.epochSeconds, begin_ts.nanoseconds) >= (end_ts.epochSeconds, end_ts.nanoseconds):
            raise ValueError(
                "delete_sample_statuses() requires begin_time strictly before end_time (half-open [begin, end))"
            )

        self.logger.debug("Building DeleteSampleStatusesRequest for domain=%s layer=%s", domain, layer)

        request = annotation_pb2.DeleteSampleStatusesRequest()
        request.timeRange.beginTime.CopyFrom(begin_ts)
        request.timeRange.endTime.CopyFrom(end_ts)
        request.domain = domain
        request.layer = layer

        if pv_names:
            request.pvNames[:] = pv_names

        self.logger.debug("DeleteSampleStatusesRequest built successfully")
        return request

    def _send_delete_sample_statuses(
        self, request: annotation_pb2.DeleteSampleStatusesRequest
    ) -> DeleteSampleStatusesApiResult:
        """
        Invokes the deleteSampleStatuses() API method with the supplied request.
        :param request: DeleteSampleStatusesRequest with parameters for the call.
        :return: A DeleteSampleStatusesApiResult with the method response and status information.
        """
        self.logger.info("Calling deleteSampleStatuses API for domain=%s layer=%s", request.domain, request.layer)

        try:
            self.logger.debug("Invoking stub.deleteSampleStatuses with request")
            response = self._stub.deleteSampleStatuses(request)
            self.logger.debug("Received response from deleteSampleStatuses API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning("DeleteSampleStatuses API returned business error: %s", error_msg)
                return DeleteSampleStatusesApiResult(is_error=True, message=error_msg)

            elif response.HasField("deleteSampleStatusesResult"):
                self.logger.info(
                    "Successfully deleted %d sample status(es)", response.deleteSampleStatusesResult.deletedCount
                )
                return DeleteSampleStatusesApiResult(is_error=False, message="", response=response)

            else:
                error_msg = "Unexpected response format: neither exceptionalResult nor deleteSampleStatusesResult found"
                self.logger.error(error_msg)
                return DeleteSampleStatusesApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during deleteSampleStatuses: %s", e.details())
            return DeleteSampleStatusesApiResult(is_error=True, message=error_msg)

        except Exception as e:
            error_msg = f"Unexpected error: {e!s}"
            self.logger.exception("Unexpected error during deleteSampleStatuses: %s", str(e))
            return DeleteSampleStatusesApiResult(is_error=True, message=error_msg)

    def delete_sample_statuses(
        self,
        begin_time: TimestampInput,
        end_time: TimestampInput,
        domain: str,
        layer: str,
        pv_names: list[str] | None = None,
        all_pvs: bool = False,
    ) -> DeleteSampleStatusesApiResult:
        """
        User-facing method for invoking the deleteSampleStatuses() API method.  Deletes statuses in the half-open
        range [begin_time, end_time) for a single required (domain, layer).

        Unlike query, deletion is exact at the sample axis: only statuses with timestamps inside the range are
        removed.  A delete matching nothing is a success with deleted_count == 0, not an error.

        The PV scope must be stated explicitly: supply pv_names, or pass all_pvs=True to delete the (domain, layer)'s
        statuses for every PV in the range (the path for retiring an obsolete layer).  Passing neither -- or both --
        raises, so the destructive wildcard is never reached by omitting an argument.  To preview what a wildcard
        delete would remove, run query_sample_statuses() over the same range and (domain, layer) first.

        :param begin_time: Inclusive start of the deletion range (tz-aware datetime, epoch seconds, or Timestamp).
        :param end_time: Exclusive end of the deletion range.
        :param domain: Required domain scoping the delete.
        :param layer: Required layer scoping the delete.
        :param pv_names: PVs to delete statuses for; mutually exclusive with all_pvs.
        :param all_pvs: Explicit opt-in to deleting statuses for every PV in the (domain, layer).
        :return: A DeleteSampleStatusesApiResult with the method response and status information.
        :raises ValueError: if domain or layer is empty, if begin_time is not strictly before end_time, or if
            neither (or both) of pv_names and all_pvs is supplied.
        """
        self.logger.info("Starting deleteSampleStatuses operation for domain=%s layer=%s", domain, layer)

        request = self._build_delete_sample_statuses_request(
            begin_time, end_time, domain, layer, pv_names=pv_names, all_pvs=all_pvs
        )
        result = self._send_delete_sample_statuses(request)

        if result.result_status.is_error:
            self.logger.error("DeleteSampleStatuses operation failed: %s", result.result_status.message)
        else:
            self.logger.info("DeleteSampleStatuses operation completed successfully")

        return result
