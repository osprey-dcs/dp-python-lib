from typing import Optional, Dict, List
from dp_python_lib.client.service_api_client_base import ServiceApiClientBase
from dp_python_lib.client.result import ApiResultBase
from dp_python_lib.grpc import ingestion_pb2_grpc
from dp_python_lib.grpc import ingestion_pb2
from dp_python_lib.grpc import common_pb2
import grpc
import logging


class RegisterProviderRequestParams:
    """
    Encapsulates client parameters for call to registerProvider() API method.
    """

    def __init__(self, name: str, description: Optional[str], tag_list: Optional[List[str]], attribute_map: Optional[Dict[str, str]]) -> None:
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

    def __init__(self, is_error: bool, message: str, response: Optional[ingestion_pb2.RegisterProviderResponse] = None) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurrent id API call.
        :param message: Error message describing error condition.
        :param response: The RegisterProviderResponse object returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

class IngestionClient(ServiceApiClientBase):
    """
    This is the user-facing Ingestion Service API class.  It provides methods and utility classes for calling
    Ingestion Service methods.
    """

    def __init__(self, channel: grpc.Channel) -> None:
        """
        :param channel: gRPC communication channel for Ingestion Service.
        """
        super().__init__(channel)
        self.logger = logging.getLogger(__name__)
        self.logger.debug("IngestionClient initialized with channel: %s", channel)

    def _build_register_provider_request(self, request_params: RegisterProviderRequestParams) -> ingestion_pb2.RegisterProviderRequest:
        """
        Builds a RegisterProviderRequest API object from the supplied RegisterProviderRequestParams object.
        :param request_params: A RegisterProviderRequestParams object containing the user parameters for call to registerProvider() API method.
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
            self.logger.debug("Adding %d attributes: %s", len(request_params.attribute_map), list(request_params.attribute_map.keys()))
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
        :param request: RegisgerProviderRequest object with parameters for call to registerProvider().
        :return: Returns a RegisterProviderApiResult with the method response and status information.
        """
        self.logger.info("Calling registerProvider API for provider: %s", request.providerName)
        ingestion_stub = ingestion_pb2_grpc.DpIngestionServiceStub(self._channel)
        
        try:
            self.logger.debug("Invoking ingestion_stub.registerProvider with request")
            response = ingestion_stub.registerProvider(request)
            self.logger.debug("Received response from registerProvider API")
            
            # Check if response contains an exceptional result (error)
            if response.HasField('exceptionalResult'):
                error_msg = response.exceptionalResult.message
                self.logger.warning("RegisterProvider API returned business error: %s", error_msg)
                return RegisterProviderApiResult(
                    is_error=True, 
                    message=error_msg
                )
            
            # Check if response contains registration result (success)
            elif response.HasField('registrationResult'):
                self.logger.info("Successfully registered provider: %s", request.providerName)
                return RegisterProviderApiResult(
                    is_error=False, 
                    message="", 
                    response=response
                )
            
            # Unexpected response structure
            else:
                error_msg = "Unexpected response format: neither exceptionalResult nor registrationResult found"
                self.logger.error(error_msg)
                return RegisterProviderApiResult(
                    is_error=True, 
                    message=error_msg
                )
                
        except grpc.RpcError as e:
            error_msg = f"gRPC error: {e.details()}"
            # Safely get error code - may not be available in test mocks
            try:
                error_code = e.code()
                self.logger.error("gRPC error during registerProvider: %s (code: %s)", e.details(), error_code)
            except (AttributeError, TypeError):
                self.logger.error("gRPC error during registerProvider: %s", e.details())
            return RegisterProviderApiResult(
                is_error=True, 
                message=error_msg
            )
        except Exception as e:
            error_msg = f"Unexpected error: {str(e)}"
            self.logger.error("Unexpected error during registerProvider: %s", str(e), exc_info=True)
            return RegisterProviderApiResult(
                is_error=True, 
                message=error_msg
            )

    def register_provider(self, request_params: RegisterProviderRequestParams) -> RegisterProviderApiResult:
        """
        User facing method for invoking the registerProvider() API method.
        :param request_params: Contains user parameters for call to registerProvider() API method.
        :return: Returns RegisterProviderApiResult with the method response and status information.
        """
        self.logger.info("Starting registerProvider operation for provider: %s", request_params.name)
        
        request = self._build_register_provider_request(request_params)
        result = self._send_register_provider(request)
        
        if result.result_status.is_error:
            self.logger.error("RegisterProvider operation failed: %s", result.result_status.message)
        else:
            self.logger.info("RegisterProvider operation completed successfully for provider: %s", request_params.name)
            
        return result



