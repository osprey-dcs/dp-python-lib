import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from dp_python_lib.client.ingestion_client import IngestionClient, RegisterProviderRequestParams, RegisterProviderApiResult
from dp_python_lib.grpc import ingestion_pb2
from dp_python_lib.grpc import common_pb2


class TestIngestionClient(unittest.TestCase):
    
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.mock_channel = Mock()
        self.client = IngestionClient(self.mock_channel)
    
    def test_build_register_provider_request_all_fields(self):
        """Test _build_register_provider_request with all fields populated."""
        params = RegisterProviderRequestParams(
            name="test_provider",
            description="Test provider description",
            tag_list=["tag1", "tag2", "tag3"],
            attribute_map={"key1": "value1", "key2": "value2"}
        )
        
        request = self.client._build_register_provider_request(params)
        
        # Verify request type
        self.assertIsInstance(request, ingestion_pb2.RegisterProviderRequest)
        
        # Verify required field
        self.assertEqual(request.providerName, "test_provider")
        
        # Verify optional description
        self.assertEqual(request.description, "Test provider description")
        
        # Verify tags
        self.assertEqual(list(request.tags), ["tag1", "tag2", "tag3"])
        
        # Verify attributes
        self.assertEqual(len(request.attributes), 2)
        
        # Check attribute contents (order may vary)
        attr_dict = {attr.name: attr.value for attr in request.attributes}
        self.assertEqual(attr_dict, {"key1": "value1", "key2": "value2"})
    
    def test_build_register_provider_request_name_only(self):
        """Test _build_register_provider_request with only required name field."""
        params = RegisterProviderRequestParams(
            name="minimal_provider",
            description=None,
            tag_list=None,
            attribute_map=None
        )
        
        request = self.client._build_register_provider_request(params)
        
        # Verify request type and required field
        self.assertIsInstance(request, ingestion_pb2.RegisterProviderRequest)
        self.assertEqual(request.providerName, "minimal_provider")
        
        # Verify optional fields are not set or empty
        self.assertEqual(request.description, "")
        self.assertEqual(len(request.tags), 0)
        self.assertEqual(len(request.attributes), 0)
    
    def test_build_register_provider_request_empty_description(self):
        """Test _build_register_provider_request with empty description."""
        params = RegisterProviderRequestParams(
            name="test_provider",
            description="",
            tag_list=None,
            attribute_map=None
        )
        
        request = self.client._build_register_provider_request(params)
        
        self.assertEqual(request.providerName, "test_provider")
        # Empty string should not be set as description
        self.assertEqual(request.description, "")
    
    def test_build_register_provider_request_empty_collections(self):
        """Test _build_register_provider_request with empty tag_list and attribute_map."""
        params = RegisterProviderRequestParams(
            name="test_provider",
            description="Test description",
            tag_list=[],
            attribute_map={}
        )
        
        request = self.client._build_register_provider_request(params)
        
        self.assertEqual(request.providerName, "test_provider")
        self.assertEqual(request.description, "Test description")
        self.assertEqual(len(request.tags), 0)
        self.assertEqual(len(request.attributes), 0)
    
    def test_build_register_provider_request_single_tag(self):
        """Test _build_register_provider_request with single tag."""
        params = RegisterProviderRequestParams(
            name="test_provider",
            description=None,
            tag_list=["single_tag"],
            attribute_map=None
        )
        
        request = self.client._build_register_provider_request(params)
        
        self.assertEqual(request.providerName, "test_provider")
        self.assertEqual(list(request.tags), ["single_tag"])
        self.assertEqual(len(request.attributes), 0)
    
    def test_build_register_provider_request_single_attribute(self):
        """Test _build_register_provider_request with single attribute."""
        params = RegisterProviderRequestParams(
            name="test_provider",
            description=None,
            tag_list=None,
            attribute_map={"single_key": "single_value"}
        )
        
        request = self.client._build_register_provider_request(params)
        
        self.assertEqual(request.providerName, "test_provider")
        self.assertEqual(len(request.tags), 0)
        self.assertEqual(len(request.attributes), 1)
        
        attr = request.attributes[0]
        self.assertIsInstance(attr, common_pb2.Attribute)
        self.assertEqual(attr.name, "single_key")
        self.assertEqual(attr.value, "single_value")
    
    def test_build_register_provider_request_multiple_attributes(self):
        """Test _build_register_provider_request with multiple attributes."""
        params = RegisterProviderRequestParams(
            name="test_provider",
            description=None,
            tag_list=None,
            attribute_map={"attr1": "val1", "attr2": "val2", "attr3": "val3"}
        )
        
        request = self.client._build_register_provider_request(params)
        
        self.assertEqual(len(request.attributes), 3)
        
        # Verify all attributes are present
        attr_dict = {attr.name: attr.value for attr in request.attributes}
        expected = {"attr1": "val1", "attr2": "val2", "attr3": "val3"}
        self.assertEqual(attr_dict, expected)
        
        # Verify all are Attribute objects
        for attr in request.attributes:
            self.assertIsInstance(attr, common_pb2.Attribute)

    @patch('dp_python_lib.client.ingestion_client.ingestion_pb2_grpc.DpIngestionServiceStub')
    def test_send_register_provider_success(self, mock_stub_class):
        """Test _send_register_provider with successful response."""
        # Create mock response with registrationResult
        mock_response = Mock()
        mock_response.HasField = Mock()
        mock_response.HasField.side_effect = lambda field: field == 'registrationResult'
        
        # Setup mock stub
        mock_stub = Mock()
        mock_stub.registerProvider.return_value = mock_response
        mock_stub_class.return_value = mock_stub
        
        # Create test request
        request = ingestion_pb2.RegisterProviderRequest()
        request.providerName = "test_provider"
        
        # Call method
        result = self.client._send_register_provider(request)
        
        # Verify results
        self.assertIsInstance(result, RegisterProviderApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "")
        self.assertEqual(result.response, mock_response)
        
        # Verify stub was called correctly
        mock_stub_class.assert_called_once_with(self.mock_channel)
        mock_stub.registerProvider.assert_called_once_with(request)
    
    @patch('dp_python_lib.client.ingestion_client.ingestion_pb2_grpc.DpIngestionServiceStub')
    def test_send_register_provider_exceptional_result(self, mock_stub_class):
        """Test _send_register_provider with exceptionalResult (business error)."""
        # Create mock response with exceptionalResult
        mock_response = Mock()
        mock_response.HasField = Mock()
        mock_response.HasField.side_effect = lambda field: field == 'exceptionalResult'
        mock_response.exceptionalResult.message = "Provider name already exists"
        
        # Setup mock stub
        mock_stub = Mock()
        mock_stub.registerProvider.return_value = mock_response
        mock_stub_class.return_value = mock_stub
        
        # Create test request
        request = ingestion_pb2.RegisterProviderRequest()
        request.providerName = "duplicate_provider"
        
        # Call method
        result = self.client._send_register_provider(request)
        
        # Verify results
        self.assertIsInstance(result, RegisterProviderApiResult)
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "Provider name already exists")
        self.assertIsNone(result.response)
    
    @patch('dp_python_lib.client.ingestion_client.ingestion_pb2_grpc.DpIngestionServiceStub')
    def test_send_register_provider_unexpected_response(self, mock_stub_class):
        """Test _send_register_provider with unexpected response format."""
        # Create mock response with neither field
        mock_response = Mock()
        mock_response.HasField = Mock(return_value=False)
        
        # Setup mock stub
        mock_stub = Mock()
        mock_stub.registerProvider.return_value = mock_response
        mock_stub_class.return_value = mock_stub
        
        # Create test request
        request = ingestion_pb2.RegisterProviderRequest()
        request.providerName = "test_provider"
        
        # Call method
        result = self.client._send_register_provider(request)
        
        # Verify results
        self.assertIsInstance(result, RegisterProviderApiResult)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)
        self.assertIsNone(result.response)
    
    @patch('dp_python_lib.client.ingestion_client.ingestion_pb2_grpc.DpIngestionServiceStub')
    def test_send_register_provider_grpc_error(self, mock_stub_class):
        """Test _send_register_provider with gRPC RpcError."""
        # Setup mock stub to raise gRPC error
        mock_stub = Mock()
        mock_grpc_error = grpc.RpcError()
        mock_grpc_error.details = Mock(return_value="Connection timeout")
        mock_stub.registerProvider.side_effect = mock_grpc_error
        mock_stub_class.return_value = mock_stub
        
        # Create test request
        request = ingestion_pb2.RegisterProviderRequest()
        request.providerName = "test_provider"
        
        # Call method
        result = self.client._send_register_provider(request)
        
        # Verify results
        self.assertIsInstance(result, RegisterProviderApiResult)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: Connection timeout", result.result_status.message)
        self.assertIsNone(result.response)
    
    @patch('dp_python_lib.client.ingestion_client.ingestion_pb2_grpc.DpIngestionServiceStub')
    def test_send_register_provider_general_exception(self, mock_stub_class):
        """Test _send_register_provider with general exception."""
        # Setup mock stub to raise general exception
        mock_stub = Mock()
        mock_stub.registerProvider.side_effect = ValueError("Invalid parameter")
        mock_stub_class.return_value = mock_stub
        
        # Create test request
        request = ingestion_pb2.RegisterProviderRequest()
        request.providerName = "test_provider"
        
        # Call method
        result = self.client._send_register_provider(request)
        
        # Verify results
        self.assertIsInstance(result, RegisterProviderApiResult)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: Invalid parameter", result.result_status.message)
        self.assertIsNone(result.response)


if __name__ == '__main__':
    unittest.main()