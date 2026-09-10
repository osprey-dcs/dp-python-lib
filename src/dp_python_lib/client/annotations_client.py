import logging
from collections.abc import Iterator

import grpc

from dp_python_lib.client.query_support import check_at_most_one_text_criterion
from dp_python_lib.client.result import ApiResultBase
from dp_python_lib.client.service_api_client_base import ServiceApiClientBase
from dp_python_lib.grpc import annotation_pb2, annotation_pb2_grpc, common_pb2


def calculations(frames: dict[str, common_pb2.DataFrame]) -> annotation_pb2.Calculations:
    """
    Builds a Calculations payload from named data frames -- the derived values an annotation attaches to its
    DataSets, one frame per time axis.

    Taking a dict rather than a list makes frame-name uniqueness true by construction; the server rejects duplicate
    frame names, and a list would let a caller build one.

    Each frame is a common.DataFrame: a time axis plus the columns sampled on it.  Assemble that message directly
    for now -- the data_frame builders (dp_python_lib.client.data_frame), which validate a frame's internal shape
    (column count against the time axis, unique column names, non-empty names and values) so an error names the
    offending column, arrive in the follow-up PR for issue #6.

    Note the proto's naming trap: the repeated field is 'calculationDataFrames' (singular "calculation") while the
    message it holds is 'CalculationsDataFrame' (plural).

    :param frames: Mapping of frame name to the common.DataFrame carrying that frame's columns.
    :return: An annotation.Calculations carrying one CalculationsDataFrame per entry.
    :raises ValueError: if frames is empty or any frame name is empty.
    """
    if not frames:
        raise ValueError("calculations() requires at least one frame")

    result = annotation_pb2.Calculations()
    for frame_name, frame in frames.items():
        if not frame_name:
            raise ValueError("calculations() requires a non-empty name for every frame")
        calculations_frame = result.calculationDataFrames.add()
        calculations_frame.name = frame_name
        calculations_frame.frame.CopyFrom(frame)
    return result


class AnnotationQuery:
    """
    Factory of lightweight helpers for building QueryAnnotationsRequest.QueryAnnotationsCriterion objects for use
    with AnnotationsClient.query_annotations() and iter_annotations().  Each helper returns a single criterion;
    callers pass a list of criteria to the query methods.

    Criteria AND across the list and OR within a single criterion.  An empty or omitted criteria list matches all
    annotations, so "browse everything, paged" is a legitimate call.

    Example:
        from dp_python_lib.client import AnnotationQuery as AQ
        criteria = [AQ.tags(["reviewed"]), AQ.datasets([dataset_id])]
        result = client.annotation.annotations.query_annotations(criteria)
    """

    _Criterion = annotation_pb2.QueryAnnotationsRequest.QueryAnnotationsCriterion

    @staticmethod
    def ids(values: list[str]) -> "annotation_pb2.QueryAnnotationsRequest.QueryAnnotationsCriterion":
        """
        Builds a criterion matching annotations with any of the specified ids.
        :param values: Annotation ids to match.
        :return: A QueryAnnotationsCriterion with an idCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("ids() requires a non-empty values list")
        criterion = AnnotationQuery._Criterion()
        criterion.idCriterion.ids[:] = values
        return criterion

    @staticmethod
    def owners(values: list[str]) -> "annotation_pb2.QueryAnnotationsRequest.QueryAnnotationsCriterion":
        """
        Builds a criterion matching annotations owned by any of the specified owner ids.
        :param values: Owner ids to match.
        :return: A QueryAnnotationsCriterion with an ownerCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("owners() requires a non-empty values list")
        criterion = AnnotationQuery._Criterion()
        criterion.ownerCriterion.ownerIds[:] = values
        return criterion

    @staticmethod
    def datasets(values: list[str]) -> "annotation_pb2.QueryAnnotationsRequest.QueryAnnotationsCriterion":
        """
        Builds a criterion matching annotations that target any of the specified DataSet ids.

        This is the criterion to use before deleting a DataSet, to find the annotations blocking the delete.

        :param values: DataSet ids to match.
        :return: A QueryAnnotationsCriterion with a dataSetsCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("datasets() requires a non-empty values list")
        criterion = AnnotationQuery._Criterion()
        criterion.dataSetsCriterion.dataSetIds[:] = values
        return criterion

    @staticmethod
    def annotations(values: list[str]) -> "annotation_pb2.QueryAnnotationsRequest.QueryAnnotationsCriterion":
        """
        Builds a criterion matching annotations that reference any of the specified annotation ids.

        These are soft references: deleting an annotation does not clean up incoming links, so a matched
        annotationIds entry may name an annotation that no longer exists.

        :param values: Referenced annotation ids to match.
        :return: A QueryAnnotationsCriterion with an annotationsCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("annotations() requires a non-empty values list")
        criterion = AnnotationQuery._Criterion()
        criterion.annotationsCriterion.annotationIds[:] = values
        return criterion

    @staticmethod
    def name(
        exact: list[str] | None = None,
        prefix: list[str] | None = None,
        contains: list[str] | None = None,
    ) -> "annotation_pb2.QueryAnnotationsRequest.QueryAnnotationsCriterion":
        """
        Builds a criterion matching annotation names by exact value, prefix, and/or substring.
        :param exact: Names to match exactly.
        :param prefix: Name prefixes to match.
        :param contains: Substrings the name must contain.
        :return: A QueryAnnotationsCriterion with a nameCriterion.
        :raises ValueError: if none of exact/prefix/contains is provided and non-empty.
        """
        if not (exact or prefix or contains):
            raise ValueError("name() requires at least one non-empty of exact/prefix/contains")
        criterion = AnnotationQuery._Criterion()
        name_criterion = criterion.nameCriterion
        if exact:
            name_criterion.exact[:] = exact
        if prefix:
            name_criterion.prefix[:] = prefix
        if contains:
            name_criterion.contains[:] = contains
        return criterion

    @staticmethod
    def text(text: str) -> "annotation_pb2.QueryAnnotationsRequest.QueryAnnotationsCriterion":
        """
        Builds a criterion running a full-text search over the annotation's indexed text fields (name and
        description).

        This is a collection-level text index search, not a per-field match; use name() when a match must be
        restricted to the name.  At most ONE text criterion is allowed per request -- two $text clauses cannot be
        ANDed, so the server rejects a second one, and query_annotations() rejects it client-side first.

        :param text: The text to search for.
        :return: A QueryAnnotationsCriterion with a textCriterion.
        :raises ValueError: if text is empty.
        """
        if not text:
            raise ValueError("text() requires a non-empty text value")
        criterion = AnnotationQuery._Criterion()
        criterion.textCriterion.text = text
        return criterion

    @staticmethod
    def tags(values: list[str]) -> "annotation_pb2.QueryAnnotationsRequest.QueryAnnotationsCriterion":
        """
        Builds a criterion matching annotations having any of the specified tags.

        Tags are normalized (lowercased, deduplicated, sorted) when saved, so match against the lowercase form.

        :param values: Tag values to match.
        :return: A QueryAnnotationsCriterion with a tagsCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("tags() requires a non-empty values list")
        criterion = AnnotationQuery._Criterion()
        criterion.tagsCriterion.values[:] = values
        return criterion

    @staticmethod
    def attributes(
        key: str, values: list[str] | None = None
    ) -> "annotation_pb2.QueryAnnotationsRequest.QueryAnnotationsCriterion":
        """
        Builds a criterion matching annotations by attribute key and optional value(s).

        Omitting values (or passing an empty list) performs a key-only existence search: any annotation possessing
        the key matches, whatever its value.  This differs from the older PvMetadataQuery/ConfigurationQuery
        helpers, which require values; relaxing those is issue #40.

        :param key: Attribute key to match (maps to Attribute.name).
        :param values: Attribute values to match for that key, or None for a key-only existence search.
        :return: A QueryAnnotationsCriterion with an attributesCriterion.
        :raises ValueError: if key is empty.
        """
        if not key:
            raise ValueError("attributes() requires a non-empty key")
        criterion = AnnotationQuery._Criterion()
        criterion.attributesCriterion.key = key
        if values:
            criterion.attributesCriterion.values[:] = values
        return criterion


class SaveAnnotationRequestParams:
    """
    Encapsulates client parameters for a call to the saveAnnotation() API method.

    Saving is an id-driven upsert that REPLACES IN FULL, and that includes the calculations: supplying annotation_id
    without calculations CLEARS the stored calculations object and deletes it.  To change an annotation without
    losing its calculations, read the current state with get_annotation() (the only method that returns calculations
    inline) and resend them.  Omitting annotation_id creates a new annotation.
    """

    def __init__(
        self,
        name: str,
        owner_id: str,
        dataset_ids: list[str],
        annotation_ids: list[str] | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        attributes: dict[str, str] | None = None,
        modified_by: str | None = None,
        calculations: annotation_pb2.Calculations | None = None,
        annotation_id: str | None = None,
    ) -> None:
        """
        :param name: Human-readable name of the annotation.  Required by the server.
        :param owner_id: Identifier of the annotation's owner.  Required by the server.
        :param dataset_ids: Ids of the DataSets this annotation targets.  Must be non-empty.
        :param annotation_ids: Ids of other annotations this one references (soft links; may dangle).
        :param description: Human-readable description of the annotation.
        :param tags: List of tags (keywords).  Normalized lowercase/deduplicated/sorted on save.
        :param attributes: Map of key/value attributes describing the annotation.
        :param modified_by: Identifier of the user or process making the change.
        :param calculations: Derived values to store with the annotation (see calculations()).  saveAnnotation() is
            the only write path for calculations, and omitting them on a replace clears the stored object.
        :param annotation_id: Id of an existing annotation to replace in full.  Omit to create a new one.
        :raises ValueError: if name, owner_id, or dataset_ids is empty.
        """
        if not name:
            raise ValueError("SaveAnnotationRequestParams requires a non-empty name")
        if not owner_id:
            raise ValueError("SaveAnnotationRequestParams requires a non-empty owner_id")
        if not dataset_ids:
            raise ValueError("SaveAnnotationRequestParams requires a non-empty dataset_ids list")

        self.name = name
        self.owner_id = owner_id
        self.dataset_ids = dataset_ids
        self.annotation_ids = annotation_ids
        self.description = description
        self.tags = tags
        self.attributes = attributes
        self.modified_by = modified_by
        self.calculations = calculations
        self.annotation_id = annotation_id


class SaveAnnotationApiResult(ApiResultBase):
    """
    Wraps the response from saveAnnotation(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.SaveAnnotationResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The SaveAnnotationResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def annotation_id(self) -> str | None:
        """Id of the annotation that was saved, or None on error."""
        if self.response is not None and self.response.HasField("saveAnnotationResult"):
            return self.response.saveAnnotationResult.annotationId
        return None

    @property
    def calculations_id(self) -> str | None:
        """
        Id of the calculations object stored with the annotation, or None on error.

        This is empty string -- not None -- when the request carried no calculations; None means the call failed.
        A replace that changes the calculations returns a NEW id, and the previous object is deleted.
        """
        if self.response is not None and self.response.HasField("saveAnnotationResult"):
            return self.response.saveAnnotationResult.calculationsId
        return None


class GetAnnotationApiResult(ApiResultBase):
    """
    Wraps the response from getAnnotation(), with a status object including an error flag and message.

    This is the only method that returns an annotation's calculations inline; queryAnnotations() results carry the
    calculationsId but leave the content empty.  An annotation that does not exist is a business error.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.GetAnnotationResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The GetAnnotationResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def annotation(self) -> annotation_pb2.Annotation | None:
        """The requested Annotation, or None on error."""
        if self.response is not None and self.response.HasField("getAnnotationResult"):
            return self.response.getAnnotationResult.annotation
        return None

    @property
    def calculations(self) -> annotation_pb2.Calculations | None:
        """
        The annotation's inline Calculations, or None on error or when the annotation has none.

        A calculationsId that resolves to nothing is a server-side error (data corruption), never an empty success,
        so an annotation reported here as having no calculations genuinely has none.
        """
        annotation = self.annotation
        if annotation is not None and annotation.HasField("calculations"):
            return annotation.calculations
        return None


class QueryAnnotationsApiResult(ApiResultBase):
    """
    Wraps a single page of the response from queryAnnotations(), with a status object including an error flag and
    message.  Use AnnotationsClient.iter_annotations() to transparently page through all results.

    Query results carry ids rather than content: an annotation's calculationsId is populated but its calculations
    are not.  Fetch them with get_annotation() or get_calculations().
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.QueryAnnotationsResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The QueryAnnotationsResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def annotations(self) -> list[annotation_pb2.Annotation]:
        """The Annotation records in this page, or an empty list on error."""
        if self.response is not None and self.response.HasField("annotationsResult"):
            return list(self.response.annotationsResult.annotations)
        return []

    @property
    def next_page_token(self) -> str:
        """Token for retrieving the next page, or empty string if there are no more pages."""
        if self.response is not None and self.response.HasField("annotationsResult"):
            return self.response.annotationsResult.nextPageToken
        return ""


class DeleteAnnotationApiResult(ApiResultBase):
    """
    Wraps the response from deleteAnnotation(), with a status object including an error flag and message.

    Deleting an annotation that does not exist is a business error, not a silent success.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.DeleteAnnotationResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The DeleteAnnotationResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def annotation_id(self) -> str | None:
        """Id of the annotation that was deleted, or None on error."""
        if self.response is not None and self.response.HasField("deleteAnnotationResult"):
            return self.response.deleteAnnotationResult.annotationId
        return None


class GetCalculationsApiResult(ApiResultBase):
    """
    Wraps the response from getCalculations(), with a status object including an error flag and message.

    This is the click-through path from a query result: queryAnnotations() gives you a calculationsId, and this
    fetches the content it names without re-fetching the whole annotation.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.GetCalculationsResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The GetCalculationsResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def calculations(self) -> annotation_pb2.Calculations | None:
        """The requested Calculations, or None on error."""
        if self.response is not None and self.response.HasField("getCalculationsResult"):
            return self.response.getCalculationsResult.calculations
        return None


class AnnotationsClient(ServiceApiClientBase):
    """
    User-facing client for the annotation methods of the MLDP Annotation Service.  An annotation describes one or
    more DataSets -- a name, a description, tags, and optionally a Calculations payload of derived values with
    column-level provenance.

    Provides low-level wrappers for saveAnnotation(), getAnnotation(), queryAnnotations(), deleteAnnotation(), and
    getCalculations(), plus the iter_annotations() paging iterator.

    Note this is AnnotationsClient (plural), the feature client, as distinct from AnnotationClient (singular), the
    facade that owns the Annotation Service channel and exposes this client as client.annotation.annotations.  Users
    reach this through the facade and never construct either directly.

    patchAnnotation() is not wrapped: it is a reserved placeholder that returns "not implemented".
    """

    def __init__(self, channel: grpc.Channel) -> None:
        """
        :param channel: gRPC communication channel for the Annotation Service.
        """
        super().__init__(channel, annotation_pb2_grpc.DpAnnotationServiceStub)
        self.logger = logging.getLogger(__name__)
        self.logger.debug("AnnotationsClient initialized with channel: %s", channel)

    # ------------------------------------------------------------------
    # saveAnnotation
    # ------------------------------------------------------------------

    def _build_save_annotation_request(
        self, request_params: SaveAnnotationRequestParams
    ) -> annotation_pb2.SaveAnnotationRequest:
        """
        Builds a SaveAnnotationRequest from the supplied SaveAnnotationRequestParams.
        :param request_params: User parameters for the call to saveAnnotation().
        :return: A SaveAnnotationRequest for the specified params.
        """
        self.logger.debug("Building SaveAnnotationRequest for annotation: %s", request_params.name)

        request = annotation_pb2.SaveAnnotationRequest()
        request.name = request_params.name
        request.ownerId = request_params.owner_id

        if request_params.annotation_id:
            request.id = request_params.annotation_id

        if request_params.dataset_ids:
            request.dataSetIds[:] = request_params.dataset_ids

        if request_params.annotation_ids:
            request.annotationIds[:] = request_params.annotation_ids

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

        if request_params.calculations is not None:
            request.calculations.CopyFrom(request_params.calculations)

        self.logger.debug("SaveAnnotationRequest built successfully")
        return request

    def _send_save_annotation(self, request: annotation_pb2.SaveAnnotationRequest) -> SaveAnnotationApiResult:
        """
        Invokes the saveAnnotation() API method with the supplied request.
        :param request: SaveAnnotationRequest with parameters for the call.
        :return: A SaveAnnotationApiResult with the method response and status information.
        """
        return self._dispatch(
            self._stub.saveAnnotation,
            request,
            SaveAnnotationApiResult,
            "saveAnnotationResult",
            "saveAnnotation",
            request_log=lambda: self.logger.info(
                "Calling saveAnnotation API for annotation: %s targeting %d dataset(s)",
                request.name,
                len(request.dataSetIds),
            ),
            success_log=lambda response: self.logger.info(
                "Successfully saved annotation: %s with id: %s",
                request.name,
                response.saveAnnotationResult.annotationId,
            ),
        )

    def save_annotation(self, request_params: SaveAnnotationRequestParams) -> SaveAnnotationApiResult:
        """
        User-facing method for invoking the saveAnnotation() API method.

        Saving replaces in full when request_params.annotation_id is set, and that INCLUDES the calculations:
        omitting them clears and deletes the stored object.  Read with get_annotation() and resend to preserve them.

        :param request_params: Contains user parameters for the call to saveAnnotation().
        :return: A SaveAnnotationApiResult with the method response and status information.
        """
        self.logger.info("Starting saveAnnotation operation for annotation: %s", request_params.name)

        request = self._build_save_annotation_request(request_params)
        result = self._send_save_annotation(request)

        if result.result_status.is_error:
            self.logger.error("SaveAnnotation operation failed: %s", result.result_status.message)
        else:
            self.logger.info("SaveAnnotation operation completed successfully for: %s", request_params.name)

        return result

    # ------------------------------------------------------------------
    # getAnnotation
    # ------------------------------------------------------------------

    def _build_get_annotation_request(self, annotation_id: str) -> annotation_pb2.GetAnnotationRequest:
        """
        Builds a GetAnnotationRequest for the supplied annotation id.
        :param annotation_id: Id of the annotation to retrieve.
        :return: A GetAnnotationRequest for the specified id.
        """
        self.logger.debug("Building GetAnnotationRequest for id: %s", annotation_id)
        request = annotation_pb2.GetAnnotationRequest()
        request.annotationId = annotation_id
        return request

    def _send_get_annotation(self, request: annotation_pb2.GetAnnotationRequest) -> GetAnnotationApiResult:
        """
        Invokes the getAnnotation() API method with the supplied request.
        :param request: GetAnnotationRequest with parameters for the call.
        :return: A GetAnnotationApiResult with the method response and status information.
        """
        return self._dispatch(
            self._stub.getAnnotation,
            request,
            GetAnnotationApiResult,
            "getAnnotationResult",
            "getAnnotation",
            request_log=lambda: self.logger.info("Calling getAnnotation API for id: %s", request.annotationId),
            success_log=lambda _response: self.logger.info(
                "Successfully retrieved annotation for id: %s", request.annotationId
            ),
        )

    def get_annotation(self, annotation_id: str) -> GetAnnotationApiResult:
        """
        User-facing method for invoking the getAnnotation() API method.

        This is the only method that returns an annotation's calculations inline.  An annotation that does not exist
        comes back as a business error, not an empty success.

        :param annotation_id: Id of the annotation to retrieve.
        :return: A GetAnnotationApiResult with the method response and status information.
        """
        self.logger.info("Starting getAnnotation operation for id: %s", annotation_id)

        request = self._build_get_annotation_request(annotation_id)
        result = self._send_get_annotation(request)

        if result.result_status.is_error:
            self.logger.error("GetAnnotation operation failed: %s", result.result_status.message)
        else:
            self.logger.info("GetAnnotation operation completed successfully for id: %s", annotation_id)

        return result

    # ------------------------------------------------------------------
    # queryAnnotations
    # ------------------------------------------------------------------

    def _build_query_annotations_request(
        self,
        criteria: list[annotation_pb2.QueryAnnotationsRequest.QueryAnnotationsCriterion] | None = None,
        limit: int | None = None,
        page_token: str | None = None,
    ) -> annotation_pb2.QueryAnnotationsRequest:
        """
        Builds a QueryAnnotationsRequest from the supplied criteria and paging parameters.
        :param criteria: List of QueryAnnotationsCriterion objects (see AnnotationQuery helpers), or None to
            match all.
        :param limit: Maximum number of records to return per page (optional).
        :param page_token: Token for retrieving a subsequent page (optional).
        :return: A QueryAnnotationsRequest for the specified params.
        """
        self.logger.debug("Building QueryAnnotationsRequest with %d criteria", len(criteria) if criteria else 0)
        request = annotation_pb2.QueryAnnotationsRequest()
        if criteria:
            request.criteria.extend(criteria)
        if limit is not None:
            request.limit = limit
        if page_token:
            request.pageToken = page_token
        return request

    def _send_query_annotations(self, request: annotation_pb2.QueryAnnotationsRequest) -> QueryAnnotationsApiResult:
        """
        Invokes the queryAnnotations() API method with the supplied request.
        :param request: QueryAnnotationsRequest with parameters for the call.
        :return: A QueryAnnotationsApiResult with the method response and status information.
        """
        return self._dispatch(
            self._stub.queryAnnotations,
            request,
            QueryAnnotationsApiResult,
            "annotationsResult",
            "queryAnnotations",
            request_log=lambda: self.logger.info(
                "Calling queryAnnotations API with %d criteria", len(request.criteria)
            ),
            success_log=lambda response: self.logger.info(
                "QueryAnnotations returned %d records", len(response.annotationsResult.annotations)
            ),
        )

    def query_annotations(
        self,
        criteria: list[annotation_pb2.QueryAnnotationsRequest.QueryAnnotationsCriterion] | None = None,
        limit: int | None = None,
        page_token: str | None = None,
    ) -> QueryAnnotationsApiResult:
        """
        User-facing method for invoking the queryAnnotations() API method.  Returns a single page of results; use
        iter_annotations() to page through all results transparently.

        An omitted or empty criteria list matches all annotations.  Results carry ids rather than content: the
        calculationsId is populated but the calculations themselves are not.

        :param criteria: List of QueryAnnotationsCriterion objects (see AnnotationQuery helpers), or None to
            match all.
        :param limit: Maximum number of records to return PER PAGE -- not a cap on the total (optional).
        :param page_token: Token for retrieving a subsequent page (optional).
        :return: A QueryAnnotationsApiResult with a single page of results and status information.
        :raises ValueError: if criteria contains more than one text criterion.
        """
        criteria = criteria or []
        check_at_most_one_text_criterion(criteria, "query_annotations()")

        self.logger.info("Starting queryAnnotations operation with %d criteria", len(criteria))

        request = self._build_query_annotations_request(criteria, limit=limit, page_token=page_token)
        result = self._send_query_annotations(request)

        if result.result_status.is_error:
            self.logger.error("QueryAnnotations operation failed: %s", result.result_status.message)
        else:
            self.logger.info("QueryAnnotations operation completed successfully")

        return result

    def iter_annotations(
        self,
        criteria: list[annotation_pb2.QueryAnnotationsRequest.QueryAnnotationsCriterion] | None = None,
        limit: int | None = None,
    ) -> Iterator[annotation_pb2.Annotation]:
        """
        Convenience generator that transparently pages through all queryAnnotations() results, following the
        nextPageToken until the results are exhausted.  Yields individual Annotation records.

        Raises RuntimeError if any page returns an error, so callers can distinguish failure from an empty result set.

        :param criteria: List of QueryAnnotationsCriterion objects (see AnnotationQuery helpers), or None to
            match all.
        :param limit: Maximum number of records to return per page (optional).
        :return: An iterator over all matching Annotation records across all pages.
        :raises ValueError: if criteria contains more than one text criterion.
        :raises RuntimeError: if any page returns an error.
        """
        page_token: str | None = None
        while True:
            result = self.query_annotations(criteria, limit=limit, page_token=page_token)
            if result.result_status.is_error:
                raise RuntimeError(f"queryAnnotations failed during paging: {result.result_status.message}")

            yield from result.annotations

            page_token = result.next_page_token
            if not page_token:
                break

    # ------------------------------------------------------------------
    # deleteAnnotation
    # ------------------------------------------------------------------

    def _build_delete_annotation_request(self, annotation_id: str) -> annotation_pb2.DeleteAnnotationRequest:
        """
        Builds a DeleteAnnotationRequest for the supplied annotation id.
        :param annotation_id: Id of the annotation to delete.
        :return: A DeleteAnnotationRequest for the specified id.
        """
        self.logger.debug("Building DeleteAnnotationRequest for id: %s", annotation_id)
        request = annotation_pb2.DeleteAnnotationRequest()
        request.annotationId = annotation_id
        return request

    def _send_delete_annotation(self, request: annotation_pb2.DeleteAnnotationRequest) -> DeleteAnnotationApiResult:
        """
        Invokes the deleteAnnotation() API method with the supplied request.
        :param request: DeleteAnnotationRequest with parameters for the call.
        :return: A DeleteAnnotationApiResult with the method response and status information.
        """
        return self._dispatch(
            self._stub.deleteAnnotation,
            request,
            DeleteAnnotationApiResult,
            "deleteAnnotationResult",
            "deleteAnnotation",
            request_log=lambda: self.logger.info("Calling deleteAnnotation API for id: %s", request.annotationId),
            success_log=lambda _response: self.logger.info(
                "Successfully deleted annotation id: %s", request.annotationId
            ),
        )

    def delete_annotation(self, annotation_id: str) -> DeleteAnnotationApiResult:
        """
        User-facing method for invoking the deleteAnnotation() API method.

        The delete cascades to the annotation's calculations.  It does NOT clean up incoming soft references: other
        annotations' annotationIds entries, and derivedFrom provenance links naming this annotation's calculations,
        are left dangling for readers to tolerate.

        Deleting an annotation that does not exist is a business error, not a silent success.

        :param annotation_id: Id of the annotation to delete.
        :return: A DeleteAnnotationApiResult with the method response and status information.
        """
        self.logger.info("Starting deleteAnnotation operation for id: %s", annotation_id)

        request = self._build_delete_annotation_request(annotation_id)
        result = self._send_delete_annotation(request)

        if result.result_status.is_error:
            self.logger.error("DeleteAnnotation operation failed: %s", result.result_status.message)
        else:
            self.logger.info("DeleteAnnotation operation completed successfully for id: %s", annotation_id)

        return result

    # ------------------------------------------------------------------
    # getCalculations
    # ------------------------------------------------------------------

    def _build_get_calculations_request(self, calculations_id: str) -> annotation_pb2.GetCalculationsRequest:
        """
        Builds a GetCalculationsRequest for the supplied calculations id.
        :param calculations_id: Id of the calculations object to retrieve.
        :return: A GetCalculationsRequest for the specified id.
        """
        self.logger.debug("Building GetCalculationsRequest for id: %s", calculations_id)
        request = annotation_pb2.GetCalculationsRequest()
        request.calculationsId = calculations_id
        return request

    def _send_get_calculations(self, request: annotation_pb2.GetCalculationsRequest) -> GetCalculationsApiResult:
        """
        Invokes the getCalculations() API method with the supplied request.
        :param request: GetCalculationsRequest with parameters for the call.
        :return: A GetCalculationsApiResult with the method response and status information.
        """
        return self._dispatch(
            self._stub.getCalculations,
            request,
            GetCalculationsApiResult,
            "getCalculationsResult",
            "getCalculations",
            request_log=lambda: self.logger.info("Calling getCalculations API for id: %s", request.calculationsId),
            success_log=lambda response: self.logger.info(
                "Successfully retrieved calculations id: %s with %d frame(s)",
                request.calculationsId,
                len(response.getCalculationsResult.calculations.calculationDataFrames),
            ),
        )

    def get_calculations(self, calculations_id: str) -> GetCalculationsApiResult:
        """
        User-facing method for invoking the getCalculations() API method.

        This is the click-through path from a queryAnnotations() result, which carries the calculationsId but not the
        content.  Calculations that do not exist come back as a business error.

        :param calculations_id: Id of the calculations object to retrieve.
        :return: A GetCalculationsApiResult with the method response and status information.
        """
        self.logger.info("Starting getCalculations operation for id: %s", calculations_id)

        request = self._build_get_calculations_request(calculations_id)
        result = self._send_get_calculations(request)

        if result.result_status.is_error:
            self.logger.error("GetCalculations operation failed: %s", result.result_status.message)
        else:
            self.logger.info("GetCalculations operation completed successfully for id: %s", calculations_id)

        return result
