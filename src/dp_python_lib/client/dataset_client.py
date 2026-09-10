import logging
from collections.abc import Iterator

import grpc

from dp_python_lib.client.query_support import check_at_most_one_text_criterion
from dp_python_lib.client.result import ApiResultBase
from dp_python_lib.client.service_api_client_base import ServiceApiClientBase
from dp_python_lib.client.time_conversions import TimestampInput, to_timestamp
from dp_python_lib.grpc import annotation_pb2, annotation_pb2_grpc, common_pb2

ID_QUERY_CHUNK_SIZE = 100
"""
Default number of ids per query in get_datasets().  Keeps a single request's $in clause and message size bounded
when the caller passes an arbitrarily long id list; the value is a conservative round number, not a server limit.
"""


def data_block(
    begin_time: TimestampInput,
    end_time: TimestampInput,
    pv_names: list[str],
) -> annotation_pb2.DataBlock:
    """
    Builds a DataBlock: one time range plus the PV names covered over it.  A DataSet is a list of these, and the
    exportData() API accepts them inline for a one-off export that does not warrant saving a DataSet.

    The range is HALF-OPEN, [begin_time, end_time), matching the v2 query API's QueryParams.  annotation.proto does
    not say so, but the server's sample-level retention test does: TabularDataUtility.isRetained() excludes a sample
    landing exactly on end_time ("a sample exactly at an interval's end belongs to the next interval, not this one"),
    and the export job reaches that same function that querySamples() does.  So back-to-back blocks -- one ending at
    T, the next beginning at T -- cover the sample at T exactly once.

    That holds for CSV and XLSX exports.  HDF5 export is bucket-granular: ExportDataJobAbstractBucketed writes every
    bucket that OVERLAPS the block, whole and untrimmed, so an HDF5 file can contain samples outside the requested
    range, and back-to-back blocks sharing a straddling bucket write it twice.  Nothing client-side can change that;
    it is a property of the export format.

    Note that begin < end is checked HERE and nowhere else: the server validates only that each bound is non-zero and
    that pvNames is non-empty (AnnotationValidationUtility.validateDataBlock), and never compares the two bounds, so
    a reversed block would otherwise be accepted and stored.

    :param begin_time: Start of the block's time range, inclusive (tz-aware datetime, epoch seconds,
        or common.Timestamp).
    :param end_time: End of the block's time range, exclusive (same accepted forms).
    :param pv_names: Names of the PVs the block covers.  A list -- passing a bare string is rejected rather than
        silently iterated into one PV name per character.
    :return: An annotation.DataBlock for the specified range and PVs.
    :raises ValueError: if pv_names is empty or a bare string, or begin_time is not strictly before end_time.
    """
    if isinstance(pv_names, str):
        raise ValueError(f"data_block() requires a list of PV names, not a bare string; got {pv_names!r}")
    if not pv_names:
        raise ValueError("data_block() requires a non-empty pv_names list")

    begin = to_timestamp(begin_time)
    end = to_timestamp(end_time)
    if (begin.epochSeconds, begin.nanoseconds) >= (end.epochSeconds, end.nanoseconds):
        raise ValueError(
            f"data_block() requires begin_time strictly before end_time; got begin "
            f"{begin.epochSeconds}.{begin.nanoseconds:09d} and end {end.epochSeconds}.{end.nanoseconds:09d}"
        )

    block = annotation_pb2.DataBlock()
    block.beginTime.CopyFrom(begin)
    block.endTime.CopyFrom(end)
    block.pvNames[:] = pv_names
    return block


class DataSetQuery:
    """
    Factory of lightweight helpers for building QueryDataSetsRequest.QueryDataSetsCriterion objects for use with
    DataSetClient.query_datasets() and iter_datasets().  Each helper returns a single criterion; callers pass a list
    of criteria to the query methods.

    Criteria AND across the list and OR within a single criterion.  An empty or omitted criteria list matches all
    DataSets, so "browse everything, paged" is a legitimate call.

    Example:
        from dp_python_lib.client import DataSetQuery as DS
        criteria = [DS.owners(["cmcchesney"]), DS.tags(["ramp-study"])]
        result = client.annotation.datasets.query_datasets(criteria)
    """

    _Criterion = annotation_pb2.QueryDataSetsRequest.QueryDataSetsCriterion

    @staticmethod
    def ids(values: list[str]) -> "annotation_pb2.QueryDataSetsRequest.QueryDataSetsCriterion":
        """
        Builds a criterion matching DataSets with any of the specified ids.
        :param values: DataSet ids to match.
        :return: A QueryDataSetsCriterion with an idCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("ids() requires a non-empty values list")
        criterion = DataSetQuery._Criterion()
        criterion.idCriterion.ids[:] = values
        return criterion

    @staticmethod
    def owners(values: list[str]) -> "annotation_pb2.QueryDataSetsRequest.QueryDataSetsCriterion":
        """
        Builds a criterion matching DataSets owned by any of the specified owner ids.
        :param values: Owner ids to match.
        :return: A QueryDataSetsCriterion with an ownerCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("owners() requires a non-empty values list")
        criterion = DataSetQuery._Criterion()
        criterion.ownerCriterion.ownerIds[:] = values
        return criterion

    @staticmethod
    def name(
        exact: list[str] | None = None,
        prefix: list[str] | None = None,
        contains: list[str] | None = None,
    ) -> "annotation_pb2.QueryDataSetsRequest.QueryDataSetsCriterion":
        """
        Builds a criterion matching DataSet names by exact value, prefix, and/or substring.
        :param exact: Names to match exactly.
        :param prefix: Name prefixes to match.
        :param contains: Substrings the name must contain.
        :return: A QueryDataSetsCriterion with a nameCriterion.
        :raises ValueError: if none of exact/prefix/contains is provided and non-empty.
        """
        if not (exact or prefix or contains):
            raise ValueError("name() requires at least one non-empty of exact/prefix/contains")
        criterion = DataSetQuery._Criterion()
        name_criterion = criterion.nameCriterion
        if exact:
            name_criterion.exact[:] = exact
        if prefix:
            name_criterion.prefix[:] = prefix
        if contains:
            name_criterion.contains[:] = contains
        return criterion

    @staticmethod
    def text(text: str) -> "annotation_pb2.QueryDataSetsRequest.QueryDataSetsCriterion":
        """
        Builds a criterion running a full-text search over the DataSet's indexed text fields (name and description).

        This is a collection-level text index search, not a per-field match; use name() when a match must be
        restricted to the name.  At most ONE text criterion is allowed per request -- two $text clauses cannot be
        ANDed, so the server rejects a second one, and query_datasets() rejects it client-side first.

        :param text: The text to search for.
        :return: A QueryDataSetsCriterion with a textCriterion.
        :raises ValueError: if text is empty.
        """
        if not text:
            raise ValueError("text() requires a non-empty text value")
        criterion = DataSetQuery._Criterion()
        criterion.textCriterion.text = text
        return criterion

    @staticmethod
    def pv_names(values: list[str]) -> "annotation_pb2.QueryDataSetsRequest.QueryDataSetsCriterion":
        """
        Builds a criterion matching DataSets whose data blocks cover any of the specified PV names.
        :param values: PV names to match.
        :return: A QueryDataSetsCriterion with a pvNameCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("pv_names() requires a non-empty values list")
        criterion = DataSetQuery._Criterion()
        criterion.pvNameCriterion.names[:] = values
        return criterion

    @staticmethod
    def tags(values: list[str]) -> "annotation_pb2.QueryDataSetsRequest.QueryDataSetsCriterion":
        """
        Builds a criterion matching DataSets having any of the specified tags.

        Tags are normalized (lowercased, deduplicated, sorted) when saved, so match against the lowercase form.

        :param values: Tag values to match.
        :return: A QueryDataSetsCriterion with a tagsCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("tags() requires a non-empty values list")
        criterion = DataSetQuery._Criterion()
        criterion.tagsCriterion.values[:] = values
        return criterion

    @staticmethod
    def attributes(
        key: str, values: list[str] | None = None
    ) -> "annotation_pb2.QueryDataSetsRequest.QueryDataSetsCriterion":
        """
        Builds a criterion matching DataSets by attribute key and optional value(s).

        Omitting values (or passing an empty list) performs a key-only existence search: any DataSet possessing the
        key matches, whatever its value.  Every attribute helper in the library behaves this way (issue #40
        back-ported it to the older PvMetadataQuery/ConfigurationQuery helpers, which originally required values).

        :param key: Attribute key to match (maps to Attribute.name).
        :param values: Attribute values to match for that key, or None for a key-only existence search.
        :return: A QueryDataSetsCriterion with an attributesCriterion.
        :raises ValueError: if key is empty.
        """
        if not key:
            raise ValueError("attributes() requires a non-empty key")
        criterion = DataSetQuery._Criterion()
        criterion.attributesCriterion.key = key
        if values:
            criterion.attributesCriterion.values[:] = values
        return criterion


class SaveDataSetRequestParams:
    """
    Encapsulates client parameters for a call to the saveDataSet() API method.

    Saving is an id-driven upsert that REPLACES IN FULL: supplying dataset_id replaces that DataSet with exactly the
    content given here, so omitted fields are cleared rather than left alone.  Read the current state with
    get_dataset() and resend it with your changes.  Omitting dataset_id creates a new DataSet.
    """

    def __init__(
        self,
        name: str,
        owner_id: str,
        data_blocks: list[annotation_pb2.DataBlock],
        description: str | None = None,
        tags: list[str] | None = None,
        attributes: dict[str, str] | None = None,
        modified_by: str | None = None,
        dataset_id: str | None = None,
    ) -> None:
        """
        :param name: Human-readable name of the DataSet.  Required by the server.
        :param owner_id: Identifier of the DataSet's owner.  Required by the server.
        :param data_blocks: The time ranges and PV names the DataSet covers (see data_block()).  Must be non-empty.
        :param description: Human-readable description of the DataSet.
        :param tags: List of tags (keywords) describing the DataSet.  Normalized lowercase/deduplicated/sorted on save.
        :param attributes: Map of key/value attributes describing the DataSet.
        :param modified_by: Identifier of the user or process making the change.
        :param dataset_id: Id of an existing DataSet to replace in full.  Omit to create a new one.
        :raises ValueError: if name, owner_id, or data_blocks is empty.
        """
        if not name:
            raise ValueError("SaveDataSetRequestParams requires a non-empty name")
        if not owner_id:
            raise ValueError("SaveDataSetRequestParams requires a non-empty owner_id")
        if not data_blocks:
            raise ValueError("SaveDataSetRequestParams requires a non-empty data_blocks list")

        self.name = name
        self.owner_id = owner_id
        self.data_blocks = data_blocks
        self.description = description
        self.tags = tags
        self.attributes = attributes
        self.modified_by = modified_by
        self.dataset_id = dataset_id


class SaveDataSetApiResult(ApiResultBase):
    """
    Wraps the response from saveDataSet(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.SaveDataSetResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The SaveDataSetResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def dataset_id(self) -> str | None:
        """Id of the DataSet that was saved, or None on error."""
        if self.response is not None and self.response.HasField("saveDataSetResult"):
            return self.response.saveDataSetResult.dataSetId
        return None


class GetDataSetApiResult(ApiResultBase):
    """
    Wraps the response from getDataSet(), with a status object including an error flag and message.

    A DataSet that does not exist is a business error, not an empty success.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.GetDataSetResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The GetDataSetResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def dataset(self) -> annotation_pb2.DataSet | None:
        """The requested DataSet, or None on error."""
        if self.response is not None and self.response.HasField("getDataSetResult"):
            return self.response.getDataSetResult.dataSet
        return None


class QueryDataSetsApiResult(ApiResultBase):
    """
    Wraps a single page of the response from queryDataSets(), with a status object including an error flag and
    message.  Use DataSetClient.iter_datasets() to transparently page through all results.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.QueryDataSetsResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The QueryDataSetsResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def datasets(self) -> list[annotation_pb2.DataSet]:
        """The DataSet records in this page, or an empty list on error."""
        if self.response is not None and self.response.HasField("dataSetsResult"):
            return list(self.response.dataSetsResult.dataSets)
        return []

    @property
    def next_page_token(self) -> str:
        """Token for retrieving the next page, or empty string if there are no more pages."""
        if self.response is not None and self.response.HasField("dataSetsResult"):
            return self.response.dataSetsResult.nextPageToken
        return ""


class DeleteDataSetApiResult(ApiResultBase):
    """
    Wraps the response from deleteDataSet(), with a status object including an error flag and message.

    Two cases surface as business errors rather than successes: deleting a DataSet still referenced by an annotation
    (the message names one referencing annotation id and the total count), and deleting one that does not exist.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.DeleteDataSetResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The DeleteDataSetResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def dataset_id(self) -> str | None:
        """Id of the DataSet that was deleted, or None on error."""
        if self.response is not None and self.response.HasField("deleteDataSetResult"):
            return self.response.deleteDataSetResult.dataSetId
        return None


class DataSetClient(ServiceApiClientBase):
    """
    User-facing client for the DataSet methods of the MLDP Annotation Service.  A DataSet names a region of the
    archive -- a list of DataBlocks, each a time range plus the PVs covered over it -- so that region can be found,
    annotated, and exported later.

    Provides low-level wrappers for saveDataSet(), getDataSet(), queryDataSets(), and deleteDataSet(), plus the
    iter_datasets() paging iterator and the get_datasets() batch fetch.

    patchDataSet() is not wrapped: it is a reserved placeholder that returns "not implemented".
    """

    def __init__(self, channel: grpc.Channel) -> None:
        """
        :param channel: gRPC communication channel for the Annotation Service.
        """
        super().__init__(channel, annotation_pb2_grpc.DpAnnotationServiceStub)
        self.logger = logging.getLogger(__name__)
        self.logger.debug("DataSetClient initialized with channel: %s", channel)

    # ------------------------------------------------------------------
    # saveDataSet
    # ------------------------------------------------------------------

    def _build_save_dataset_request(
        self, request_params: SaveDataSetRequestParams
    ) -> annotation_pb2.SaveDataSetRequest:
        """
        Builds a SaveDataSetRequest from the supplied SaveDataSetRequestParams.
        :param request_params: User parameters for the call to saveDataSet().
        :return: A SaveDataSetRequest for the specified params.
        """
        self.logger.debug("Building SaveDataSetRequest for DataSet: %s", request_params.name)

        request = annotation_pb2.SaveDataSetRequest()
        request.name = request_params.name
        request.ownerId = request_params.owner_id

        if request_params.dataset_id:
            request.id = request_params.dataset_id

        if request_params.data_blocks:
            request.dataBlocks.extend(request_params.data_blocks)

        if request_params.description:
            request.description = request_params.description

        if request_params.tags:
            request.tags[:] = request_params.tags

        if request_params.attributes:
            for name, value in request_params.attributes.items():
                attribute = common_pb2.Attribute()
                attribute.name = name
                attribute.value = value
                request.attributes.append(attribute)

        if request_params.modified_by:
            request.modifiedBy = request_params.modified_by

        self.logger.debug("SaveDataSetRequest built successfully")
        return request

    def _send_save_dataset(self, request: annotation_pb2.SaveDataSetRequest) -> SaveDataSetApiResult:
        """
        Invokes the saveDataSet() API method with the supplied request.
        :param request: SaveDataSetRequest with parameters for the call.
        :return: A SaveDataSetApiResult with the method response and status information.
        """
        return self._dispatch(
            self._stub.saveDataSet,
            request,
            SaveDataSetApiResult,
            "saveDataSetResult",
            "saveDataSet",
            request_log=lambda: self.logger.info(
                "Calling saveDataSet API for DataSet: %s with %d data blocks", request.name, len(request.dataBlocks)
            ),
            success_log=lambda response: self.logger.info(
                "Successfully saved DataSet: %s with id: %s", request.name, response.saveDataSetResult.dataSetId
            ),
        )

    def save_dataset(self, request_params: SaveDataSetRequestParams) -> SaveDataSetApiResult:
        """
        User-facing method for invoking the saveDataSet() API method.

        Saving replaces in full when request_params.dataset_id is set: omitted fields are cleared, not preserved.

        :param request_params: Contains user parameters for the call to saveDataSet().
        :return: A SaveDataSetApiResult with the method response and status information.
        """
        self.logger.info("Starting saveDataSet operation for DataSet: %s", request_params.name)

        request = self._build_save_dataset_request(request_params)
        result = self._send_save_dataset(request)

        if result.result_status.is_error:
            self.logger.error("SaveDataSet operation failed: %s", result.result_status.message)
        else:
            self.logger.info("SaveDataSet operation completed successfully for DataSet: %s", request_params.name)

        return result

    # ------------------------------------------------------------------
    # getDataSet
    # ------------------------------------------------------------------

    def _build_get_dataset_request(self, dataset_id: str) -> annotation_pb2.GetDataSetRequest:
        """
        Builds a GetDataSetRequest for the supplied DataSet id.
        :param dataset_id: Id of the DataSet to retrieve.
        :return: A GetDataSetRequest for the specified id.
        """
        self.logger.debug("Building GetDataSetRequest for id: %s", dataset_id)
        request = annotation_pb2.GetDataSetRequest()
        request.dataSetId = dataset_id
        return request

    def _send_get_dataset(self, request: annotation_pb2.GetDataSetRequest) -> GetDataSetApiResult:
        """
        Invokes the getDataSet() API method with the supplied request.
        :param request: GetDataSetRequest with parameters for the call.
        :return: A GetDataSetApiResult with the method response and status information.
        """
        return self._dispatch(
            self._stub.getDataSet,
            request,
            GetDataSetApiResult,
            "getDataSetResult",
            "getDataSet",
            request_log=lambda: self.logger.info("Calling getDataSet API for id: %s", request.dataSetId),
            success_log=lambda _response: self.logger.info(
                "Successfully retrieved DataSet for id: %s", request.dataSetId
            ),
        )

    def get_dataset(self, dataset_id: str) -> GetDataSetApiResult:
        """
        User-facing method for invoking the getDataSet() API method.

        A DataSet that does not exist comes back as a business error, not an empty success.

        :param dataset_id: Id of the DataSet to retrieve.
        :return: A GetDataSetApiResult with the method response and status information.
        """
        self.logger.info("Starting getDataSet operation for id: %s", dataset_id)

        request = self._build_get_dataset_request(dataset_id)
        result = self._send_get_dataset(request)

        if result.result_status.is_error:
            self.logger.error("GetDataSet operation failed: %s", result.result_status.message)
        else:
            self.logger.info("GetDataSet operation completed successfully for id: %s", dataset_id)

        return result

    # ------------------------------------------------------------------
    # queryDataSets
    # ------------------------------------------------------------------

    def _build_query_datasets_request(
        self,
        criteria: list[annotation_pb2.QueryDataSetsRequest.QueryDataSetsCriterion] | None = None,
        limit: int | None = None,
        page_token: str | None = None,
    ) -> annotation_pb2.QueryDataSetsRequest:
        """
        Builds a QueryDataSetsRequest from the supplied criteria and paging parameters.
        :param criteria: List of QueryDataSetsCriterion objects (see DataSetQuery helpers), or None to match all.
        :param limit: Maximum number of records to return per page (optional).
        :param page_token: Token for retrieving a subsequent page (optional).
        :return: A QueryDataSetsRequest for the specified params.
        """
        self.logger.debug("Building QueryDataSetsRequest with %d criteria", len(criteria) if criteria else 0)
        request = annotation_pb2.QueryDataSetsRequest()
        if criteria:
            request.criteria.extend(criteria)
        if limit is not None:
            request.limit = limit
        if page_token:
            request.pageToken = page_token
        return request

    def _send_query_datasets(self, request: annotation_pb2.QueryDataSetsRequest) -> QueryDataSetsApiResult:
        """
        Invokes the queryDataSets() API method with the supplied request.
        :param request: QueryDataSetsRequest with parameters for the call.
        :return: A QueryDataSetsApiResult with the method response and status information.
        """
        return self._dispatch(
            self._stub.queryDataSets,
            request,
            QueryDataSetsApiResult,
            "dataSetsResult",
            "queryDataSets",
            request_log=lambda: self.logger.info("Calling queryDataSets API with %d criteria", len(request.criteria)),
            success_log=lambda response: self.logger.info(
                "QueryDataSets returned %d records", len(response.dataSetsResult.dataSets)
            ),
        )

    def query_datasets(
        self,
        criteria: list[annotation_pb2.QueryDataSetsRequest.QueryDataSetsCriterion] | None = None,
        limit: int | None = None,
        page_token: str | None = None,
    ) -> QueryDataSetsApiResult:
        """
        User-facing method for invoking the queryDataSets() API method.  Returns a single page of results; use
        iter_datasets() to page through all results transparently.

        An omitted or empty criteria list matches all DataSets.

        :param criteria: List of QueryDataSetsCriterion objects (see DataSetQuery helpers), or None to match all.
        :param limit: Maximum number of records to return PER PAGE -- not a cap on the total (optional).
        :param page_token: Token for retrieving a subsequent page (optional).
        :return: A QueryDataSetsApiResult with a single page of results and status information.
        :raises ValueError: if criteria contains more than one text criterion.
        """
        criteria = criteria or []
        check_at_most_one_text_criterion(criteria, "query_datasets()")

        self.logger.info("Starting queryDataSets operation with %d criteria", len(criteria))

        request = self._build_query_datasets_request(criteria, limit=limit, page_token=page_token)
        result = self._send_query_datasets(request)

        if result.result_status.is_error:
            self.logger.error("QueryDataSets operation failed: %s", result.result_status.message)
        else:
            self.logger.info("QueryDataSets operation completed successfully")

        return result

    def iter_datasets(
        self,
        criteria: list[annotation_pb2.QueryDataSetsRequest.QueryDataSetsCriterion] | None = None,
        limit: int | None = None,
    ) -> Iterator[annotation_pb2.DataSet]:
        """
        Convenience generator that transparently pages through all queryDataSets() results, following the
        nextPageToken until the results are exhausted.  Yields individual DataSet records.

        Raises RuntimeError if any page returns an error, so callers can distinguish failure from an empty result set.

        :param criteria: List of QueryDataSetsCriterion objects (see DataSetQuery helpers), or None to match all.
        :param limit: Maximum number of records to return per page (optional).
        :return: An iterator over all matching DataSet records across all pages.
        :raises ValueError: if criteria contains more than one text criterion.
        :raises RuntimeError: if any page returns an error.
        """
        page_token: str | None = None
        while True:
            result = self.query_datasets(criteria, limit=limit, page_token=page_token)
            if result.result_status.is_error:
                raise RuntimeError(f"queryDataSets failed during paging: {result.result_status.message}")

            yield from result.datasets

            page_token = result.next_page_token
            if not page_token:
                break

    def get_datasets(self, ids: list[str], chunk_size: int = ID_QUERY_CHUNK_SIZE) -> dict[str, annotation_pb2.DataSet]:
        """
        Batch-fetches DataSets by id in a small number of paged queries, rather than one getDataSet() call apiece.

        This exists for the common "a page of annotations needs its datasets" case: annotation records carry
        dataSetIds rather than the DataSets themselves, so resolving them one at a time is an N+1.

        Ids are deduplicated with their order preserved.  An empty ids list returns {} without issuing an RPC,
        because callers feed this from annotation dataSetIds lists that may legitimately be empty.

        Ids are fetched in chunks of chunk_size, because the id list is caller-supplied and effectively unbounded --
        it comes from the dataSetIds of a whole page of annotations -- and one criterion carrying every id becomes a
        single large $in and a correspondingly large request message, which at some size exceeds the gRPC maximum.
        Chunking keeps each request bounded regardless of how many ids are asked for.

        Ids that resolve to nothing are simply absent from the returned dict, because a dangling dataSetIds entry is
        a normal consequence of deleting a DataSet's annotations out from under it, not an error.  Note that an id
        withheld for any other reason is indistinguishable from a dangling one here, so a short result is logged at
        WARNING rather than passed over silently; compare len() against the ids you asked for if it matters.

        :param ids: DataSet ids to fetch.
        :param chunk_size: Maximum number of ids to request per query.  Rarely worth overriding.
        :return: A dict mapping DataSet id to DataSet, for those ids that resolved.
        :raises ValueError: if chunk_size is not positive.
        :raises RuntimeError: if any page returns an error.
        """
        if chunk_size < 1:
            raise ValueError(f"get_datasets() requires a positive chunk_size, got {chunk_size}")

        if not ids:
            self.logger.debug("get_datasets() called with no ids; returning empty result without an RPC")
            return {}

        unique_ids = list(dict.fromkeys(ids))
        self.logger.info("Starting get_datasets batch fetch for %d unique id(s)", len(unique_ids))

        found: dict[str, annotation_pb2.DataSet] = {}
        for offset in range(0, len(unique_ids), chunk_size):
            chunk = unique_ids[offset : offset + chunk_size]
            for dataset in self.iter_datasets([DataSetQuery.ids(chunk)]):
                found[dataset.id] = dataset

        if len(found) < len(unique_ids):
            self.logger.warning(
                "get_datasets resolved only %d of %d requested id(s); the rest named DataSets that do not exist "
                "or were not returned",
                len(found),
                len(unique_ids),
            )
        else:
            self.logger.info("get_datasets resolved %d of %d requested id(s)", len(found), len(unique_ids))
        return found

    # ------------------------------------------------------------------
    # deleteDataSet
    # ------------------------------------------------------------------

    def _build_delete_dataset_request(self, dataset_id: str) -> annotation_pb2.DeleteDataSetRequest:
        """
        Builds a DeleteDataSetRequest for the supplied DataSet id.
        :param dataset_id: Id of the DataSet to delete.
        :return: A DeleteDataSetRequest for the specified id.
        """
        self.logger.debug("Building DeleteDataSetRequest for id: %s", dataset_id)
        request = annotation_pb2.DeleteDataSetRequest()
        request.dataSetId = dataset_id
        return request

    def _send_delete_dataset(self, request: annotation_pb2.DeleteDataSetRequest) -> DeleteDataSetApiResult:
        """
        Invokes the deleteDataSet() API method with the supplied request.
        :param request: DeleteDataSetRequest with parameters for the call.
        :return: A DeleteDataSetApiResult with the method response and status information.
        """
        return self._dispatch(
            self._stub.deleteDataSet,
            request,
            DeleteDataSetApiResult,
            "deleteDataSetResult",
            "deleteDataSet",
            request_log=lambda: self.logger.info("Calling deleteDataSet API for id: %s", request.dataSetId),
            success_log=lambda _response: self.logger.info("Successfully deleted DataSet id: %s", request.dataSetId),
        )

    def delete_dataset(self, dataset_id: str) -> DeleteDataSetApiResult:
        """
        User-facing method for invoking the deleteDataSet() API method.

        The server refuses to delete a DataSet that any annotation still references, and reports which one; there is
        deliberately no cascade here.  To remove a referenced DataSet, delete its annotations first:

            for annotation in client.annotation.annotations.iter_annotations([AnnotationQuery.datasets([id])]):
                client.annotation.annotations.delete_annotation(annotation.id)
            client.annotation.datasets.delete_dataset(id)

        Deleting a DataSet that does not exist is likewise a business error, not a silent success.

        :param dataset_id: Id of the DataSet to delete.
        :return: A DeleteDataSetApiResult with the method response and status information.
        """
        self.logger.info("Starting deleteDataSet operation for id: %s", dataset_id)

        request = self._build_delete_dataset_request(dataset_id)
        result = self._send_delete_dataset(request)

        if result.result_status.is_error:
            self.logger.error("DeleteDataSet operation failed: %s", result.result_status.message)
        else:
            self.logger.info("DeleteDataSet operation completed successfully for id: %s", dataset_id)

        return result
