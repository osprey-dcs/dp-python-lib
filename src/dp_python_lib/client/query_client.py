from typing import Optional, List, Iterator, Any
from dp_python_lib.client.service_api_client_base import ServiceApiClientBase
from dp_python_lib.client.result import ApiResultBase
from dp_python_lib.client.machine_config_client import to_timestamp, TimestampInput
from dp_python_lib.grpc import query_pb2_grpc
from dp_python_lib.grpc import query_pb2
from dp_python_lib.grpc import common_pb2
import grpc
import logging


class PvQuery:
    """
    Factory of lightweight helpers for building the PvSelector of a QueryParams (see QueryClient).  A PV selector is
    one of three mutually-exclusive forms; use exactly one:

      - PvQuery.name_list([...])      -- an explicit list of PV names.
      - PvQuery.pattern("ABC:.*")     -- a single name pattern.
      - PvQuery.metadata([...])       -- a metadata query built from the metadata-criterion helpers below.

    The metadata-criterion helpers (pv_name/aliases/tags/attr) each return a single criterion; pass a list of them
    to metadata().

    Example:
        from dp_python_lib.client import PvQuery as PV
        selector = PV.metadata([PV.pv_name(prefix=["ABC:"]), PV.tags(["vacuum"])])
    """

    _MetaCriterion = query_pb2.PvSelector.MetadataQuery.Criterion

    @staticmethod
    def name_list(pv_names: List[str]) -> query_pb2.PvSelector:
        """
        Builds a PvSelector selecting an explicit list of PV names.
        :param pv_names: PV names to select.
        :return: A PvSelector with a pvNameList.
        :raises ValueError: if pv_names is empty.
        """
        if not pv_names:
            raise ValueError("name_list() requires a non-empty pv_names list")
        selector = query_pb2.PvSelector()
        selector.pvNameList.pvNames[:] = pv_names
        return selector

    @staticmethod
    def pattern(pattern: str) -> query_pb2.PvSelector:
        """
        Builds a PvSelector selecting PVs whose name matches the given pattern.
        :param pattern: The name pattern to match.
        :return: A PvSelector with a pvNamePattern.
        :raises ValueError: if pattern is empty.
        """
        if not pattern:
            raise ValueError("pattern() requires a non-empty pattern")
        selector = query_pb2.PvSelector()
        selector.pvNamePattern.pattern = pattern
        return selector

    @staticmethod
    def metadata(criteria: List["query_pb2.PvSelector.MetadataQuery.Criterion"]) -> query_pb2.PvSelector:
        """
        Builds a PvSelector selecting PVs matching a metadata query (a list of AND-combined criteria).
        :param criteria: List of metadata criteria (see pv_name/aliases/tags/attr helpers).
        :return: A PvSelector with a metadataQuery.
        :raises ValueError: if criteria is empty.
        """
        if not criteria:
            raise ValueError("metadata() requires a non-empty criteria list")
        selector = query_pb2.PvSelector()
        selector.metadataQuery.criteria.extend(criteria)
        return selector

    @staticmethod
    def pv_name(exact: Optional[List[str]] = None, prefix: Optional[List[str]] = None,
                contains: Optional[List[str]] = None) -> "query_pb2.PvSelector.MetadataQuery.Criterion":
        """
        Builds a metadata criterion matching PV names by exact value, prefix, and/or substring.  The three forms are
        repeated and may coexist (all are ANDed by the server).
        :param exact: PV names to match exactly.
        :param prefix: PV name prefixes to match.
        :param contains: Substrings the PV name must contain.
        :return: A metadata Criterion with a pvNameCriterion.
        :raises ValueError: if none of exact/prefix/contains is provided and non-empty.
        """
        if not (exact or prefix or contains):
            raise ValueError("pv_name() requires at least one non-empty of exact/prefix/contains")
        criterion = PvQuery._MetaCriterion()
        if exact:
            criterion.pvNameCriterion.exact[:] = exact
        if prefix:
            criterion.pvNameCriterion.prefix[:] = prefix
        if contains:
            criterion.pvNameCriterion.contains[:] = contains
        return criterion

    @staticmethod
    def aliases(exact: Optional[List[str]] = None, prefix: Optional[List[str]] = None,
                contains: Optional[List[str]] = None) -> "query_pb2.PvSelector.MetadataQuery.Criterion":
        """
        Builds a metadata criterion matching PV aliases by exact value, prefix, and/or substring.  The three forms are
        repeated and may coexist.
        :param exact: Aliases to match exactly.
        :param prefix: Alias prefixes to match.
        :param contains: Substrings the alias must contain.
        :return: A metadata Criterion with an aliasesCriterion.
        :raises ValueError: if none of exact/prefix/contains is provided and non-empty.
        """
        if not (exact or prefix or contains):
            raise ValueError("aliases() requires at least one non-empty of exact/prefix/contains")
        criterion = PvQuery._MetaCriterion()
        if exact:
            criterion.aliasesCriterion.exact[:] = exact
        if prefix:
            criterion.aliasesCriterion.prefix[:] = prefix
        if contains:
            criterion.aliasesCriterion.contains[:] = contains
        return criterion

    @staticmethod
    def tags(values: List[str]) -> "query_pb2.PvSelector.MetadataQuery.Criterion":
        """
        Builds a metadata criterion matching PVs having any of the specified tags.
        :param values: Tag values to match.
        :return: A metadata Criterion with a tagsCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("tags() requires a non-empty values list")
        criterion = PvQuery._MetaCriterion()
        criterion.tagsCriterion.values[:] = values
        return criterion

    @staticmethod
    def attr(key: str, values: List[str]) -> "query_pb2.PvSelector.MetadataQuery.Criterion":
        """
        Builds a metadata criterion matching PVs whose attribute with the given key has any of the specified values.
        :param key: Attribute key to match.
        :param values: Attribute values to match for that key.
        :return: A metadata Criterion with an attributesCriterion.
        :raises ValueError: if key is empty or values is empty.
        """
        if not key:
            raise ValueError("attr() requires a non-empty key")
        if not values:
            raise ValueError("attr() requires a non-empty values list")
        criterion = PvQuery._MetaCriterion()
        criterion.attributesCriterion.key = key
        criterion.attributesCriterion.values[:] = values
        return criterion


class ConfigQuery:
    """
    Factory of lightweight helpers for building the ConfigurationSelector criteria of a QueryParams (see QueryClient).
    Each helper returns a single criterion; callers pass a list of criteria (AND-combined) as QueryParams.config_criteria.

    A configuration selector restricts query results to the intervals during which matching machine-configuration
    activations were in effect.

    Example:
        from dp_python_lib.client import ConfigQuery as CFG
        config_criteria = [CFG.configuration_name(["beamline-optics"]), CFG.category(["optics"])]
    """

    _Criterion = query_pb2.ConfigurationSelector.Criterion

    @staticmethod
    def configuration_name(values: List[str]) -> "query_pb2.ConfigurationSelector.Criterion":
        """
        Builds a criterion matching activations whose configuration name is any of the specified values.
        :param values: Configuration names to match.
        :return: A ConfigurationSelector.Criterion with a configurationNameCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("configuration_name() requires a non-empty values list")
        criterion = ConfigQuery._Criterion()
        criterion.configurationNameCriterion.values[:] = values
        return criterion

    @staticmethod
    def client_activation_id(values: List[str]) -> "query_pb2.ConfigurationSelector.Criterion":
        """
        Builds a criterion matching activations whose client activation id is any of the specified values.
        :param values: Client activation ids to match.
        :return: A ConfigurationSelector.Criterion with a clientActivationIdCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("client_activation_id() requires a non-empty values list")
        criterion = ConfigQuery._Criterion()
        criterion.clientActivationIdCriterion.values[:] = values
        return criterion

    @staticmethod
    def category(values: List[str]) -> "query_pb2.ConfigurationSelector.Criterion":
        """
        Builds a criterion matching activations whose configuration category is any of the specified values.
        :param values: Category values to match.
        :return: A ConfigurationSelector.Criterion with a categoryCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("category() requires a non-empty values list")
        criterion = ConfigQuery._Criterion()
        criterion.categoryCriterion.values[:] = values
        return criterion

    @staticmethod
    def tags(values: List[str]) -> "query_pb2.ConfigurationSelector.Criterion":
        """
        Builds a criterion matching activations having any of the specified tags.
        :param values: Tag values to match.
        :return: A ConfigurationSelector.Criterion with a tagsCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("tags() requires a non-empty values list")
        criterion = ConfigQuery._Criterion()
        criterion.tagsCriterion.values[:] = values
        return criterion

    @staticmethod
    def attr(key: str, values: List[str]) -> "query_pb2.ConfigurationSelector.Criterion":
        """
        Builds a criterion matching activations whose attribute with the given key has any of the specified values.
        :param key: Attribute key to match.
        :param values: Attribute values to match for that key.
        :return: A ConfigurationSelector.Criterion with an attributesCriterion.
        :raises ValueError: if key is empty or values is empty.
        """
        if not key:
            raise ValueError("attr() requires a non-empty key")
        if not values:
            raise ValueError("attr() requires a non-empty values list")
        criterion = ConfigQuery._Criterion()
        criterion.attributesCriterion.key = key
        criterion.attributesCriterion.values[:] = values
        return criterion


class QueryParams:
    """
    Encapsulates client parameters for a v2 time-series query.  This is the kind-neutral representation of the shared
    QuerySpec: it is used to build both the sample-oriented querySamples()/querySamplesStream() requests (in scope for
    this release) and, in a future release, the bucket-oriented queryBuckets() requests.

    A query selects data over a half-open time range [begin_time, end_time) for a set of PVs.  The PV set is chosen
    by at most one pv_selector form (see PvQuery: name-list, pattern, or metadata) and/or restricted by a list of
    config_criteria (see ConfigQuery).  A config-only query (all PVs active under a configuration in the window) is
    legal, so pv_selector may be omitted -- but at least one of {pv_selector, config_criteria} must be present.
    """

    def __init__(self, begin_time: TimestampInput, end_time: TimestampInput,
                 pv_selector: Optional[query_pb2.PvSelector] = None,
                 config_criteria: Optional[List["query_pb2.ConfigurationSelector.Criterion"]] = None,
                 limit: Optional[int] = None,
                 exclude_column_metadata: bool = False) -> None:
        """
        :param begin_time: Inclusive start of the query range (tz-aware datetime, epoch seconds, or common.Timestamp).
        :param end_time: Exclusive end of the query range (tz-aware datetime, epoch seconds, or common.Timestamp).
            The range is half-open [begin_time, end_time); the server trims edge samples.
        :param pv_selector: The PV selection (see PvQuery: name_list/pattern/metadata).  Optional for a config-only
            query.  At most one form may be set -- the PvQuery helpers each produce exactly one form.
        :param config_criteria: List of AND-combined configuration criteria (see ConfigQuery) restricting results to
            intervals when matching configurations were active.  Optional.
        :param limit: Maximum number of rows to return per page (optional).  This is a per-page size, NOT a total
            cap.  0 is meaningful and means "let the server pick a default"; a negative value raises.
        :param exclude_column_metadata: If True, omit per-column ColumnMetadata from the results.  Defaults to False
            (metadata included).
        :raises ValueError: if both begin_time and end_time are not supplied, if neither pv_selector nor
            config_criteria is present, if begin_time is not strictly before end_time, or if limit is negative.
        """
        if begin_time is None or end_time is None:
            raise ValueError("QueryParams requires both begin_time and end_time")

        begin_ts = to_timestamp(begin_time)
        end_ts = to_timestamp(end_time)
        if (begin_ts.epochSeconds, begin_ts.nanoseconds) >= (end_ts.epochSeconds, end_ts.nanoseconds):
            raise ValueError(
                "QueryParams requires begin_time strictly before end_time (half-open [begin, end))")

        if pv_selector is None and not config_criteria:
            raise ValueError(
                "QueryParams requires at least one of pv_selector or config_criteria")

        # Validate eagerly here rather than letting a negative surface as a raw protobuf range error deep in
        # request building (ExecutionOptions.limit is a uint32).  Note limit=0 is explicitly meaningful per the
        # proto -- "if limit is omitted (0) the server selects an appropriate default" -- so only reject < 0.
        if limit is not None and limit < 0:
            raise ValueError(f"QueryParams limit must be non-negative, got {limit}")

        self.begin_time = begin_time
        self.end_time = end_time
        self._begin_ts = begin_ts
        self._end_ts = end_ts
        self.pv_selector = pv_selector
        self.config_criteria = config_criteria
        self.limit = limit
        self.exclude_column_metadata = exclude_column_metadata

    @property
    def begin_timestamp(self) -> common_pb2.Timestamp:
        """The validated common.Timestamp for begin_time, converted once at construction."""
        return self._begin_ts

    @property
    def end_timestamp(self) -> common_pb2.Timestamp:
        """The validated common.Timestamp for end_time, converted once at construction."""
        return self._end_ts


class QuerySamplesApiResult(ApiResultBase):
    """
    Wraps a single page (unary) or a single streamed message (streaming) of a querySamples()/querySamplesStream()
    response, with a status object including an error flag and message.

    The raw protobuf ColumnTable is available via .column_table.  Convenience conversions to pandas/NumPy are provided
    by .to_dataframe()/.to_numpy(), which require the optional [analysis] extra (pandas/numpy) -- see query_conversions.
    """

    def __init__(self, is_error: bool, message: str,
                 response: Optional[query_pb2.QuerySamplesResponse] = None) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The QuerySamplesResponse for this page/message, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def column_table(self) -> Optional[query_pb2.ColumnTable]:
        """The raw ColumnTable for this page/message, or None on error."""
        if self.response is not None and self.response.HasField('sampleQueryResult'):
            return self.response.sampleQueryResult.columnTable
        return None

    @property
    def next_page_token(self) -> str:
        """
        Token for retrieving the next page (unary querySamples() only), or empty string if there are no more pages.
        Always empty for streamed messages (querySamplesStream() is fire-and-consume).
        """
        if self.response is not None and self.response.HasField('sampleQueryResult'):
            return self.response.sampleQueryResult.nextPageToken
        return ""

    def to_dataframe(self, exclude_column_metadata: bool = False) -> Any:
        """
        Converts this page's ColumnTable into a pandas DataFrame (timestamp index + one column per DataColumn).
        Requires the optional [analysis] extra.  Delegates to query_conversions (imported lazily so the core client
        carries no pandas/numpy dependency).
        :param exclude_column_metadata: If True, do not attach per-column ColumnMetadata to the DataFrame.
        :return: A pandas.DataFrame for this page.
        """
        from dp_python_lib.client import query_conversions
        return query_conversions.column_table_to_dataframe(
            self.column_table, exclude_column_metadata=exclude_column_metadata)

    def to_numpy(self) -> Any:
        """
        Converts this page's ColumnTable into NumPy arrays (a dict of column-name -> ndarray).  Requires the optional
        [analysis] extra.  Delegates to query_conversions (imported lazily).
        :return: A dict of column name to numpy.ndarray for this page.
        """
        from dp_python_lib.client import query_conversions
        return query_conversions.column_table_to_numpy(self.column_table)


class QueryClient(ServiceApiClientBase):
    """
    User-facing client for the sample-oriented v2 query methods of the MLDP Query Service: querySamples() (unary, one
    resumable page) and querySamplesStream() (server-streaming, fire-and-consume).  Provides low-level wrappers around
    the raw protobuf ColumnTable plus transparent-paging iterators.  Higher-level pandas/NumPy/Excel conversions live in
    query_conversions and are reached via QuerySamplesApiResult.to_dataframe()/.to_numpy().

    Queries are described by a kind-neutral QueryParams built from the PvQuery (PV) and ConfigQuery (CFG) helpers.
    """

    def __init__(self, channel: grpc.Channel) -> None:
        """
        :param channel: gRPC communication channel for the Query Service.
        """
        super().__init__(channel, query_pb2_grpc.DpQueryServiceStub)
        self.logger = logging.getLogger(__name__)
        self.logger.debug("QueryClient initialized with channel: %s", channel)

    # ------------------------------------------------------------------
    # Request building (shared QuerySpec seam)
    # ------------------------------------------------------------------

    def _build_query_spec(self, request_params: QueryParams) -> query_pb2.QuerySpec:
        """
        Builds the shared QuerySpec (time range + PV selector + configuration selector) from the supplied QueryParams.
        Factored out so both _build_query_samples_request() and a future _build_query_buckets_request() reuse it.
        :param request_params: User parameters for the query.
        :return: A QuerySpec for the specified params.
        """
        self.logger.debug("Building QuerySpec")
        spec = query_pb2.QuerySpec()
        # Reuse the timestamps QueryParams converted and validated at construction rather than re-converting,
        # so the request always carries exactly the values the [begin, end) ordering check was applied to.
        spec.timeRange.beginTime.CopyFrom(request_params.begin_timestamp)
        spec.timeRange.endTime.CopyFrom(request_params.end_timestamp)

        if request_params.pv_selector is not None:
            spec.pvSelector.CopyFrom(request_params.pv_selector)

        if request_params.config_criteria:
            spec.configurationSelector.criteria.extend(request_params.config_criteria)

        return spec

    def _build_query_samples_request(
            self, request_params: QueryParams,
            page_token: Optional[str] = None) -> query_pb2.QuerySamplesRequest:
        """
        Builds a QuerySamplesRequest from the supplied QueryParams and optional page token.  Used by both the unary and
        streaming RPCs (they share the request type); the streaming path must never supply a page_token.
        :param request_params: User parameters for the query.
        :param page_token: Token for retrieving a subsequent page (unary paging only).
        :return: A QuerySamplesRequest for the specified params.
        """
        self.logger.debug("Building QuerySamplesRequest")
        request = query_pb2.QuerySamplesRequest()
        request.querySpec.CopyFrom(self._build_query_spec(request_params))

        if request_params.limit is not None:
            request.executionOptions.limit = request_params.limit
        if page_token:
            request.executionOptions.pageToken = page_token

        # v1 always uses dense dataColumns; serialized columns are deferred (Q3).
        request.resultRepresentation.useSerializedColumns = False
        request.resultRepresentation.excludeColumnMetadata = request_params.exclude_column_metadata

        return request

    # ------------------------------------------------------------------
    # querySamples (unary)
    # ------------------------------------------------------------------

    def _send_query_samples(self, request: query_pb2.QuerySamplesRequest) -> QuerySamplesApiResult:
        """
        Invokes the querySamples() unary API method with the supplied request.
        :param request: QuerySamplesRequest with parameters for the call.
        :return: A QuerySamplesApiResult with the method response and status information.
        """
        self.logger.info("Calling querySamples API")

        try:
            self.logger.debug("Invoking stub.querySamples with request")
            response = self._stub.querySamples(request)
            self.logger.debug("Received response from querySamples API")

            if response.HasField('exceptionalResult'):
                error_msg = response.exceptionalResult.message
                self.logger.warning("QuerySamples API returned business error: %s", error_msg)
                return QuerySamplesApiResult(is_error=True, message=error_msg)

            elif response.HasField('sampleQueryResult'):
                self.logger.info("QuerySamples returned a result page")
                return QuerySamplesApiResult(is_error=False, message="", response=response)

            else:
                error_msg = "Unexpected response format: neither exceptionalResult nor sampleQueryResult found"
                self.logger.error(error_msg)
                return QuerySamplesApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during querySamples: %s", e.details())
            return QuerySamplesApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.logger.error("Unexpected error during querySamples: %s", str(e), exc_info=True)
            return QuerySamplesApiResult(is_error=True, message=error_msg)

    def query_samples(self, request_params: QueryParams,
                      page_token: Optional[str] = None) -> QuerySamplesApiResult:
        """
        User-facing method for invoking the unary querySamples() API method.  Returns a single page of results; use
        iter_query_samples() to page through all results transparently.
        :param request_params: User parameters for the query (see QueryParams / PvQuery / ConfigQuery).
        :param page_token: Token for retrieving a subsequent page (optional).
        :return: A QuerySamplesApiResult with a single page of results and status information.
        """
        self.logger.info("Starting querySamples operation")

        request = self._build_query_samples_request(request_params, page_token=page_token)
        result = self._send_query_samples(request)

        if result.result_status.is_error:
            self.logger.error("QuerySamples operation failed: %s", result.result_status.message)
        else:
            self.logger.info("QuerySamples operation completed successfully")

        return result

    def iter_query_samples(self, request_params: QueryParams) -> Iterator[QuerySamplesApiResult]:
        """
        Convenience generator that transparently pages through all unary querySamples() results, following the
        nextPageToken (reached via response.sampleQueryResult.nextPageToken) until the results are exhausted.  Yields
        one QuerySamplesApiResult per page.

        Raises RuntimeError if any page returns an error, so callers can distinguish failure from an empty result set.

        :param request_params: User parameters for the query (see QueryParams / PvQuery / ConfigQuery).
        :return: An iterator over the result pages.
        """
        page_token: Optional[str] = None
        while True:
            result = self.query_samples(request_params, page_token=page_token)
            if result.result_status.is_error:
                raise RuntimeError(f"querySamples failed during paging: {result.result_status.message}")

            yield result

            page_token = result.next_page_token
            if not page_token:
                break

    # ------------------------------------------------------------------
    # querySamplesStream (server-streaming)
    # ------------------------------------------------------------------

    def _send_query_samples_stream(
            self, request: query_pb2.QuerySamplesRequest) -> Iterator[QuerySamplesApiResult]:
        """
        Invokes the querySamplesStream() server-streaming API method with the supplied request, yielding one
        QuerySamplesApiResult per streamed message.

        Errors are yielded, not raised: a business error on a message, an unrecognized response, or a gRPC/unexpected
        error while iterating the stream each yield an is_error result, and a transport error terminates the
        generator after that final error item.  This is the internal contract -- the public
        iter_query_samples_stream() wrapper consumes these and converts the first error result into a RuntimeError,
        so callers of the public method never see an error result yielded.

        :param request: QuerySamplesRequest with parameters for the call (must carry no page token).
        :return: An iterator over the streamed result messages, possibly ending in an error result.
        """
        self.logger.info("Calling querySamplesStream API")

        try:
            self.logger.debug("Invoking stub.querySamplesStream with request")
            stream = self._stub.querySamplesStream(request)
            for response in stream:
                if response.HasField('exceptionalResult'):
                    error_msg = response.exceptionalResult.message
                    self.logger.warning("QuerySamplesStream returned business error: %s", error_msg)
                    yield QuerySamplesApiResult(is_error=True, message=error_msg)
                elif response.HasField('sampleQueryResult'):
                    yield QuerySamplesApiResult(is_error=False, message="", response=response)
                else:
                    error_msg = ("Unexpected response format: neither exceptionalResult nor "
                                 "sampleQueryResult found")
                    self.logger.error(error_msg)
                    yield QuerySamplesApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during querySamplesStream: %s", e.details())
            yield QuerySamplesApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.logger.error("Unexpected error during querySamplesStream: %s", str(e), exc_info=True)
            yield QuerySamplesApiResult(is_error=True, message=error_msg)

    def iter_query_samples_stream(self, request_params: QueryParams) -> Iterator[QuerySamplesApiResult]:
        """
        User-facing lazy generator for the server-streaming querySamplesStream() API method.  Yields one
        QuerySamplesApiResult per streamed message, symmetric with iter_query_samples() but fire-and-consume: the
        server pushes pages and there are no page tokens.  The stream is not concatenated -- callers who want the whole
        result assemble it themselves (or use the unary path).

        Streaming does not support paging: this method never sets a page token.  (Sending a page token on the streaming
        RPC is a server-side client error.)

        Raises RuntimeError on a mid-stream error, matching the iter_* page-error behavior, so callers can distinguish
        failure from an empty stream.

        :param request_params: User parameters for the query (see QueryParams / PvQuery / ConfigQuery).
        :return: A lazy iterator over the streamed result messages.
        """
        self.logger.info("Starting querySamplesStream operation")

        request = self._build_query_samples_request(request_params, page_token=None)
        for result in self._send_query_samples_stream(request):
            if result.result_status.is_error:
                raise RuntimeError(
                    f"querySamplesStream failed during streaming: {result.result_status.message}")
            yield result
