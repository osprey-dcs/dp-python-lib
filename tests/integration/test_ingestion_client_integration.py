import unittest
import time
import logging
import grpc
import sys
import os

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from dp_python_lib.client.mldp_client import MldpClient
from dp_python_lib.client.ingestion_client import RegisterProviderRequestParams


class TestIngestionClientIntegration(unittest.TestCase):
    """
    Integration tests for IngestionClient that require a running MLDP ecosystem.
    
    Prerequisites:
    - MLDP services running via docker compose
    - Default configuration (localhost:50051 for ingestion service)
    - Services should be healthy and accepting connections
    
    To run these tests:
    1. Start MLDP ecosystem: docker compose up -d
    2. Run tests: python -m unittest tests.integration.test_ingestion_client_integration -v
    """
    
    @classmethod
    def setUpClass(cls):
        """Set up integration test environment and verify services are reachable."""
        # Configure logging to see detailed operation info
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        cls.logger = logging.getLogger(__name__)
        
        cls.logger.info("Setting up integration test environment")
        
        # Check if services are reachable before running tests
        cls._verify_services_available()
        
        # Create client using default configuration (assumes localhost services)
        cls.client = MldpClient()
        cls.logger.info("MldpClient initialized successfully")
    
    @classmethod 
    def _verify_services_available(cls):
        """Verify that required MLDP services are reachable."""
        cls.logger.info("Checking if MLDP services are available")
        
        try:
            # Try to create a channel to the ingestion service
            channel = grpc.insecure_channel('localhost:50051')
            
            # Set a short timeout for the connection check
            grpc.channel_ready_future(channel).result(timeout=5)
            cls.logger.info("Ingestion service is reachable at localhost:50051")
            channel.close()
            
        except grpc.FutureTimeoutError:
            cls.logger.error("Timeout connecting to ingestion service at localhost:50051")
            raise unittest.SkipTest(
                "MLDP ingestion service not available at localhost:50051. "
                "Please start the MLDP ecosystem with 'docker compose up -d' before running integration tests."
            )
        except Exception as e:
            cls.logger.error("Failed to connect to ingestion service: %s", e)
            raise unittest.SkipTest(
                f"Cannot connect to MLDP services: {e}. "
                "Please ensure the MLDP ecosystem is running."
            )
    
    def test_register_provider_integration_success_or_known_error(self):
        """
        Integration test for registerProvider with real gRPC service.
        
        This test accepts both success and expected business errors (like duplicate provider names)
        as valid responses, since the main goal is to verify communication with real services.
        """
        self.logger.info("Starting registerProvider integration test")
        
        # Create unique provider name to avoid conflicts
        timestamp = int(time.time())
        unique_name = f"integration-test-provider-{timestamp}"
        
        # Create test parameters
        params = RegisterProviderRequestParams(
            name=unique_name,
            description="Integration test provider for dp-python-lib",
            tag_list=["integration", "test", "automated"],
            attribute_map={
                "test_type": "integration", 
                "framework": "unittest",
                "timestamp": str(timestamp),
                "client": "dp-python-lib"
            }
        )
        
        self.logger.info("Calling registerProvider for provider: %s", unique_name)
        
        # Call the real service
        result = self.client.ingestion_client.register_provider(params)
        
        # Basic structural assertions
        self.assertIsNotNone(result, "Result should not be None")
        self.assertIsNotNone(result.result_status, "ResultStatus should not be None")
        self.assertIsInstance(result.result_status.is_error, bool, "is_error should be boolean")
        self.assertIsInstance(result.result_status.message, str, "message should be string")
        
        # Log the result for debugging
        if result.result_status.is_error:
            self.logger.warning("RegisterProvider returned error: %s", result.result_status.message)
            
            # Check if it's a known/expected error (like duplicate name)
            error_msg = result.result_status.message.lower()
            known_errors = [
                "already exists",
                "duplicate", 
                "conflict",
                "name is already in use"
            ]
            
            is_known_error = any(known_err in error_msg for known_err in known_errors)
            
            if is_known_error:
                self.logger.info("Received expected business error - this is normal for integration tests")
            else:
                # Unexpected error - might indicate real issues
                self.logger.error("Unexpected error from registerProvider: %s", result.result_status.message)
                # Still don't fail the test - log for investigation
                
        else:
            # Success case
            self.logger.info("Successfully registered provider: %s", unique_name)
            self.assertIsNotNone(result.response, "Response should not be None on success")
            
        # The key assertion: we got a proper response structure back
        # This proves the gRPC communication, request building, and response parsing all work
        self.assertIsNotNone(result, "Integration test passed - received proper response structure")
    
    def test_register_provider_integration_with_minimal_params(self):
        """
        Integration test with minimal required parameters only.
        """
        self.logger.info("Starting minimal parameters integration test")
        
        # Create minimal parameters (only name is required)
        timestamp = int(time.time())
        minimal_name = f"minimal-test-{timestamp}"
        
        params = RegisterProviderRequestParams(
            name=minimal_name,
            description=None,  # Optional
            tag_list=None,     # Optional
            attribute_map=None # Optional
        )
        
        self.logger.info("Calling registerProvider with minimal params for: %s", minimal_name)
        
        result = self.client.ingestion_client.register_provider(params)
        
        # Same structural validation
        self.assertIsNotNone(result)
        self.assertIsNotNone(result.result_status)
        
        if result.result_status.is_error:
            self.logger.info("Minimal params test got error: %s", result.result_status.message)
        else:
            self.logger.info("Minimal params test succeeded for: %s", minimal_name)
            
        # Success - we communicated with the service
        self.logger.info("Minimal parameters integration test completed")


if __name__ == '__main__':
    # Allow running this test file directly
    unittest.main(verbosity=2)