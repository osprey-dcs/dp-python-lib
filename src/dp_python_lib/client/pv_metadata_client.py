import logging
from collections.abc import Iterator

import grpc

from dp_python_lib.client.result import ApiResultBase
from dp_python_lib.client.service_api_client_base import ServiceApiClientBase
from dp_python_lib.grpc import annotation_pb2, annotation_pb2_grpc, common_pb2


class PvMetadataQuery:
    """
    Factory of lightweight helpers for building QueryPvMetadataRequest.QueryPvMetadataCriterion objects for use with
    PvMetadataClient.query_pv_metadata() and iter_pv_metadata().  Each helper returns a single criterion; callers pass
    a list of criteria to the query methods.

    Example:
        from dp_python_lib.client import PvMetadataQuery as Q
        criteria = [Q.pv_name(prefix=["ABC:"]), Q.tags(["vacuum"])]
        result = client.annotation.pv_metadata.query_pv_metadata(criteria=criteria)
    """

    _Criterion = annotation_pb2.QueryPvMetadataRequest.QueryPvMetadataCriterion

    @staticmethod
    def pv_name(
        exact: list[str] | None = None,
        prefix: list[str] | None = None,
        contains: list[str] | None = None,
    ) -> "annotation_pb2.QueryPvMetadataRequest.QueryPvMetadataCriterion":
        """
        Builds a criterion matching PV names by exact value, prefix, and/or substring.
        :param exact: PV names to match exactly.
        :param prefix: PV name prefixes to match.
        :param contains: Substrings the PV name must contain.
        :return: A QueryPvMetadataCriterion with a pvNameCriterion.
        :raises ValueError: if none of exact/prefix/contains is provided and non-empty.
        """
        if not (exact or prefix or contains):
            raise ValueError("pv_name() requires at least one non-empty of exact/prefix/contains")
        criterion = PvMetadataQuery._Criterion()
        name_criterion = criterion.pvNameCriterion
        if exact:
            name_criterion.exact[:] = exact
        if prefix:
            name_criterion.prefix[:] = prefix
        if contains:
            name_criterion.contains[:] = contains
        return criterion

    @staticmethod
    def aliases(
        exact: list[str] | None = None,
        prefix: list[str] | None = None,
        contains: list[str] | None = None,
    ) -> "annotation_pb2.QueryPvMetadataRequest.QueryPvMetadataCriterion":
        """
        Builds a criterion matching PV aliases by exact value, prefix, and/or substring.
        :param exact: Aliases to match exactly.
        :param prefix: Alias prefixes to match.
        :param contains: Substrings the alias must contain.
        :return: A QueryPvMetadataCriterion with an aliasesCriterion.
        :raises ValueError: if none of exact/prefix/contains is provided and non-empty.
        """
        if not (exact or prefix or contains):
            raise ValueError("aliases() requires at least one non-empty of exact/prefix/contains")
        criterion = PvMetadataQuery._Criterion()
        aliases_criterion = criterion.aliasesCriterion
        if exact:
            aliases_criterion.exact[:] = exact
        if prefix:
            aliases_criterion.prefix[:] = prefix
        if contains:
            aliases_criterion.contains[:] = contains
        return criterion

    @staticmethod
    def tags(values: list[str]) -> "annotation_pb2.QueryPvMetadataRequest.QueryPvMetadataCriterion":
        """
        Builds a criterion matching PVs having any of the specified tags.
        :param values: Tag values to match.
        :return: A QueryPvMetadataCriterion with a tagsCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("tags() requires a non-empty values list")
        criterion = PvMetadataQuery._Criterion()
        criterion.tagsCriterion.values[:] = values
        return criterion

    @staticmethod
    def attributes(key: str, values: list[str]) -> "annotation_pb2.QueryPvMetadataRequest.QueryPvMetadataCriterion":
        """
        Builds a criterion matching PVs whose attribute with the given key has any of the specified values.
        :param key: Attribute key to match.
        :param values: Attribute values to match for that key.
        :return: A QueryPvMetadataCriterion with an attributesCriterion.
        :raises ValueError: if key is empty or values is empty.
        """
        if not key:
            raise ValueError("attributes() requires a non-empty key")
        if not values:
            raise ValueError("attributes() requires a non-empty values list")
        criterion = PvMetadataQuery._Criterion()
        criterion.attributesCriterion.key = key
        criterion.attributesCriterion.values[:] = values
        return criterion


class SavePvMetadataRequestParams:
    """
    Encapsulates client parameters for a call to the savePvMetadata() API method.
    """

    def __init__(
        self,
        pv_name: str,
        aliases: list[str] | None = None,
        tags: list[str] | None = None,
        attributes: dict[str, str] | None = None,
        modified_by: str | None = None,
        description: str | None = None,
    ) -> None:
        """
        :param pv_name: Name of the PV whose metadata is being saved.
        :param aliases: Alternate names for the PV.
        :param tags: List of tags (keywords) describing the PV.
        :param attributes: Map of key/value attributes describing the PV.
        :param modified_by: Identifier of the user or process making the change.
        :param description: Human-readable description of the PV.
        """
        self.pv_name = pv_name
        self.aliases = aliases
        self.tags = tags
        self.attributes = attributes
        self.modified_by = modified_by
        self.description = description


class SavePvMetadataApiResult(ApiResultBase):
    """
    Wraps the response from savePvMetadata(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.SavePvMetadataResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The SavePvMetadataResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def pv_name(self) -> str | None:
        """Name of the PV whose metadata was saved, or None on error."""
        if self.response is not None and self.response.HasField("savePvMetadataResult"):
            return self.response.savePvMetadataResult.pvName
        return None


class GetPvMetadataApiResult(ApiResultBase):
    """
    Wraps the response from getPvMetadata(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.GetPvMetadataResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The GetPvMetadataResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def pv_metadata(self) -> common_pb2.PvMetadata | None:
        """The PvMetadata for the requested PV, or None on error."""
        if self.response is not None and self.response.HasField("getPvMetadataResult"):
            return self.response.getPvMetadataResult.pvMetadata
        return None


class QueryPvMetadataApiResult(ApiResultBase):
    """
    Wraps a single page of the response from queryPvMetadata(), with a status object including an error flag and
    message.  Use PvMetadataClient.iter_pv_metadata() to transparently page through all results.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.QueryPvMetadataResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The QueryPvMetadataResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def pv_metadata_list(self) -> list[common_pb2.PvMetadata]:
        """The PvMetadata records in this page, or an empty list on error."""
        if self.response is not None and self.response.HasField("pvMetadataResult"):
            return list(self.response.pvMetadataResult.pvMetadata)
        return []

    @property
    def next_page_token(self) -> str:
        """Token for retrieving the next page, or empty string if there are no more pages."""
        if self.response is not None and self.response.HasField("pvMetadataResult"):
            return self.response.pvMetadataResult.nextPageToken
        return ""


class DeletePvMetadataApiResult(ApiResultBase):
    """
    Wraps the response from deletePvMetadata(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.DeletePvMetadataResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The DeletePvMetadataResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def pv_name(self) -> str | None:
        """Name of the PV whose metadata was deleted, or None on error."""
        if self.response is not None and self.response.HasField("deletePvMetadataResult"):
            return self.response.deletePvMetadataResult.pvName
        return None


class PvMetadataClient(ServiceApiClientBase):
    """
    User-facing client for the PV metadata methods of the MLDP Annotation Service.  Provides low-level wrappers for
    savePvMetadata(), getPvMetadata(), queryPvMetadata(), and deletePvMetadata(), plus conveniences such as
    dict/list inputs, name-or-alias lookups, and an iter_pv_metadata() paging iterator.
    """

    def __init__(self, channel: grpc.Channel) -> None:
        """
        :param channel: gRPC communication channel for the Annotation Service.
        """
        super().__init__(channel, annotation_pb2_grpc.DpAnnotationServiceStub)
        self.logger = logging.getLogger(__name__)
        self.logger.debug("PvMetadataClient initialized with channel: %s", channel)

    # ------------------------------------------------------------------
    # savePvMetadata
    # ------------------------------------------------------------------

    def _build_save_pv_metadata_request(
        self, request_params: SavePvMetadataRequestParams
    ) -> annotation_pb2.SavePvMetadataRequest:
        """
        Builds a SavePvMetadataRequest from the supplied SavePvMetadataRequestParams.
        :param request_params: User parameters for the call to savePvMetadata().
        :return: A SavePvMetadataRequest for the specified params.
        """
        self.logger.debug("Building SavePvMetadataRequest for PV: %s", request_params.pv_name)

        request = annotation_pb2.SavePvMetadataRequest()
        request.pvName = request_params.pv_name

        if request_params.aliases:
            request.aliases[:] = request_params.aliases

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

        if request_params.description:
            request.description = request_params.description

        self.logger.debug("SavePvMetadataRequest built successfully")
        return request

    def _send_save_pv_metadata(self, request: annotation_pb2.SavePvMetadataRequest) -> SavePvMetadataApiResult:
        """
        Invokes the savePvMetadata() API method with the supplied request.
        :param request: SavePvMetadataRequest with parameters for the call.
        :return: A SavePvMetadataApiResult with the method response and status information.
        """
        self.logger.info("Calling savePvMetadata API for PV: %s", request.pvName)

        try:
            self.logger.debug("Invoking stub.savePvMetadata with request")
            response = self._stub.savePvMetadata(request)
            self.logger.debug("Received response from savePvMetadata API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning("SavePvMetadata API returned business error: %s", error_msg)
                return SavePvMetadataApiResult(is_error=True, message=error_msg)

            elif response.HasField("savePvMetadataResult"):
                self.logger.info("Successfully saved PV metadata for: %s", request.pvName)
                return SavePvMetadataApiResult(is_error=False, message="", response=response)

            else:
                error_msg = "Unexpected response format: neither exceptionalResult nor savePvMetadataResult found"
                self.logger.error(error_msg)
                return SavePvMetadataApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during savePvMetadata: %s", e.details())
            return SavePvMetadataApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {e!s}"
            self.logger.error("Unexpected error during savePvMetadata: %s", str(e), exc_info=True)
            return SavePvMetadataApiResult(is_error=True, message=error_msg)

    def save_pv_metadata(self, request_params: SavePvMetadataRequestParams) -> SavePvMetadataApiResult:
        """
        User-facing method for invoking the savePvMetadata() API method.
        :param request_params: Contains user parameters for the call to savePvMetadata().
        :return: A SavePvMetadataApiResult with the method response and status information.
        """
        self.logger.info("Starting savePvMetadata operation for PV: %s", request_params.pv_name)

        request = self._build_save_pv_metadata_request(request_params)
        result = self._send_save_pv_metadata(request)

        if result.result_status.is_error:
            self.logger.error("SavePvMetadata operation failed: %s", result.result_status.message)
        else:
            self.logger.info("SavePvMetadata operation completed successfully for PV: %s", request_params.pv_name)

        return result

    # ------------------------------------------------------------------
    # getPvMetadata
    # ------------------------------------------------------------------

    def _build_get_pv_metadata_request(self, pv_name_or_alias: str) -> annotation_pb2.GetPvMetadataRequest:
        """
        Builds a GetPvMetadataRequest for the supplied PV name or alias.
        :param pv_name_or_alias: PV name or alias to look up.
        :return: A GetPvMetadataRequest for the specified name or alias.
        """
        self.logger.debug("Building GetPvMetadataRequest for: %s", pv_name_or_alias)
        request = annotation_pb2.GetPvMetadataRequest()
        request.pvNameOrAlias = pv_name_or_alias
        return request

    def _send_get_pv_metadata(self, request: annotation_pb2.GetPvMetadataRequest) -> GetPvMetadataApiResult:
        """
        Invokes the getPvMetadata() API method with the supplied request.
        :param request: GetPvMetadataRequest with parameters for the call.
        :return: A GetPvMetadataApiResult with the method response and status information.
        """
        self.logger.info("Calling getPvMetadata API for: %s", request.pvNameOrAlias)

        try:
            self.logger.debug("Invoking stub.getPvMetadata with request")
            response = self._stub.getPvMetadata(request)
            self.logger.debug("Received response from getPvMetadata API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning("GetPvMetadata API returned business error: %s", error_msg)
                return GetPvMetadataApiResult(is_error=True, message=error_msg)

            elif response.HasField("getPvMetadataResult"):
                self.logger.info("Successfully retrieved PV metadata for: %s", request.pvNameOrAlias)
                return GetPvMetadataApiResult(is_error=False, message="", response=response)

            else:
                error_msg = "Unexpected response format: neither exceptionalResult nor getPvMetadataResult found"
                self.logger.error(error_msg)
                return GetPvMetadataApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during getPvMetadata: %s", e.details())
            return GetPvMetadataApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {e!s}"
            self.logger.error("Unexpected error during getPvMetadata: %s", str(e), exc_info=True)
            return GetPvMetadataApiResult(is_error=True, message=error_msg)

    def get_pv_metadata(self, pv_name_or_alias: str) -> GetPvMetadataApiResult:
        """
        User-facing method for invoking the getPvMetadata() API method.
        :param pv_name_or_alias: PV name or alias to look up.
        :return: A GetPvMetadataApiResult with the method response and status information.
        """
        self.logger.info("Starting getPvMetadata operation for: %s", pv_name_or_alias)

        request = self._build_get_pv_metadata_request(pv_name_or_alias)
        result = self._send_get_pv_metadata(request)

        if result.result_status.is_error:
            self.logger.error("GetPvMetadata operation failed: %s", result.result_status.message)
        else:
            self.logger.info("GetPvMetadata operation completed successfully for: %s", pv_name_or_alias)

        return result

    # ------------------------------------------------------------------
    # queryPvMetadata
    # ------------------------------------------------------------------

    def _build_query_pv_metadata_request(
        self,
        criteria: list[annotation_pb2.QueryPvMetadataRequest.QueryPvMetadataCriterion],
        limit: int | None = None,
        page_token: str | None = None,
    ) -> annotation_pb2.QueryPvMetadataRequest:
        """
        Builds a QueryPvMetadataRequest from the supplied criteria and paging parameters.
        :param criteria: List of QueryPvMetadataCriterion objects (see PvMetadataQuery helpers).
        :param limit: Maximum number of records to return per page (optional).
        :param page_token: Token for retrieving a subsequent page (optional).
        :return: A QueryPvMetadataRequest for the specified params.
        """
        self.logger.debug("Building QueryPvMetadataRequest with %d criteria", len(criteria))
        request = annotation_pb2.QueryPvMetadataRequest()
        request.criteria.extend(criteria)
        if limit is not None:
            request.limit = limit
        if page_token:
            request.pageToken = page_token
        return request

    def _send_query_pv_metadata(self, request: annotation_pb2.QueryPvMetadataRequest) -> QueryPvMetadataApiResult:
        """
        Invokes the queryPvMetadata() API method with the supplied request.
        :param request: QueryPvMetadataRequest with parameters for the call.
        :return: A QueryPvMetadataApiResult with the method response and status information.
        """
        self.logger.info("Calling queryPvMetadata API with %d criteria", len(request.criteria))

        try:
            self.logger.debug("Invoking stub.queryPvMetadata with request")
            response = self._stub.queryPvMetadata(request)
            self.logger.debug("Received response from queryPvMetadata API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning("QueryPvMetadata API returned business error: %s", error_msg)
                return QueryPvMetadataApiResult(is_error=True, message=error_msg)

            elif response.HasField("pvMetadataResult"):
                self.logger.info("QueryPvMetadata returned %d records", len(response.pvMetadataResult.pvMetadata))
                return QueryPvMetadataApiResult(is_error=False, message="", response=response)

            else:
                error_msg = "Unexpected response format: neither exceptionalResult nor pvMetadataResult found"
                self.logger.error(error_msg)
                return QueryPvMetadataApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during queryPvMetadata: %s", e.details())
            return QueryPvMetadataApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {e!s}"
            self.logger.error("Unexpected error during queryPvMetadata: %s", str(e), exc_info=True)
            return QueryPvMetadataApiResult(is_error=True, message=error_msg)

    def query_pv_metadata(
        self,
        criteria: list[annotation_pb2.QueryPvMetadataRequest.QueryPvMetadataCriterion],
        limit: int | None = None,
        page_token: str | None = None,
    ) -> QueryPvMetadataApiResult:
        """
        User-facing method for invoking the queryPvMetadata() API method.  Returns a single page of results; use
        iter_pv_metadata() to page through all results transparently.
        :param criteria: List of QueryPvMetadataCriterion objects (see PvMetadataQuery helpers).
        :param limit: Maximum number of records to return per page (optional).
        :param page_token: Token for retrieving a subsequent page (optional).
        :return: A QueryPvMetadataApiResult with a single page of results and status information.
        """
        self.logger.info("Starting queryPvMetadata operation with %d criteria", len(criteria))

        request = self._build_query_pv_metadata_request(criteria, limit=limit, page_token=page_token)
        result = self._send_query_pv_metadata(request)

        if result.result_status.is_error:
            self.logger.error("QueryPvMetadata operation failed: %s", result.result_status.message)
        else:
            self.logger.info("QueryPvMetadata operation completed successfully")

        return result

    def iter_pv_metadata(
        self,
        criteria: list[annotation_pb2.QueryPvMetadataRequest.QueryPvMetadataCriterion],
        limit: int | None = None,
    ) -> Iterator[common_pb2.PvMetadata]:
        """
        Convenience generator that transparently pages through all queryPvMetadata() results, following the
        nextPageToken until the results are exhausted.  Yields individual PvMetadata records.

        Raises RuntimeError if any page returns an error, so callers can distinguish failure from an empty result set.

        :param criteria: List of QueryPvMetadataCriterion objects (see PvMetadataQuery helpers).
        :param limit: Maximum number of records to return per page (optional).
        :return: An iterator over all matching PvMetadata records across all pages.
        """
        page_token: str | None = None
        while True:
            result = self.query_pv_metadata(criteria, limit=limit, page_token=page_token)
            if result.result_status.is_error:
                raise RuntimeError(f"queryPvMetadata failed during paging: {result.result_status.message}")

            for pv_metadata in result.pv_metadata_list:
                yield pv_metadata

            page_token = result.next_page_token
            if not page_token:
                break

    # ------------------------------------------------------------------
    # deletePvMetadata
    # ------------------------------------------------------------------

    def _build_delete_pv_metadata_request(self, pv_name_or_alias: str) -> annotation_pb2.DeletePvMetadataRequest:
        """
        Builds a DeletePvMetadataRequest for the supplied PV name or alias.
        :param pv_name_or_alias: PV name or alias to delete.
        :return: A DeletePvMetadataRequest for the specified name or alias.
        """
        self.logger.debug("Building DeletePvMetadataRequest for: %s", pv_name_or_alias)
        request = annotation_pb2.DeletePvMetadataRequest()
        request.pvNameOrAlias = pv_name_or_alias
        return request

    def _send_delete_pv_metadata(self, request: annotation_pb2.DeletePvMetadataRequest) -> DeletePvMetadataApiResult:
        """
        Invokes the deletePvMetadata() API method with the supplied request.
        :param request: DeletePvMetadataRequest with parameters for the call.
        :return: A DeletePvMetadataApiResult with the method response and status information.
        """
        self.logger.info("Calling deletePvMetadata API for: %s", request.pvNameOrAlias)

        try:
            self.logger.debug("Invoking stub.deletePvMetadata with request")
            response = self._stub.deletePvMetadata(request)
            self.logger.debug("Received response from deletePvMetadata API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning("DeletePvMetadata API returned business error: %s", error_msg)
                return DeletePvMetadataApiResult(is_error=True, message=error_msg)

            elif response.HasField("deletePvMetadataResult"):
                self.logger.info("Successfully deleted PV metadata for: %s", request.pvNameOrAlias)
                return DeletePvMetadataApiResult(is_error=False, message="", response=response)

            else:
                error_msg = "Unexpected response format: neither exceptionalResult nor deletePvMetadataResult found"
                self.logger.error(error_msg)
                return DeletePvMetadataApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during deletePvMetadata: %s", e.details())
            return DeletePvMetadataApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {e!s}"
            self.logger.error("Unexpected error during deletePvMetadata: %s", str(e), exc_info=True)
            return DeletePvMetadataApiResult(is_error=True, message=error_msg)

    def delete_pv_metadata(self, pv_name_or_alias: str) -> DeletePvMetadataApiResult:
        """
        User-facing method for invoking the deletePvMetadata() API method.
        :param pv_name_or_alias: PV name or alias to delete.
        :return: A DeletePvMetadataApiResult with the method response and status information.
        """
        self.logger.info("Starting deletePvMetadata operation for: %s", pv_name_or_alias)

        request = self._build_delete_pv_metadata_request(pv_name_or_alias)
        result = self._send_delete_pv_metadata(request)

        if result.result_status.is_error:
            self.logger.error("DeletePvMetadata operation failed: %s", result.result_status.message)
        else:
            self.logger.info("DeletePvMetadata operation completed successfully for: %s", pv_name_or_alias)

        return result
