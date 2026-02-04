import unittest
from unittest.mock import Mock, patch
import sys
import os
import tempfile

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from dp_python_lib.client.mldp_client import MldpClient
from dp_python_lib.config import MldpConfig, ServiceConfig


class TestMldpClient(unittest.TestCase):
    
    def setUp(self):
        """Set up test fixtures."""
        self.mock_channel = Mock()
    
    def test_init_with_explicit_channel(self):
        """Test MldpClient initialization with explicit ingestion channel."""
        client = MldpClient(ingestion_channel=self.mock_channel)
        
        self.assertEqual(client._ingestion_channel, self.mock_channel)
        self.assertIsNotNone(client.ingestion_client)
        self.assertIsNone(client._query_channel)
        self.assertIsNone(client._annotation_channel)
    
    def test_init_with_all_channels(self):
        """Test MldpClient initialization with all channels."""
        query_channel = Mock()
        annotation_channel = Mock()
        
        client = MldpClient(
            ingestion_channel=self.mock_channel,
            query_channel=query_channel,
            annotation_channel=annotation_channel
        )
        
        self.assertEqual(client._ingestion_channel, self.mock_channel)
        self.assertEqual(client._query_channel, query_channel)
        self.assertEqual(client._annotation_channel, annotation_channel)
    
    @patch('dp_python_lib.client.mldp_client.load_config')
    @patch('dp_python_lib.config.config.MldpConfig.create_ingestion_channel')
    @patch('dp_python_lib.config.config.MldpConfig.create_query_channel')
    @patch('dp_python_lib.config.config.MldpConfig.create_annotation_channel')
    def test_init_with_default_config(self, mock_annotation_channel, mock_query_channel, 
                                      mock_ingestion_channel, mock_load_config):
        """Test MldpClient initialization with default configuration."""
        mock_config = MldpConfig()
        mock_load_config.return_value = mock_config
        mock_ingestion_channel.return_value = self.mock_channel
        mock_query_channel.return_value = Mock()
        mock_annotation_channel.return_value = Mock()
        
        client = MldpClient()
        
        mock_load_config.assert_called_once_with(config_file=None)
        mock_ingestion_channel.assert_called_once()
        mock_query_channel.assert_called_once()
        mock_annotation_channel.assert_called_once()
        self.assertEqual(client._config, mock_config)
    
    @patch('dp_python_lib.client.mldp_client.load_config')
    @patch('dp_python_lib.config.config.MldpConfig.create_ingestion_channel')
    def test_init_with_config_file(self, mock_ingestion_channel, mock_load_config):
        """Test MldpClient initialization with specific config file."""
        mock_config = MldpConfig()
        mock_load_config.return_value = mock_config
        mock_ingestion_channel.return_value = self.mock_channel
        
        client = MldpClient(config_file='custom-config.yaml')
        
        mock_load_config.assert_called_once_with(config_file='custom-config.yaml')
        self.assertEqual(client._config, mock_config)
    
    @patch('dp_python_lib.config.config.MldpConfig.create_ingestion_channel')
    @patch('dp_python_lib.config.config.MldpConfig.create_query_channel')
    @patch('dp_python_lib.config.config.MldpConfig.create_annotation_channel')
    def test_init_with_explicit_config_object(self, mock_annotation_channel, 
                                              mock_query_channel, mock_ingestion_channel):
        """Test MldpClient initialization with explicit config object."""
        config = MldpConfig(ingestion_host='custom-host', ingestion_port=9999)
        
        mock_ingestion_channel.return_value = self.mock_channel
        mock_query_channel.return_value = Mock()
        mock_annotation_channel.return_value = Mock()
        
        client = MldpClient(config=config)
        
        mock_ingestion_channel.assert_called_once()
        self.assertEqual(client._config, config)
    
    def test_init_no_ingestion_channel_or_config(self):
        """Test MldpClient initialization fails without ingestion channel or config."""
        with patch('dp_python_lib.client.mldp_client.load_config') as mock_load_config:
            mock_load_config.return_value = None
            
            with self.assertRaises(ValueError) as context:
                MldpClient(ingestion_channel=None, config=None, config_file=None)
            
            self.assertIn("Either ingestion_channel or config must be provided", 
                         str(context.exception))
    
    @patch('dp_python_lib.config.config.MldpConfig.create_ingestion_channel')
    def test_mixed_channels_and_config(self, mock_ingestion_channel):
        """Test MldpClient with mix of explicit channels and config."""
        config = MldpConfig()
        query_channel = Mock()
        
        mock_ingestion_channel.return_value = self.mock_channel
        
        client = MldpClient(
            ingestion_channel=self.mock_channel,  # Explicit channel
            query_channel=query_channel,          # Explicit channel
            config=config                         # Config for annotation
        )
        
        # Should use explicit channels where provided
        self.assertEqual(client._ingestion_channel, self.mock_channel)
        self.assertEqual(client._query_channel, query_channel)
        # Should use config for annotation
        self.assertIsNotNone(client._annotation_channel)
        
        # Should not call create_ingestion_channel since explicit channel provided
        mock_ingestion_channel.assert_not_called()
    
    @patch('dp_python_lib.client.mldp_client.IngestionClient')
    def test_ingestion_client_creation(self, mock_ingestion_client_class):
        """Test that IngestionClient is properly created."""
        mock_ingestion_client = Mock()
        mock_ingestion_client_class.return_value = mock_ingestion_client
        
        client = MldpClient(ingestion_channel=self.mock_channel)
        
        mock_ingestion_client_class.assert_called_once_with(self.mock_channel)
        self.assertEqual(client.ingestion_client, mock_ingestion_client)


class TestMldpClientConfigIntegration(unittest.TestCase):
    """Integration tests for MldpClient with real configuration."""
    
    def test_config_from_yaml_file(self):
        """Test loading MldpClient from YAML configuration."""
        yaml_content = """
ingestion:
  host: test-ingestion.example.com
  port: 9001
  use_tls: false
query:
  host: test-query.example.com  
  port: 9002
annotation:
  host: test-annotation.example.com
  port: 9003
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            
            try:
                with patch('grpc.insecure_channel') as mock_insecure_channel:
                    mock_insecure_channel.return_value = Mock()
                    
                    client = MldpClient(config_file=f.name)
                    
                    # Verify channels were created with correct connection strings
                    expected_calls = [
                        unittest.mock.call('test-ingestion.example.com:9001'),
                        unittest.mock.call('test-query.example.com:9002'),
                        unittest.mock.call('test-annotation.example.com:9003')
                    ]
                    mock_insecure_channel.assert_has_calls(expected_calls, any_order=True)
                    
            finally:
                os.unlink(f.name)


if __name__ == '__main__':
    unittest.main()