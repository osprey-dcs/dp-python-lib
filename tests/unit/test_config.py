import unittest
from unittest.mock import patch, mock_open
import tempfile
import os
import sys
from pathlib import Path

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from dp_python_lib.config import ServiceConfig, MldpConfig, load_config
from dp_python_lib.config.loader import find_config_file, get_default_config
import grpc


class TestServiceConfig(unittest.TestCase):
    
    def test_service_config_defaults(self):
        """Test ServiceConfig with default values."""
        config = ServiceConfig()
        
        self.assertEqual(config.host, "localhost")
        self.assertEqual(config.port, 50051)
        self.assertFalse(config.use_tls)
    
    def test_service_config_custom_values(self):
        """Test ServiceConfig with custom values."""
        config = ServiceConfig(host="example.com", port=443, use_tls=True)
        
        self.assertEqual(config.host, "example.com")
        self.assertEqual(config.port, 443)
        self.assertTrue(config.use_tls)
    
    def test_connection_string(self):
        """Test connection string generation."""
        config = ServiceConfig(host="test.example.com", port=8080)
        
        self.assertEqual(config.connection_string(), "test.example.com:8080")
    
    @patch('grpc.insecure_channel')
    def test_create_channel_insecure(self, mock_insecure_channel):
        """Test creating insecure gRPC channel."""
        config = ServiceConfig(host="localhost", port=50051, use_tls=False)
        mock_channel = unittest.mock.Mock()
        mock_insecure_channel.return_value = mock_channel
        
        channel = config.create_channel()
        
        mock_insecure_channel.assert_called_once_with("localhost:50051")
        self.assertEqual(channel, mock_channel)
    
    @patch('grpc.ssl_channel_credentials')
    @patch('grpc.secure_channel')
    def test_create_channel_secure(self, mock_secure_channel, mock_ssl_creds):
        """Test creating secure gRPC channel."""
        config = ServiceConfig(host="secure.example.com", port=443, use_tls=True)
        mock_channel = unittest.mock.Mock()
        mock_creds = unittest.mock.Mock()
        mock_secure_channel.return_value = mock_channel
        mock_ssl_creds.return_value = mock_creds
        
        channel = config.create_channel()
        
        mock_ssl_creds.assert_called_once()
        mock_secure_channel.assert_called_once_with("secure.example.com:443", mock_creds)
        self.assertEqual(channel, mock_channel)


class TestMldpConfig(unittest.TestCase):
    
    def test_mldp_config_defaults(self):
        """Test MldpConfig with default values."""
        config = MldpConfig()
        
        self.assertEqual(config.ingestion.host, "localhost")
        self.assertEqual(config.ingestion.port, 50051)
        self.assertEqual(config.query.port, 50052)
        self.assertEqual(config.annotation.port, 50053)
        self.assertFalse(config.ingestion.use_tls)
    
    @patch.dict(os.environ, {
        'MLDP_INGESTION_HOST': 'prod-ingestion.example.com',
        'MLDP_INGESTION_PORT': '443', 
        'MLDP_INGESTION_USE_TLS': 'true',
        'MLDP_QUERY_HOST': 'prod-query.example.com'
    })
    def test_environment_variable_override(self):
        """Test that environment variables override default values."""
        config = MldpConfig()
        
        self.assertEqual(config.ingestion.host, 'prod-ingestion.example.com')
        self.assertEqual(config.ingestion.port, 443)
        self.assertTrue(config.ingestion.use_tls)
        self.assertEqual(config.query.host, 'prod-query.example.com')
        # Annotation should still have defaults
        self.assertEqual(config.annotation.host, 'localhost')

    @patch.dict(os.environ, {'MLDP_INGESTION_USE_TLS': '1'})
    def test_environment_variable_boolean_parsing(self):
        """Test that various boolean string values are parsed correctly."""
        config = MldpConfig()
        self.assertTrue(config.ingestion.use_tls)
        
    @patch.dict(os.environ, {'MLDP_INGESTION_USE_TLS': 'false'}) 
    def test_environment_variable_boolean_false(self):
        """Test that false values are parsed correctly."""
        config = MldpConfig()
        self.assertFalse(config.ingestion.use_tls)
    
    def test_from_yaml_valid(self):
        """Test loading config from valid YAML."""
        yaml_content = """
ingestion:
  host: yaml-ingestion.example.com
  port: 9001
  use_tls: true
query:
  host: yaml-query.example.com
  port: 9002
annotation:
  host: yaml-annotation.example.com
  port: 9003
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            
            try:
                config = MldpConfig.from_yaml(f.name)
                
                self.assertEqual(config.ingestion.host, 'yaml-ingestion.example.com')
                self.assertEqual(config.ingestion.port, 9001)
                self.assertTrue(config.ingestion.use_tls)
                self.assertEqual(config.query.host, 'yaml-query.example.com')
                self.assertEqual(config.query.port, 9002)
                self.assertEqual(config.annotation.host, 'yaml-annotation.example.com')
                
            finally:
                os.unlink(f.name)
    
    def test_from_yaml_file_not_found(self):
        """Test loading config when YAML file doesn't exist."""
        config = MldpConfig.from_yaml('nonexistent-file.yaml')
        
        # Should return default config
        self.assertEqual(config.ingestion.host, 'localhost')
        self.assertEqual(config.ingestion.port, 50051)
    
    def test_from_yaml_invalid_yaml(self):
        """Test loading config with invalid YAML."""
        invalid_yaml = "invalid: yaml: content: ["
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(invalid_yaml)
            f.flush()
            
            try:
                with self.assertRaises(ValueError) as context:
                    MldpConfig.from_yaml(f.name)
                
                self.assertIn("Error loading configuration", str(context.exception))
                
            finally:
                os.unlink(f.name)
    
    @patch('dp_python_lib.config.config.ServiceConfig.create_channel')
    def test_create_channels(self, mock_create_channel):
        """Test creating gRPC channels from config."""
        mock_channel = unittest.mock.Mock()
        mock_create_channel.return_value = mock_channel
        
        config = MldpConfig()
        
        ingestion_channel = config.create_ingestion_channel()
        query_channel = config.create_query_channel()
        annotation_channel = config.create_annotation_channel()
        
        self.assertEqual(ingestion_channel, mock_channel)
        self.assertEqual(query_channel, mock_channel)
        self.assertEqual(annotation_channel, mock_channel)
        self.assertEqual(mock_create_channel.call_count, 3)


class TestConfigLoader(unittest.TestCase):
    
    def test_find_config_file_explicit(self):
        """Test finding config file when explicitly provided."""
        with tempfile.NamedTemporaryFile(suffix='.yaml', delete=False) as f:
            try:
                result = find_config_file(f.name)
                self.assertEqual(result, f.name)
            finally:
                os.unlink(f.name)
    
    def test_find_config_file_explicit_not_found(self):
        """Test finding config file when explicit file doesn't exist."""
        with self.assertRaises(FileNotFoundError):
            find_config_file('nonexistent-file.yaml')
    
    @patch.dict(os.environ, {'MLDP_CONFIG_FILE': '/path/to/config.yaml'})
    @patch('os.path.exists')
    def test_find_config_file_environment_variable(self, mock_exists):
        """Test finding config file from environment variable."""
        mock_exists.return_value = True
        
        result = find_config_file()
        
        self.assertEqual(result, '/path/to/config.yaml')
        mock_exists.assert_called_with('/path/to/config.yaml')
    
    @patch('pathlib.Path.exists')
    @patch('os.path.exists') 
    @patch('os.getenv')
    def test_find_config_file_none_found(self, mock_getenv, mock_path_exists, mock_pathlib_exists):
        """Test when no config file is found."""
        mock_getenv.return_value = None  # No MLDP_CONFIG_FILE env var
        mock_path_exists.return_value = False  # No config files exist
        mock_pathlib_exists.return_value = False  # No pathlib files exist
        
        result = find_config_file()
        
        # Should return None when no config file found
        self.assertIsNone(result)
    
    def test_load_config_with_explicit_object(self):
        """Test loading config with explicit config object."""
        custom_config = MldpConfig(ingestion_host="custom-host")
        
        result = load_config(config_object=custom_config)
        
        self.assertEqual(result.ingestion.host, "custom-host")
    
    @patch('dp_python_lib.config.loader.find_config_file')
    @patch('dp_python_lib.config.config.MldpConfig.from_yaml')
    def test_load_config_from_yaml(self, mock_from_yaml, mock_find_config):
        """Test loading config from YAML file."""
        mock_config = MldpConfig()
        mock_find_config.return_value = 'test-config.yaml'
        mock_from_yaml.return_value = mock_config
        
        result = load_config()
        
        mock_find_config.assert_called_once_with(None)
        mock_from_yaml.assert_called_once_with('test-config.yaml')
        self.assertEqual(result, mock_config)
    
    @patch('dp_python_lib.config.loader.find_config_file')
    def test_load_config_no_yaml_file(self, mock_find_config):
        """Test loading config when no YAML file found."""
        mock_find_config.return_value = None
        
        result = load_config()
        
        # Should return default config
        self.assertIsInstance(result, MldpConfig)
        self.assertEqual(result.ingestion.host, 'localhost')
    
    def test_get_default_config(self):
        """Test getting default configuration."""
        config = get_default_config()
        
        self.assertIsInstance(config, MldpConfig)
        self.assertEqual(config.ingestion.host, 'localhost')
        self.assertEqual(config.ingestion.port, 50051)


if __name__ == '__main__':
    unittest.main()