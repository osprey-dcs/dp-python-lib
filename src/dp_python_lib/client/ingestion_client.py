from dp_python_lib.client.service_api_client_base import ServiceApiClientBase
from dp_python_lib.client.result import ApiResultBase
from dp_python_lib.grpc import ingestion_pb2_grpc
from dp_python_lib.grpc import ingestion_pb2
from dp_python_lib.grpc import common_pb2
import grpc


class RegisterProviderRequestParams:
    """
    Encapsulates client parameters for call to registerProvider() API method.
    """

    def __init__(self, name, description, tag_list, attribute_map):
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

    def __init__(self, is_error, message, response=None):
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

    def __init__(self, channel):
        """
        :param channel: gRPC communication channel for Ingestion Service.
        """
        super().__init__(channel)

    def _build_register_provider_request(self, request_params):
        """
        Builds a RegisterProviderRequest API object from the supplied RegisterProviderRequestParams object.
        :param request_params: A RegisterProviderRequestParams object containing the user parameters for call to registerProvider() API method.
        :return: Returns a RegisterProviderRequest API object for the specified params.
        """

        request = ingestion_pb2.RegisterProviderRequest()
        request.providerName = request_params.name

        if request_params.description:
            request.description = request_params.description

        if request_params.tag_list:
            request.tags[:] = request_params.tag_list

        if request_params.attribute_map:
            for name, value in request_params.attribute_map.items():
                attribute = common_pb2.Attribute()
                attribute.name = name
                attribute.value = value
                request.attributes.append(attribute)

        return request

    def _send_register_provider(self, request):
        """
        Invokes the registerProvider() API method with the supplied request object.
        :param request: RegisgerProviderRequest object with parameters for call to registerProvider().
        :return: Returns a RegisterProviderApiResult with the method response and status information.
        """
        ingestion_stub = ingestion_pb2_grpc.DpIngestionServiceStub(self._channel)
        
        try:
            response = ingestion_stub.registerProvider(request)
            
            # Check if response contains an exceptional result (error)
            if response.HasField('exceptionalResult'):
                return RegisterProviderApiResult(
                    is_error=True, 
                    message=response.exceptionalResult.message
                )
            
            # Check if response contains registration result (success)
            elif response.HasField('registrationResult'):
                return RegisterProviderApiResult(
                    is_error=False, 
                    message=None, 
                    response=response
                )
            
            # Unexpected response structure
            else:
                return RegisterProviderApiResult(
                    is_error=True, 
                    message="Unexpected response format: neither exceptionalResult nor registrationResult found"
                )
                
        except grpc.RpcError as e:
            return RegisterProviderApiResult(
                is_error=True, 
                message=f"gRPC error: {e.details()}"
            )
        except Exception as e:
            return RegisterProviderApiResult(
                is_error=True, 
                message=f"Unexpected error: {str(e)}"
            )

    def register_provider(self, request_params):
        """
        User facing method for invoking the registerProvider() API method.
        :param request_params: Contains user parameters for call to registerProvider() API method.
        :return: Returns RegisterProviderApiResult with the method response and status information.
        """
        request = self._build_register_provider_request(request_params)
        return self._send_register_provider(request)



