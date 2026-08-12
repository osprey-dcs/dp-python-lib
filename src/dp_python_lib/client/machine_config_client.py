import math
from datetime import datetime, timezone
from typing import Optional, Dict, List, Iterator, Union
from dp_python_lib.client.service_api_client_base import ServiceApiClientBase
from dp_python_lib.client.result import ApiResultBase
from dp_python_lib.grpc import annotation_pb2_grpc
from dp_python_lib.grpc import annotation_pb2
from dp_python_lib.grpc import common_pb2
import grpc
import logging


# Accepted input types for API parameters that map to a common.Timestamp:
# a timezone-aware datetime, epoch seconds (int or float), or an already-built Timestamp.
TimestampInput = Union[datetime, int, float, common_pb2.Timestamp]


def to_timestamp(value: TimestampInput) -> common_pb2.Timestamp:
    """
    Converts a user-supplied time value into a common.Timestamp{epochSeconds, nanoseconds}.

    Accepts:
      - a timezone-aware datetime (naive datetimes are rejected to avoid silent local-timezone bugs),
      - epoch seconds as an int or float (float fractional part becomes nanoseconds),
      - an already-built common.Timestamp (returned as-is).

    :param value: The time value to convert.
    :return: An equivalent common.Timestamp.
    :raises ValueError: if a datetime is naive (has no tzinfo), or if the resulting epoch seconds are negative
        (pre-1970) -- common.Timestamp.epochSeconds is an unsigned (uint64) field and cannot represent them.
    :raises TypeError: if value is not one of the supported types.
    """
    if isinstance(value, common_pb2.Timestamp):
        return value

    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                "to_timestamp() requires a timezone-aware datetime; naive datetimes are rejected "
                "to avoid silent local-timezone bugs. Use datetime.now(timezone.utc) or attach tzinfo."
            )
        epoch = value.timestamp()
        return to_timestamp(epoch)

    if isinstance(value, bool):
        # bool is a subclass of int; reject it explicitly as it is virtually always a mistake.
        raise TypeError("to_timestamp() does not accept bool")

    if isinstance(value, (int, float)):
        # Floor the seconds (not truncate toward zero) so the fractional remainder, and therefore
        # nanoseconds, is always in [0, 1_000_000_000) even for negative epoch inputs.
        epoch_seconds = math.floor(value)
        nanoseconds = int(round((float(value) - epoch_seconds) * 1_000_000_000))
        # Guard against float rounding pushing nanoseconds up to a full second.
        if nanoseconds >= 1_000_000_000:
            epoch_seconds += 1
            nanoseconds -= 1_000_000_000
        timestamp = common_pb2.Timestamp()
        timestamp.epochSeconds = epoch_seconds
        timestamp.nanoseconds = nanoseconds
        return timestamp

    raise TypeError(
        f"to_timestamp() expects datetime, int/float epoch seconds, or common.Timestamp, got {type(value).__name__}"
    )


class ConfigurationQuery:
    """
    Factory of lightweight helpers for building QueryConfigurationsRequest.QueryConfigurationsCriterion objects for use
    with MachineConfigClient.query_configurations() and iter_configurations().  Each helper returns a single criterion;
    callers pass a list of criteria to the query methods.

    Example:
        from dp_python_lib.client import ConfigurationQuery as C
        criteria = [C.name(prefix=["beamline-"]), C.tags(["production"])]
        result = client.annotation.machine_config.query_configurations(criteria=criteria)
    """

    _Criterion = annotation_pb2.QueryConfigurationsRequest.QueryConfigurationsCriterion

    @staticmethod
    def name(
        exact: Optional[List[str]] = None,
        prefix: Optional[List[str]] = None,
        contains: Optional[List[str]] = None,
    ) -> "annotation_pb2.QueryConfigurationsRequest.QueryConfigurationsCriterion":
        """
        Builds a criterion matching configuration names by exact value, prefix, and/or substring.
        :param exact: Configuration names to match exactly.
        :param prefix: Configuration name prefixes to match.
        :param contains: Substrings the configuration name must contain.
        :return: A QueryConfigurationsCriterion with a nameCriterion.
        :raises ValueError: if none of exact/prefix/contains is provided and non-empty.
        """
        if not (exact or prefix or contains):
            raise ValueError("name() requires at least one non-empty of exact/prefix/contains")
        criterion = ConfigurationQuery._Criterion()
        name_criterion = criterion.nameCriterion
        if exact:
            name_criterion.exact[:] = exact
        if prefix:
            name_criterion.prefix[:] = prefix
        if contains:
            name_criterion.contains[:] = contains
        return criterion

    @staticmethod
    def category(
        values: List[str],
    ) -> "annotation_pb2.QueryConfigurationsRequest.QueryConfigurationsCriterion":
        """
        Builds a criterion matching configurations having any of the specified categories.
        :param values: Category values to match.
        :return: A QueryConfigurationsCriterion with a categoryCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("category() requires a non-empty values list")
        criterion = ConfigurationQuery._Criterion()
        criterion.categoryCriterion.values[:] = values
        return criterion

    @staticmethod
    def tags(
        values: List[str],
    ) -> "annotation_pb2.QueryConfigurationsRequest.QueryConfigurationsCriterion":
        """
        Builds a criterion matching configurations having any of the specified tags.
        :param values: Tag values to match.
        :return: A QueryConfigurationsCriterion with a tagsCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("tags() requires a non-empty values list")
        criterion = ConfigurationQuery._Criterion()
        criterion.tagsCriterion.values[:] = values
        return criterion

    @staticmethod
    def attributes(
        key: str, values: List[str]
    ) -> "annotation_pb2.QueryConfigurationsRequest.QueryConfigurationsCriterion":
        """
        Builds a criterion matching configurations whose attribute with the given key has any of the specified values.
        :param key: Attribute key to match.
        :param values: Attribute values to match for that key.
        :return: A QueryConfigurationsCriterion with an attributesCriterion.
        :raises ValueError: if key is empty or values is empty.
        """
        if not key:
            raise ValueError("attributes() requires a non-empty key")
        if not values:
            raise ValueError("attributes() requires a non-empty values list")
        criterion = ConfigurationQuery._Criterion()
        criterion.attributesCriterion.key = key
        criterion.attributesCriterion.values[:] = values
        return criterion

    @staticmethod
    def parent(
        values: List[str],
    ) -> "annotation_pb2.QueryConfigurationsRequest.QueryConfigurationsCriterion":
        """
        Builds a criterion matching configurations whose parent configuration name is any of the specified values.
        :param values: Parent configuration names to match.
        :return: A QueryConfigurationsCriterion with a parentCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("parent() requires a non-empty values list")
        criterion = ConfigurationQuery._Criterion()
        criterion.parentCriterion.values[:] = values
        return criterion


class ConfigurationActivationQuery:
    """
    Factory of lightweight helpers for building
    QueryConfigurationActivationsRequest.QueryConfigurationActivationsCriterion objects for use with
    MachineConfigClient.query_configuration_activations() and iter_configuration_activations().  Each helper returns a
    single criterion; callers pass a list of criteria to the query methods.

    Example:
        from dp_python_lib.client import ConfigurationActivationQuery as CA
        criteria = [CA.configuration_name(["cfg-1"]), CA.time_range(start_dt, end_dt)]
        result = client.annotation.machine_config.query_configuration_activations(criteria=criteria)
    """

    _Criterion = (
        annotation_pb2.QueryConfigurationActivationsRequest.QueryConfigurationActivationsCriterion
    )

    @staticmethod
    def timestamp(
        value: TimestampInput,
    ) -> (
        "annotation_pb2.QueryConfigurationActivationsRequest.QueryConfigurationActivationsCriterion"
    ):
        """
        Builds a criterion matching activations that were active at the specified instant.
        :param value: The instant to match, as a tz-aware datetime, epoch seconds, or common.Timestamp
            (see to_timestamp()).
        :return: A QueryConfigurationActivationsCriterion with a timestampCriterion.
        """
        criterion = ConfigurationActivationQuery._Criterion()
        criterion.timestampCriterion.timestamp.CopyFrom(to_timestamp(value))
        return criterion

    @staticmethod
    def time_range(
        start: TimestampInput, end: TimestampInput
    ) -> (
        "annotation_pb2.QueryConfigurationActivationsRequest.QueryConfigurationActivationsCriterion"
    ):
        """
        Builds a criterion matching activations that overlap the specified time range.
        :param start: Start of the range, as a tz-aware datetime, epoch seconds, or common.Timestamp.
        :param end: End of the range, as a tz-aware datetime, epoch seconds, or common.Timestamp.
        :return: A QueryConfigurationActivationsCriterion with a timeRangeCriterion.
        """
        criterion = ConfigurationActivationQuery._Criterion()
        criterion.timeRangeCriterion.startTime.CopyFrom(to_timestamp(start))
        criterion.timeRangeCriterion.endTime.CopyFrom(to_timestamp(end))
        return criterion

    @staticmethod
    def configuration_name(
        values: List[str],
    ) -> (
        "annotation_pb2.QueryConfigurationActivationsRequest.QueryConfigurationActivationsCriterion"
    ):
        """
        Builds a criterion matching activations whose configuration name is any of the specified values.
        :param values: Configuration names to match.
        :return: A QueryConfigurationActivationsCriterion with a configurationNameCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("configuration_name() requires a non-empty values list")
        criterion = ConfigurationActivationQuery._Criterion()
        criterion.configurationNameCriterion.values[:] = values
        return criterion

    @staticmethod
    def client_activation_id(
        values: List[str],
    ) -> (
        "annotation_pb2.QueryConfigurationActivationsRequest.QueryConfigurationActivationsCriterion"
    ):
        """
        Builds a criterion matching activations whose client activation id is any of the specified values.
        :param values: Client activation ids to match.
        :return: A QueryConfigurationActivationsCriterion with a clientActivationIdCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("client_activation_id() requires a non-empty values list")
        criterion = ConfigurationActivationQuery._Criterion()
        criterion.clientActivationIdCriterion.values[:] = values
        return criterion

    @staticmethod
    def category(
        values: List[str],
    ) -> (
        "annotation_pb2.QueryConfigurationActivationsRequest.QueryConfigurationActivationsCriterion"
    ):
        """
        Builds a criterion matching activations whose configuration category is any of the specified values.
        :param values: Category values to match.
        :return: A QueryConfigurationActivationsCriterion with a categoryCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("category() requires a non-empty values list")
        criterion = ConfigurationActivationQuery._Criterion()
        criterion.categoryCriterion.values[:] = values
        return criterion

    @staticmethod
    def tags(
        values: List[str],
    ) -> (
        "annotation_pb2.QueryConfigurationActivationsRequest.QueryConfigurationActivationsCriterion"
    ):
        """
        Builds a criterion matching activations having any of the specified tags.
        :param values: Tag values to match.
        :return: A QueryConfigurationActivationsCriterion with a tagsCriterion.
        :raises ValueError: if values is empty.
        """
        if not values:
            raise ValueError("tags() requires a non-empty values list")
        criterion = ConfigurationActivationQuery._Criterion()
        criterion.tagsCriterion.values[:] = values
        return criterion

    @staticmethod
    def attributes(
        key: str, values: List[str]
    ) -> (
        "annotation_pb2.QueryConfigurationActivationsRequest.QueryConfigurationActivationsCriterion"
    ):
        """
        Builds a criterion matching activations whose attribute with the given key has any of the specified values.
        :param key: Attribute key to match.
        :param values: Attribute values to match for that key.
        :return: A QueryConfigurationActivationsCriterion with an attributesCriterion.
        :raises ValueError: if key is empty or values is empty.
        """
        if not key:
            raise ValueError("attributes() requires a non-empty key")
        if not values:
            raise ValueError("attributes() requires a non-empty values list")
        criterion = ConfigurationActivationQuery._Criterion()
        criterion.attributesCriterion.key = key
        criterion.attributesCriterion.values[:] = values
        return criterion


class SaveConfigurationRequestParams:
    """
    Encapsulates client parameters for a call to the saveConfiguration() API method.
    """

    def __init__(
        self,
        configuration_name: str,
        category: Optional[str] = None,
        description: Optional[str] = None,
        parent_configuration_name: Optional[str] = None,
        tags: Optional[List[str]] = None,
        attributes: Optional[Dict[str, str]] = None,
        modified_by: Optional[str] = None,
    ) -> None:
        """
        :param configuration_name: Name of the machine configuration being saved.
        :param category: Category grouping for the configuration.
        :param description: Human-readable description of the configuration.
        :param parent_configuration_name: Name of the parent configuration, for hierarchical configurations.
        :param tags: List of tags (keywords) describing the configuration.
        :param attributes: Map of key/value attributes describing the configuration.
        :param modified_by: Identifier of the user or process making the change.
        """
        self.configuration_name = configuration_name
        self.category = category
        self.description = description
        self.parent_configuration_name = parent_configuration_name
        self.tags = tags
        self.attributes = attributes
        self.modified_by = modified_by


class SaveConfigurationApiResult(ApiResultBase):
    """
    Wraps the response from saveConfiguration(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: Optional[annotation_pb2.SaveConfigurationResponse] = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The SaveConfigurationResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def configuration_name(self) -> Optional[str]:
        """Name of the configuration that was saved, or None on error."""
        if self.response is not None and self.response.HasField("saveConfigurationResult"):
            return self.response.saveConfigurationResult.configurationName
        return None


class GetConfigurationApiResult(ApiResultBase):
    """
    Wraps the response from getConfiguration(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: Optional[annotation_pb2.GetConfigurationResponse] = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The GetConfigurationResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def configuration(self) -> Optional[common_pb2.Configuration]:
        """The Configuration for the requested name, or None on error."""
        if self.response is not None and self.response.HasField("getConfigurationResult"):
            return self.response.getConfigurationResult.configuration
        return None


class QueryConfigurationsApiResult(ApiResultBase):
    """
    Wraps a single page of the response from queryConfigurations(), with a status object including an error flag and
    message.  Use MachineConfigClient.iter_configurations() to transparently page through all results.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: Optional[annotation_pb2.QueryConfigurationsResponse] = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The QueryConfigurationsResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def configurations(self) -> List[common_pb2.Configuration]:
        """The Configuration records in this page, or an empty list on error."""
        if self.response is not None and self.response.HasField("queryConfigurationsResult"):
            return list(self.response.queryConfigurationsResult.configurations)
        return []

    @property
    def next_page_token(self) -> str:
        """Token for retrieving the next page, or empty string if there are no more pages."""
        if self.response is not None and self.response.HasField("queryConfigurationsResult"):
            return self.response.queryConfigurationsResult.nextPageToken
        return ""


class DeleteConfigurationApiResult(ApiResultBase):
    """
    Wraps the response from deleteConfiguration(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: Optional[annotation_pb2.DeleteConfigurationResponse] = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The DeleteConfigurationResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def configuration_name(self) -> Optional[str]:
        """Name of the configuration that was deleted, or None on error."""
        if self.response is not None and self.response.HasField("deleteConfigurationResult"):
            return self.response.deleteConfigurationResult.configurationName
        return None


class SaveConfigurationActivationRequestParams:
    """
    Encapsulates client parameters for a call to the saveConfigurationActivation() API method.
    """

    def __init__(
        self,
        configuration_name: str,
        start_time: TimestampInput,
        end_time: Optional[TimestampInput] = None,
        client_activation_id: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        attributes: Optional[Dict[str, str]] = None,
        modified_by: Optional[str] = None,
    ) -> None:
        """
        :param configuration_name: Name of the configuration that was active.
        :param start_time: Start of the activation interval (tz-aware datetime, epoch seconds, or common.Timestamp).
        :param end_time: End of the activation interval (tz-aware datetime, epoch seconds, or common.Timestamp).
            Omit or pass None for an open-ended activation, meaning the configuration is still in effect.
        :param client_activation_id: Optional client-supplied identifier for the activation.
        :param description: Human-readable description of the activation.
        :param tags: List of tags (keywords) describing the activation.
        :param attributes: Map of key/value attributes describing the activation.
        :param modified_by: Identifier of the user or process making the change.
        """
        self.configuration_name = configuration_name
        self.start_time = start_time
        self.end_time = end_time
        self.client_activation_id = client_activation_id
        self.description = description
        self.tags = tags
        self.attributes = attributes
        self.modified_by = modified_by


class SaveConfigurationActivationApiResult(ApiResultBase):
    """
    Wraps the response from saveConfigurationActivation(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: Optional[annotation_pb2.SaveConfigurationActivationResponse] = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The SaveConfigurationActivationResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def client_activation_id(self) -> Optional[str]:
        """Client activation id of the activation that was saved, or None on error."""
        if self.response is not None and self.response.HasField(
            "saveConfigurationActivationResult"
        ):
            return self.response.saveConfigurationActivationResult.clientActivationId
        return None


class GetConfigurationActivationApiResult(ApiResultBase):
    """
    Wraps the response from getConfigurationActivation(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: Optional[annotation_pb2.GetConfigurationActivationResponse] = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The GetConfigurationActivationResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def configuration_activation(self) -> Optional[common_pb2.ConfigurationActivation]:
        """The ConfigurationActivation for the requested key, or None on error."""
        if self.response is not None and self.response.HasField("getConfigurationActivationResult"):
            return self.response.getConfigurationActivationResult.configurationActivation
        return None


class QueryConfigurationActivationsApiResult(ApiResultBase):
    """
    Wraps a single page of the response from queryConfigurationActivations(), with a status object including an error
    flag and message.  Use MachineConfigClient.iter_configuration_activations() to transparently page through all
    results.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: Optional[annotation_pb2.QueryConfigurationActivationsResponse] = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The QueryConfigurationActivationsResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def configuration_activations(self) -> List[common_pb2.ConfigurationActivation]:
        """The ConfigurationActivation records in this page, or an empty list on error."""
        if self.response is not None and self.response.HasField(
            "queryConfigurationActivationsResult"
        ):
            return list(self.response.queryConfigurationActivationsResult.configurationActivations)
        return []

    @property
    def next_page_token(self) -> str:
        """Token for retrieving the next page, or empty string if there are no more pages."""
        if self.response is not None and self.response.HasField(
            "queryConfigurationActivationsResult"
        ):
            return self.response.queryConfigurationActivationsResult.nextPageToken
        return ""


class DeleteConfigurationActivationApiResult(ApiResultBase):
    """
    Wraps the response from deleteConfigurationActivation(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: Optional[annotation_pb2.DeleteConfigurationActivationResponse] = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The DeleteConfigurationActivationResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def client_activation_id(self) -> Optional[str]:
        """Client activation id of the activation that was deleted, or None on error."""
        if self.response is not None and self.response.HasField(
            "deleteConfigurationActivationResult"
        ):
            return self.response.deleteConfigurationActivationResult.clientActivationId
        return None


class GetActiveConfigurationsApiResult(ApiResultBase):
    """
    Wraps the response from getActiveConfigurations(), with a status object including an error flag and message.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: Optional[annotation_pb2.GetActiveConfigurationsResponse] = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The GetActiveConfigurationsResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def configuration_activations(self) -> List[common_pb2.ConfigurationActivation]:
        """The ConfigurationActivation records active at the requested instant, or an empty list on error."""
        if self.response is not None and self.response.HasField("getActiveConfigurationsResult"):
            return list(self.response.getActiveConfigurationsResult.configurationActivations)
        return []


class MachineConfigClient(ServiceApiClientBase):
    """
    User-facing client for the machine configuration methods of the MLDP Annotation Service.  Covers machine
    configurations (saveConfiguration/getConfiguration/queryConfigurations/deleteConfiguration), their temporal
    activations (saveConfigurationActivation/getConfigurationActivation/queryConfigurationActivations/
    deleteConfigurationActivation), and the point-in-time getActiveConfigurations() lookup.  Provides low-level
    wrappers plus conveniences such as dict/list inputs, friendly timestamp inputs, composite-key lookups, and
    iter_configurations()/iter_configuration_activations() paging iterators.
    """

    def __init__(self, channel: grpc.Channel) -> None:
        """
        :param channel: gRPC communication channel for the Annotation Service.
        """
        super().__init__(channel, annotation_pb2_grpc.DpAnnotationServiceStub)
        self.logger = logging.getLogger(__name__)
        self.logger.debug("MachineConfigClient initialized with channel: %s", channel)

    # ------------------------------------------------------------------
    # saveConfiguration
    # ------------------------------------------------------------------

    def _build_save_configuration_request(
        self, request_params: SaveConfigurationRequestParams
    ) -> annotation_pb2.SaveConfigurationRequest:
        """
        Builds a SaveConfigurationRequest from the supplied SaveConfigurationRequestParams.
        :param request_params: User parameters for the call to saveConfiguration().
        :return: A SaveConfigurationRequest for the specified params.
        """
        self.logger.debug(
            "Building SaveConfigurationRequest for configuration: %s",
            request_params.configuration_name,
        )

        request = annotation_pb2.SaveConfigurationRequest()
        request.configurationName = request_params.configuration_name

        if request_params.category:
            request.category = request_params.category

        if request_params.description:
            request.description = request_params.description

        if request_params.parent_configuration_name:
            request.parentConfigurationName = request_params.parent_configuration_name

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

        self.logger.debug("SaveConfigurationRequest built successfully")
        return request

    def _send_save_configuration(
        self, request: annotation_pb2.SaveConfigurationRequest
    ) -> SaveConfigurationApiResult:
        """
        Invokes the saveConfiguration() API method with the supplied request.
        :param request: SaveConfigurationRequest with parameters for the call.
        :return: A SaveConfigurationApiResult with the method response and status information.
        """
        self.logger.info(
            "Calling saveConfiguration API for configuration: %s", request.configurationName
        )

        try:
            self.logger.debug("Invoking stub.saveConfiguration with request")
            response = self._stub.saveConfiguration(request)
            self.logger.debug("Received response from saveConfiguration API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning("SaveConfiguration API returned business error: %s", error_msg)
                return SaveConfigurationApiResult(is_error=True, message=error_msg)

            elif response.HasField("saveConfigurationResult"):
                self.logger.info("Successfully saved configuration: %s", request.configurationName)
                return SaveConfigurationApiResult(is_error=False, message="", response=response)

            else:
                error_msg = "Unexpected response format: neither exceptionalResult nor saveConfigurationResult found"
                self.logger.error(error_msg)
                return SaveConfigurationApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during saveConfiguration: %s", e.details())
            return SaveConfigurationApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.logger.error(
                "Unexpected error during saveConfiguration: %s", str(e), exc_info=True
            )
            return SaveConfigurationApiResult(is_error=True, message=error_msg)

    def save_configuration(
        self, request_params: SaveConfigurationRequestParams
    ) -> SaveConfigurationApiResult:
        """
        User-facing method for invoking the saveConfiguration() API method.
        :param request_params: Contains user parameters for the call to saveConfiguration().
        :return: A SaveConfigurationApiResult with the method response and status information.
        """
        self.logger.info(
            "Starting saveConfiguration operation for configuration: %s",
            request_params.configuration_name,
        )

        request = self._build_save_configuration_request(request_params)
        result = self._send_save_configuration(request)

        if result.result_status.is_error:
            self.logger.error(
                "SaveConfiguration operation failed: %s", result.result_status.message
            )
        else:
            self.logger.info(
                "SaveConfiguration operation completed successfully for: %s",
                request_params.configuration_name,
            )

        return result

    # ------------------------------------------------------------------
    # getConfiguration
    # ------------------------------------------------------------------

    def _build_get_configuration_request(
        self, configuration_name: str
    ) -> annotation_pb2.GetConfigurationRequest:
        """
        Builds a GetConfigurationRequest for the supplied configuration name.
        :param configuration_name: Configuration name to look up.
        :return: A GetConfigurationRequest for the specified name.
        """
        self.logger.debug("Building GetConfigurationRequest for: %s", configuration_name)
        request = annotation_pb2.GetConfigurationRequest()
        request.configurationName = configuration_name
        return request

    def _send_get_configuration(
        self, request: annotation_pb2.GetConfigurationRequest
    ) -> GetConfigurationApiResult:
        """
        Invokes the getConfiguration() API method with the supplied request.
        :param request: GetConfigurationRequest with parameters for the call.
        :return: A GetConfigurationApiResult with the method response and status information.
        """
        self.logger.info("Calling getConfiguration API for: %s", request.configurationName)

        try:
            self.logger.debug("Invoking stub.getConfiguration with request")
            response = self._stub.getConfiguration(request)
            self.logger.debug("Received response from getConfiguration API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning("GetConfiguration API returned business error: %s", error_msg)
                return GetConfigurationApiResult(is_error=True, message=error_msg)

            elif response.HasField("getConfigurationResult"):
                self.logger.info(
                    "Successfully retrieved configuration: %s", request.configurationName
                )
                return GetConfigurationApiResult(is_error=False, message="", response=response)

            else:
                error_msg = "Unexpected response format: neither exceptionalResult nor getConfigurationResult found"
                self.logger.error(error_msg)
                return GetConfigurationApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during getConfiguration: %s", e.details())
            return GetConfigurationApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.logger.error("Unexpected error during getConfiguration: %s", str(e), exc_info=True)
            return GetConfigurationApiResult(is_error=True, message=error_msg)

    def get_configuration(self, configuration_name: str) -> GetConfigurationApiResult:
        """
        User-facing method for invoking the getConfiguration() API method.
        :param configuration_name: Configuration name to look up.
        :return: A GetConfigurationApiResult with the method response and status information.
        """
        self.logger.info("Starting getConfiguration operation for: %s", configuration_name)

        request = self._build_get_configuration_request(configuration_name)
        result = self._send_get_configuration(request)

        if result.result_status.is_error:
            self.logger.error("GetConfiguration operation failed: %s", result.result_status.message)
        else:
            self.logger.info(
                "GetConfiguration operation completed successfully for: %s", configuration_name
            )

        return result

    # ------------------------------------------------------------------
    # queryConfigurations
    # ------------------------------------------------------------------

    def _build_query_configurations_request(
        self,
        criteria: List[annotation_pb2.QueryConfigurationsRequest.QueryConfigurationsCriterion],
        limit: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> annotation_pb2.QueryConfigurationsRequest:
        """
        Builds a QueryConfigurationsRequest from the supplied criteria and paging parameters.
        :param criteria: List of QueryConfigurationsCriterion objects (see ConfigurationQuery helpers).
        :param limit: Maximum number of records to return per page (optional).
        :param page_token: Token for retrieving a subsequent page (optional).
        :return: A QueryConfigurationsRequest for the specified params.
        """
        self.logger.debug("Building QueryConfigurationsRequest with %d criteria", len(criteria))
        request = annotation_pb2.QueryConfigurationsRequest()
        request.criteria.extend(criteria)
        if limit is not None:
            request.limit = limit
        if page_token:
            request.pageToken = page_token
        return request

    def _send_query_configurations(
        self, request: annotation_pb2.QueryConfigurationsRequest
    ) -> QueryConfigurationsApiResult:
        """
        Invokes the queryConfigurations() API method with the supplied request.
        :param request: QueryConfigurationsRequest with parameters for the call.
        :return: A QueryConfigurationsApiResult with the method response and status information.
        """
        self.logger.info("Calling queryConfigurations API with %d criteria", len(request.criteria))

        try:
            self.logger.debug("Invoking stub.queryConfigurations with request")
            response = self._stub.queryConfigurations(request)
            self.logger.debug("Received response from queryConfigurations API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning(
                    "QueryConfigurations API returned business error: %s", error_msg
                )
                return QueryConfigurationsApiResult(is_error=True, message=error_msg)

            elif response.HasField("queryConfigurationsResult"):
                self.logger.info(
                    "QueryConfigurations returned %d records",
                    len(response.queryConfigurationsResult.configurations),
                )
                return QueryConfigurationsApiResult(is_error=False, message="", response=response)

            else:
                error_msg = (
                    "Unexpected response format: neither exceptionalResult nor "
                    "queryConfigurationsResult found"
                )
                self.logger.error(error_msg)
                return QueryConfigurationsApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during queryConfigurations: %s", e.details())
            return QueryConfigurationsApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.logger.error(
                "Unexpected error during queryConfigurations: %s", str(e), exc_info=True
            )
            return QueryConfigurationsApiResult(is_error=True, message=error_msg)

    def query_configurations(
        self,
        criteria: List[annotation_pb2.QueryConfigurationsRequest.QueryConfigurationsCriterion],
        limit: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> QueryConfigurationsApiResult:
        """
        User-facing method for invoking the queryConfigurations() API method.  Returns a single page of results; use
        iter_configurations() to page through all results transparently.
        :param criteria: List of QueryConfigurationsCriterion objects (see ConfigurationQuery helpers).
        :param limit: Maximum number of records to return per page (optional).
        :param page_token: Token for retrieving a subsequent page (optional).
        :return: A QueryConfigurationsApiResult with a single page of results and status information.
        """
        self.logger.info("Starting queryConfigurations operation with %d criteria", len(criteria))

        request = self._build_query_configurations_request(
            criteria, limit=limit, page_token=page_token
        )
        result = self._send_query_configurations(request)

        if result.result_status.is_error:
            self.logger.error(
                "QueryConfigurations operation failed: %s", result.result_status.message
            )
        else:
            self.logger.info("QueryConfigurations operation completed successfully")

        return result

    def iter_configurations(
        self,
        criteria: List[annotation_pb2.QueryConfigurationsRequest.QueryConfigurationsCriterion],
        limit: Optional[int] = None,
    ) -> Iterator[common_pb2.Configuration]:
        """
        Convenience generator that transparently pages through all queryConfigurations() results, following the
        nextPageToken until the results are exhausted.  Yields individual Configuration records.

        Raises RuntimeError if any page returns an error, so callers can distinguish failure from an empty result set.

        :param criteria: List of QueryConfigurationsCriterion objects (see ConfigurationQuery helpers).
        :param limit: Maximum number of records to return per page (optional).
        :return: An iterator over all matching Configuration records across all pages.
        """
        page_token: Optional[str] = None
        while True:
            result = self.query_configurations(criteria, limit=limit, page_token=page_token)
            if result.result_status.is_error:
                raise RuntimeError(
                    f"queryConfigurations failed during paging: {result.result_status.message}"
                )

            for configuration in result.configurations:
                yield configuration

            page_token = result.next_page_token
            if not page_token:
                break

    # ------------------------------------------------------------------
    # deleteConfiguration
    # ------------------------------------------------------------------

    def _build_delete_configuration_request(
        self, configuration_name: str
    ) -> annotation_pb2.DeleteConfigurationRequest:
        """
        Builds a DeleteConfigurationRequest for the supplied configuration name.
        :param configuration_name: Configuration name to delete.
        :return: A DeleteConfigurationRequest for the specified name.
        """
        self.logger.debug("Building DeleteConfigurationRequest for: %s", configuration_name)
        request = annotation_pb2.DeleteConfigurationRequest()
        request.configurationName = configuration_name
        return request

    def _send_delete_configuration(
        self, request: annotation_pb2.DeleteConfigurationRequest
    ) -> DeleteConfigurationApiResult:
        """
        Invokes the deleteConfiguration() API method with the supplied request.
        :param request: DeleteConfigurationRequest with parameters for the call.
        :return: A DeleteConfigurationApiResult with the method response and status information.
        """
        self.logger.info("Calling deleteConfiguration API for: %s", request.configurationName)

        try:
            self.logger.debug("Invoking stub.deleteConfiguration with request")
            response = self._stub.deleteConfiguration(request)
            self.logger.debug("Received response from deleteConfiguration API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning(
                    "DeleteConfiguration API returned business error: %s", error_msg
                )
                return DeleteConfigurationApiResult(is_error=True, message=error_msg)

            elif response.HasField("deleteConfigurationResult"):
                self.logger.info(
                    "Successfully deleted configuration: %s", request.configurationName
                )
                return DeleteConfigurationApiResult(is_error=False, message="", response=response)

            else:
                error_msg = (
                    "Unexpected response format: neither exceptionalResult nor "
                    "deleteConfigurationResult found"
                )
                self.logger.error(error_msg)
                return DeleteConfigurationApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during deleteConfiguration: %s", e.details())
            return DeleteConfigurationApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.logger.error(
                "Unexpected error during deleteConfiguration: %s", str(e), exc_info=True
            )
            return DeleteConfigurationApiResult(is_error=True, message=error_msg)

    def delete_configuration(self, configuration_name: str) -> DeleteConfigurationApiResult:
        """
        User-facing method for invoking the deleteConfiguration() API method.
        :param configuration_name: Configuration name to delete.
        :return: A DeleteConfigurationApiResult with the method response and status information.
        """
        self.logger.info("Starting deleteConfiguration operation for: %s", configuration_name)

        request = self._build_delete_configuration_request(configuration_name)
        result = self._send_delete_configuration(request)

        if result.result_status.is_error:
            self.logger.error(
                "DeleteConfiguration operation failed: %s", result.result_status.message
            )
        else:
            self.logger.info(
                "DeleteConfiguration operation completed successfully for: %s", configuration_name
            )

        return result

    # ------------------------------------------------------------------
    # saveConfigurationActivation
    # ------------------------------------------------------------------

    def _build_save_configuration_activation_request(
        self, request_params: SaveConfigurationActivationRequestParams
    ) -> annotation_pb2.SaveConfigurationActivationRequest:
        """
        Builds a SaveConfigurationActivationRequest from the supplied SaveConfigurationActivationRequestParams.
        :param request_params: User parameters for the call to saveConfigurationActivation().
        :return: A SaveConfigurationActivationRequest for the specified params.
        """
        self.logger.debug(
            "Building SaveConfigurationActivationRequest for configuration: %s",
            request_params.configuration_name,
        )

        request = annotation_pb2.SaveConfigurationActivationRequest()
        request.configurationName = request_params.configuration_name
        request.startTime.CopyFrom(to_timestamp(request_params.start_time))

        # endTime is left unset for an open-ended activation, which the API reads as "still in effect".
        if request_params.end_time is not None:
            request.endTime.CopyFrom(to_timestamp(request_params.end_time))

        if request_params.client_activation_id:
            request.clientActivationId = request_params.client_activation_id

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

        self.logger.debug("SaveConfigurationActivationRequest built successfully")
        return request

    def _send_save_configuration_activation(
        self, request: annotation_pb2.SaveConfigurationActivationRequest
    ) -> SaveConfigurationActivationApiResult:
        """
        Invokes the saveConfigurationActivation() API method with the supplied request.
        :param request: SaveConfigurationActivationRequest with parameters for the call.
        :return: A SaveConfigurationActivationApiResult with the method response and status information.
        """
        self.logger.info(
            "Calling saveConfigurationActivation API for configuration: %s",
            request.configurationName,
        )

        try:
            self.logger.debug("Invoking stub.saveConfigurationActivation with request")
            response = self._stub.saveConfigurationActivation(request)
            self.logger.debug("Received response from saveConfigurationActivation API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning(
                    "SaveConfigurationActivation API returned business error: %s", error_msg
                )
                return SaveConfigurationActivationApiResult(is_error=True, message=error_msg)

            elif response.HasField("saveConfigurationActivationResult"):
                self.logger.info(
                    "Successfully saved configuration activation for: %s", request.configurationName
                )
                return SaveConfigurationActivationApiResult(
                    is_error=False, message="", response=response
                )

            else:
                error_msg = (
                    "Unexpected response format: neither exceptionalResult nor "
                    "saveConfigurationActivationResult found"
                )
                self.logger.error(error_msg)
                return SaveConfigurationActivationApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during saveConfigurationActivation: %s", e.details())
            return SaveConfigurationActivationApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.logger.error(
                "Unexpected error during saveConfigurationActivation: %s", str(e), exc_info=True
            )
            return SaveConfigurationActivationApiResult(is_error=True, message=error_msg)

    def save_configuration_activation(
        self, request_params: SaveConfigurationActivationRequestParams
    ) -> SaveConfigurationActivationApiResult:
        """
        User-facing method for invoking the saveConfigurationActivation() API method.
        :param request_params: Contains user parameters for the call to saveConfigurationActivation().
        :return: A SaveConfigurationActivationApiResult with the method response and status information.
        """
        self.logger.info(
            "Starting saveConfigurationActivation operation for configuration: %s",
            request_params.configuration_name,
        )

        request = self._build_save_configuration_activation_request(request_params)
        result = self._send_save_configuration_activation(request)

        if result.result_status.is_error:
            self.logger.error(
                "SaveConfigurationActivation operation failed: %s", result.result_status.message
            )
        else:
            self.logger.info(
                "SaveConfigurationActivation operation completed successfully for: %s",
                request_params.configuration_name,
            )

        return result

    # ------------------------------------------------------------------
    # Composite-key helper (shared by get/delete configuration activation)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_activation_key(
        client_activation_id: Optional[str],
        configuration_name: Optional[str],
        start_time: Optional[TimestampInput],
    ) -> None:
        """
        Validates that exactly one activation key form is supplied: either a non-empty client_activation_id, or the
        composite (non-empty configuration_name and a start_time).  Raises ValueError otherwise.  Empty strings count
        as "not provided" so that a caller error (e.g. an unset id) is caught here rather than silently producing a
        request with an empty oneof value.  start_time presence is keyed on "is not None" since a valid timestamp may
        be falsy (epoch 0).
        """
        has_id = bool(client_activation_id)
        has_name = bool(configuration_name)
        has_start = start_time is not None
        has_composite = has_name or has_start
        if has_id and has_composite:
            raise ValueError(
                "provide either client_activation_id OR (configuration_name and start_time), not both"
            )
        if not has_id:
            if not has_name or not has_start:
                raise ValueError(
                    "provide a non-empty client_activation_id, or both configuration_name and start_time"
                )

    # ------------------------------------------------------------------
    # getConfigurationActivation
    # ------------------------------------------------------------------

    def _build_get_configuration_activation_request(
        self,
        client_activation_id: Optional[str],
        configuration_name: Optional[str],
        start_time: Optional[TimestampInput],
    ) -> annotation_pb2.GetConfigurationActivationRequest:
        """
        Builds a GetConfigurationActivationRequest for either a client activation id or the composite
        (configuration name, start time) key.
        :param client_activation_id: Client activation id to look up (mutually exclusive with the composite key).
        :param configuration_name: Configuration name of the composite key.
        :param start_time: Start time of the composite key (tz-aware datetime, epoch seconds, or common.Timestamp).
        :return: A GetConfigurationActivationRequest for the specified key.
        :raises ValueError: if the supplied key form is invalid (see _validate_activation_key).
        """
        self._validate_activation_key(client_activation_id, configuration_name, start_time)
        request = annotation_pb2.GetConfigurationActivationRequest()
        if client_activation_id:
            self.logger.debug(
                "Building GetConfigurationActivationRequest by clientActivationId: %s",
                client_activation_id,
            )
            request.clientActivationId = client_activation_id
        else:
            self.logger.debug(
                "Building GetConfigurationActivationRequest by composite key: %s",
                configuration_name,
            )
            request.compositeKey.configurationName = configuration_name
            request.compositeKey.startTime.CopyFrom(to_timestamp(start_time))
        return request

    def _send_get_configuration_activation(
        self, request: annotation_pb2.GetConfigurationActivationRequest
    ) -> GetConfigurationActivationApiResult:
        """
        Invokes the getConfigurationActivation() API method with the supplied request.
        :param request: GetConfigurationActivationRequest with parameters for the call.
        :return: A GetConfigurationActivationApiResult with the method response and status information.
        """
        self.logger.info("Calling getConfigurationActivation API")

        try:
            self.logger.debug("Invoking stub.getConfigurationActivation with request")
            response = self._stub.getConfigurationActivation(request)
            self.logger.debug("Received response from getConfigurationActivation API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning(
                    "GetConfigurationActivation API returned business error: %s", error_msg
                )
                return GetConfigurationActivationApiResult(is_error=True, message=error_msg)

            elif response.HasField("getConfigurationActivationResult"):
                self.logger.info("Successfully retrieved configuration activation")
                return GetConfigurationActivationApiResult(
                    is_error=False, message="", response=response
                )

            else:
                error_msg = (
                    "Unexpected response format: neither exceptionalResult nor "
                    "getConfigurationActivationResult found"
                )
                self.logger.error(error_msg)
                return GetConfigurationActivationApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during getConfigurationActivation: %s", e.details())
            return GetConfigurationActivationApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.logger.error(
                "Unexpected error during getConfigurationActivation: %s", str(e), exc_info=True
            )
            return GetConfigurationActivationApiResult(is_error=True, message=error_msg)

    def get_configuration_activation(
        self,
        client_activation_id: Optional[str] = None,
        configuration_name: Optional[str] = None,
        start_time: Optional[TimestampInput] = None,
    ) -> GetConfigurationActivationApiResult:
        """
        User-facing method for invoking the getConfigurationActivation() API method.  Look up an activation by either
        its client activation id, or the composite (configuration_name, start_time) key -- exactly one form must be
        supplied.
        :param client_activation_id: Client activation id to look up.
        :param configuration_name: Configuration name of the composite key.
        :param start_time: Start time of the composite key (tz-aware datetime, epoch seconds, or common.Timestamp).
        :return: A GetConfigurationActivationApiResult with the method response and status information.
        :raises ValueError: if the supplied key form is invalid.
        """
        self.logger.info("Starting getConfigurationActivation operation")

        request = self._build_get_configuration_activation_request(
            client_activation_id, configuration_name, start_time
        )
        result = self._send_get_configuration_activation(request)

        if result.result_status.is_error:
            self.logger.error(
                "GetConfigurationActivation operation failed: %s", result.result_status.message
            )
        else:
            self.logger.info("GetConfigurationActivation operation completed successfully")

        return result

    # ------------------------------------------------------------------
    # queryConfigurationActivations
    # ------------------------------------------------------------------

    def _build_query_configuration_activations_request(
        self,
        criteria: List[
            annotation_pb2.QueryConfigurationActivationsRequest.QueryConfigurationActivationsCriterion
        ],
        limit: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> annotation_pb2.QueryConfigurationActivationsRequest:
        """
        Builds a QueryConfigurationActivationsRequest from the supplied criteria and paging parameters.
        :param criteria: List of QueryConfigurationActivationsCriterion objects (see ConfigurationActivationQuery).
        :param limit: Maximum number of records to return per page (optional).
        :param page_token: Token for retrieving a subsequent page (optional).
        :return: A QueryConfigurationActivationsRequest for the specified params.
        """
        self.logger.debug(
            "Building QueryConfigurationActivationsRequest with %d criteria", len(criteria)
        )
        request = annotation_pb2.QueryConfigurationActivationsRequest()
        request.criteria.extend(criteria)
        if limit is not None:
            request.limit = limit
        if page_token:
            request.pageToken = page_token
        return request

    def _send_query_configuration_activations(
        self, request: annotation_pb2.QueryConfigurationActivationsRequest
    ) -> QueryConfigurationActivationsApiResult:
        """
        Invokes the queryConfigurationActivations() API method with the supplied request.
        :param request: QueryConfigurationActivationsRequest with parameters for the call.
        :return: A QueryConfigurationActivationsApiResult with the method response and status information.
        """
        self.logger.info(
            "Calling queryConfigurationActivations API with %d criteria", len(request.criteria)
        )

        try:
            self.logger.debug("Invoking stub.queryConfigurationActivations with request")
            response = self._stub.queryConfigurationActivations(request)
            self.logger.debug("Received response from queryConfigurationActivations API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning(
                    "QueryConfigurationActivations API returned business error: %s", error_msg
                )
                return QueryConfigurationActivationsApiResult(is_error=True, message=error_msg)

            elif response.HasField("queryConfigurationActivationsResult"):
                self.logger.info(
                    "QueryConfigurationActivations returned %d records",
                    len(response.queryConfigurationActivationsResult.configurationActivations),
                )
                return QueryConfigurationActivationsApiResult(
                    is_error=False, message="", response=response
                )

            else:
                error_msg = (
                    "Unexpected response format: neither exceptionalResult nor "
                    "queryConfigurationActivationsResult found"
                )
                self.logger.error(error_msg)
                return QueryConfigurationActivationsApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during queryConfigurationActivations: %s", e.details())
            return QueryConfigurationActivationsApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.logger.error(
                "Unexpected error during queryConfigurationActivations: %s", str(e), exc_info=True
            )
            return QueryConfigurationActivationsApiResult(is_error=True, message=error_msg)

    def query_configuration_activations(
        self,
        criteria: List[
            annotation_pb2.QueryConfigurationActivationsRequest.QueryConfigurationActivationsCriterion
        ],
        limit: Optional[int] = None,
        page_token: Optional[str] = None,
    ) -> QueryConfigurationActivationsApiResult:
        """
        User-facing method for invoking the queryConfigurationActivations() API method.  Returns a single page of
        results; use iter_configuration_activations() to page through all results transparently.
        :param criteria: List of QueryConfigurationActivationsCriterion objects (see ConfigurationActivationQuery).
        :param limit: Maximum number of records to return per page (optional).
        :param page_token: Token for retrieving a subsequent page (optional).
        :return: A QueryConfigurationActivationsApiResult with a single page of results and status information.
        """
        self.logger.info(
            "Starting queryConfigurationActivations operation with %d criteria", len(criteria)
        )

        request = self._build_query_configuration_activations_request(
            criteria, limit=limit, page_token=page_token
        )
        result = self._send_query_configuration_activations(request)

        if result.result_status.is_error:
            self.logger.error(
                "QueryConfigurationActivations operation failed: %s", result.result_status.message
            )
        else:
            self.logger.info("QueryConfigurationActivations operation completed successfully")

        return result

    def iter_configuration_activations(
        self,
        criteria: List[
            annotation_pb2.QueryConfigurationActivationsRequest.QueryConfigurationActivationsCriterion
        ],
        limit: Optional[int] = None,
    ) -> Iterator[common_pb2.ConfigurationActivation]:
        """
        Convenience generator that transparently pages through all queryConfigurationActivations() results, following
        the nextPageToken until the results are exhausted.  Yields individual ConfigurationActivation records.

        Raises RuntimeError if any page returns an error, so callers can distinguish failure from an empty result set.

        :param criteria: List of QueryConfigurationActivationsCriterion objects (see ConfigurationActivationQuery).
        :param limit: Maximum number of records to return per page (optional).
        :return: An iterator over all matching ConfigurationActivation records across all pages.
        """
        page_token: Optional[str] = None
        while True:
            result = self.query_configuration_activations(
                criteria, limit=limit, page_token=page_token
            )
            if result.result_status.is_error:
                raise RuntimeError(
                    f"queryConfigurationActivations failed during paging: {result.result_status.message}"
                )

            for activation in result.configuration_activations:
                yield activation

            page_token = result.next_page_token
            if not page_token:
                break

    # ------------------------------------------------------------------
    # deleteConfigurationActivation
    # ------------------------------------------------------------------

    def _build_delete_configuration_activation_request(
        self,
        client_activation_id: Optional[str],
        configuration_name: Optional[str],
        start_time: Optional[TimestampInput],
    ) -> annotation_pb2.DeleteConfigurationActivationRequest:
        """
        Builds a DeleteConfigurationActivationRequest for either a client activation id or the composite
        (configuration name, start time) key.
        :param client_activation_id: Client activation id to delete (mutually exclusive with the composite key).
        :param configuration_name: Configuration name of the composite key.
        :param start_time: Start time of the composite key (tz-aware datetime, epoch seconds, or common.Timestamp).
        :return: A DeleteConfigurationActivationRequest for the specified key.
        :raises ValueError: if the supplied key form is invalid (see _validate_activation_key).
        """
        self._validate_activation_key(client_activation_id, configuration_name, start_time)
        request = annotation_pb2.DeleteConfigurationActivationRequest()
        if client_activation_id:
            self.logger.debug(
                "Building DeleteConfigurationActivationRequest by clientActivationId: %s",
                client_activation_id,
            )
            request.clientActivationId = client_activation_id
        else:
            self.logger.debug(
                "Building DeleteConfigurationActivationRequest by composite key: %s",
                configuration_name,
            )
            request.compositeKey.configurationName = configuration_name
            request.compositeKey.startTime.CopyFrom(to_timestamp(start_time))
        return request

    def _send_delete_configuration_activation(
        self, request: annotation_pb2.DeleteConfigurationActivationRequest
    ) -> DeleteConfigurationActivationApiResult:
        """
        Invokes the deleteConfigurationActivation() API method with the supplied request.
        :param request: DeleteConfigurationActivationRequest with parameters for the call.
        :return: A DeleteConfigurationActivationApiResult with the method response and status information.
        """
        self.logger.info("Calling deleteConfigurationActivation API")

        try:
            self.logger.debug("Invoking stub.deleteConfigurationActivation with request")
            response = self._stub.deleteConfigurationActivation(request)
            self.logger.debug("Received response from deleteConfigurationActivation API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning(
                    "DeleteConfigurationActivation API returned business error: %s", error_msg
                )
                return DeleteConfigurationActivationApiResult(is_error=True, message=error_msg)

            elif response.HasField("deleteConfigurationActivationResult"):
                self.logger.info("Successfully deleted configuration activation")
                return DeleteConfigurationActivationApiResult(
                    is_error=False, message="", response=response
                )

            else:
                error_msg = (
                    "Unexpected response format: neither exceptionalResult nor "
                    "deleteConfigurationActivationResult found"
                )
                self.logger.error(error_msg)
                return DeleteConfigurationActivationApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during deleteConfigurationActivation: %s", e.details())
            return DeleteConfigurationActivationApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.logger.error(
                "Unexpected error during deleteConfigurationActivation: %s", str(e), exc_info=True
            )
            return DeleteConfigurationActivationApiResult(is_error=True, message=error_msg)

    def delete_configuration_activation(
        self,
        client_activation_id: Optional[str] = None,
        configuration_name: Optional[str] = None,
        start_time: Optional[TimestampInput] = None,
    ) -> DeleteConfigurationActivationApiResult:
        """
        User-facing method for invoking the deleteConfigurationActivation() API method.  Delete an activation by
        either its client activation id, or the composite (configuration_name, start_time) key -- exactly one form
        must be supplied.
        :param client_activation_id: Client activation id to delete.
        :param configuration_name: Configuration name of the composite key.
        :param start_time: Start time of the composite key (tz-aware datetime, epoch seconds, or common.Timestamp).
        :return: A DeleteConfigurationActivationApiResult with the method response and status information.
        :raises ValueError: if the supplied key form is invalid.
        """
        self.logger.info("Starting deleteConfigurationActivation operation")

        request = self._build_delete_configuration_activation_request(
            client_activation_id, configuration_name, start_time
        )
        result = self._send_delete_configuration_activation(request)

        if result.result_status.is_error:
            self.logger.error(
                "DeleteConfigurationActivation operation failed: %s", result.result_status.message
            )
        else:
            self.logger.info("DeleteConfigurationActivation operation completed successfully")

        return result

    # ------------------------------------------------------------------
    # getActiveConfigurations
    # ------------------------------------------------------------------

    def _build_get_active_configurations_request(
        self, timestamp: Optional[TimestampInput] = None
    ) -> annotation_pb2.GetActiveConfigurationsRequest:
        """
        Builds a GetActiveConfigurationsRequest for the supplied instant, defaulting to "now" (current UTC).
        :param timestamp: The instant to look up (tz-aware datetime, epoch seconds, or common.Timestamp).  If None,
            the current UTC time is used.
        :return: A GetActiveConfigurationsRequest for the specified instant.
        """
        request = annotation_pb2.GetActiveConfigurationsRequest()
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
            self.logger.debug("Building GetActiveConfigurationsRequest for now (%s)", timestamp)
        else:
            self.logger.debug("Building GetActiveConfigurationsRequest for supplied timestamp")
        request.timestamp.CopyFrom(to_timestamp(timestamp))
        return request

    def _send_get_active_configurations(
        self, request: annotation_pb2.GetActiveConfigurationsRequest
    ) -> GetActiveConfigurationsApiResult:
        """
        Invokes the getActiveConfigurations() API method with the supplied request.
        :param request: GetActiveConfigurationsRequest with parameters for the call.
        :return: A GetActiveConfigurationsApiResult with the method response and status information.
        """
        self.logger.info("Calling getActiveConfigurations API")

        try:
            self.logger.debug("Invoking stub.getActiveConfigurations with request")
            response = self._stub.getActiveConfigurations(request)
            self.logger.debug("Received response from getActiveConfigurations API")

            if response.HasField("exceptionalResult"):
                error_msg = response.exceptionalResult.message
                self.logger.warning(
                    "GetActiveConfigurations API returned business error: %s", error_msg
                )
                return GetActiveConfigurationsApiResult(is_error=True, message=error_msg)

            elif response.HasField("getActiveConfigurationsResult"):
                self.logger.info(
                    "GetActiveConfigurations returned %d records",
                    len(response.getActiveConfigurationsResult.configurationActivations),
                )
                return GetActiveConfigurationsApiResult(
                    is_error=False, message="", response=response
                )

            else:
                error_msg = (
                    "Unexpected response format: neither exceptionalResult nor "
                    "getActiveConfigurationsResult found"
                )
                self.logger.error(error_msg)
                return GetActiveConfigurationsApiResult(is_error=True, message=error_msg)

        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            self.logger.error("gRPC error during getActiveConfigurations: %s", e.details())
            return GetActiveConfigurationsApiResult(is_error=True, message=error_msg)
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.logger.error(
                "Unexpected error during getActiveConfigurations: %s", str(e), exc_info=True
            )
            return GetActiveConfigurationsApiResult(is_error=True, message=error_msg)

    def get_active_configurations(
        self, timestamp: Optional[TimestampInput] = None
    ) -> GetActiveConfigurationsApiResult:
        """
        User-facing method for invoking the getActiveConfigurations() API method: returns every configuration
        activation whose interval covers the given instant.  With no argument, answers "what is active right now"
        (current UTC time).
        :param timestamp: The instant to look up (tz-aware datetime, epoch seconds, or common.Timestamp).  If None,
            the current UTC time is used.
        :return: A GetActiveConfigurationsApiResult with the method response and status information.
        """
        self.logger.info("Starting getActiveConfigurations operation")

        request = self._build_get_active_configurations_request(timestamp)
        result = self._send_get_active_configurations(request)

        if result.result_status.is_error:
            self.logger.error(
                "GetActiveConfigurations operation failed: %s", result.result_status.message
            )
        else:
            self.logger.info("GetActiveConfigurations operation completed successfully")

        return result
