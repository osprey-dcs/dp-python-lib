import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))


from dp_python_lib.config import MldpConfig, ServiceConfig, load_config
from dp_python_lib.config.loader import find_config_file, get_default_config


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

    @patch("grpc.insecure_channel")
    def test_create_channel_insecure(self, mock_insecure_channel):
        """Test creating insecure gRPC channel."""
        config = ServiceConfig(host="localhost", port=50051, use_tls=False)
        mock_channel = unittest.mock.Mock()
        mock_insecure_channel.return_value = mock_channel

        channel = config.create_channel()

        mock_insecure_channel.assert_called_once_with("localhost:50051")
        self.assertEqual(channel, mock_channel)

    @patch("grpc.ssl_channel_credentials")
    @patch("grpc.secure_channel")
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

    @patch.dict(
        os.environ,
        {
            "MLDP_INGESTION_HOST": "prod-ingestion.example.com",
            "MLDP_INGESTION_PORT": "443",
            "MLDP_INGESTION_USE_TLS": "true",
            "MLDP_QUERY_HOST": "prod-query.example.com",
        },
    )
    def test_environment_variable_override(self):
        """Test that environment variables override default values."""
        config = MldpConfig()

        self.assertEqual(config.ingestion.host, "prod-ingestion.example.com")
        self.assertEqual(config.ingestion.port, 443)
        self.assertTrue(config.ingestion.use_tls)
        self.assertEqual(config.query.host, "prod-query.example.com")
        # Annotation should still have defaults
        self.assertEqual(config.annotation.host, "localhost")

    @patch.dict(os.environ, {"MLDP_INGESTION_USE_TLS": "1"})
    def test_environment_variable_boolean_parsing(self):
        """Test that various boolean string values are parsed correctly."""
        config = MldpConfig()
        self.assertTrue(config.ingestion.use_tls)

    @patch.dict(os.environ, {"MLDP_INGESTION_USE_TLS": "false"})
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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()

            try:
                config = MldpConfig.from_yaml(f.name)

                self.assertEqual(config.ingestion.host, "yaml-ingestion.example.com")
                self.assertEqual(config.ingestion.port, 9001)
                self.assertTrue(config.ingestion.use_tls)
                self.assertEqual(config.query.host, "yaml-query.example.com")
                self.assertEqual(config.query.port, 9002)
                self.assertEqual(config.annotation.host, "yaml-annotation.example.com")

            finally:
                os.unlink(f.name)

    def test_from_yaml_file_not_found(self):
        """Test loading config when YAML file doesn't exist."""
        config = MldpConfig.from_yaml("nonexistent-file.yaml")

        # Should return default config
        self.assertEqual(config.ingestion.host, "localhost")
        self.assertEqual(config.ingestion.port, 50051)

    def test_from_yaml_empty_file(self):
        """An empty YAML file parses as None and should fall back to defaults, not raise."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("")
            f.flush()

            try:
                config = MldpConfig.from_yaml(f.name)

                self.assertEqual(config.ingestion.host, "localhost")
                self.assertEqual(config.ingestion.port, 50051)
                self.assertEqual(config.query.port, 50052)
                self.assertEqual(config.annotation.port, 50053)

            finally:
                os.unlink(f.name)

    def test_from_yaml_comments_only(self):
        """A file of only comments also parses as None; same defaults fallback."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("# just a comment, nothing configured\n")
            f.flush()

            try:
                config = MldpConfig.from_yaml(f.name)

                self.assertEqual(config.ingestion.host, "localhost")
                self.assertEqual(config.ingestion.port, 50051)

            finally:
                os.unlink(f.name)

    def test_from_yaml_empty_service_section(self):
        """A section present but empty (`query:`) parses as None; skip it, keep other sections."""
        yaml_content = """
ingestion:
  host: yaml-ingestion.example.com
  port: 9001
query:
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()

            try:
                config = MldpConfig.from_yaml(f.name)

                self.assertEqual(config.ingestion.host, "yaml-ingestion.example.com")
                self.assertEqual(config.ingestion.port, 9001)
                # The empty section leaves query at its defaults.
                self.assertEqual(config.query.host, "localhost")
                self.assertEqual(config.query.port, 50052)

            finally:
                os.unlink(f.name)

    def test_from_yaml_non_mapping_top_level(self):
        """Valid YAML that isn't a mapping is a config error, not a silent defaults fallback."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("- ingestion\n- query\n")
            f.flush()

            try:
                with self.assertRaises(ValueError) as context:
                    MldpConfig.from_yaml(f.name)

                self.assertIn("expected a mapping", str(context.exception))

            finally:
                os.unlink(f.name)

    def test_from_yaml_invalid_yaml(self):
        """Test loading config with invalid YAML."""
        invalid_yaml = "invalid: yaml: content: ["

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(invalid_yaml)
            f.flush()

            try:
                with self.assertRaises(ValueError) as context:
                    MldpConfig.from_yaml(f.name)

                self.assertIn("Error loading configuration", str(context.exception))

            finally:
                os.unlink(f.name)

    @patch("dp_python_lib.config.config.ServiceConfig.create_channel")
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
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            try:
                result = find_config_file(f.name)
                self.assertEqual(result, f.name)
            finally:
                os.unlink(f.name)

    def test_find_config_file_explicit_not_found(self):
        """Test finding config file when explicit file doesn't exist."""
        with self.assertRaises(FileNotFoundError):
            find_config_file("nonexistent-file.yaml")

    @patch.dict(os.environ, {"MLDP_CONFIG_FILE": "/path/to/config.yaml"})
    @patch("os.path.exists")
    def test_find_config_file_environment_variable(self, mock_exists):
        """Test finding config file from environment variable."""
        mock_exists.return_value = True

        result = find_config_file()

        self.assertEqual(result, "/path/to/config.yaml")
        mock_exists.assert_called_with("/path/to/config.yaml")

    @patch("pathlib.Path.exists")
    @patch("os.path.exists")
    @patch("os.getenv")
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

    @patch("dp_python_lib.config.loader.find_config_file")
    def test_load_config_no_yaml_file(self, mock_find_config):
        """Test loading config when no YAML file found."""
        mock_find_config.return_value = None

        result = load_config()

        # Should return default config
        self.assertIsInstance(result, MldpConfig)
        self.assertEqual(result.ingestion.host, "localhost")

    def test_get_default_config(self):
        """Test getting default configuration."""
        config = get_default_config()

        self.assertIsInstance(config, MldpConfig)
        self.assertEqual(config.ingestion.host, "localhost")
        self.assertEqual(config.ingestion.port, 50051)


@contextmanager
def mldp_env(**overrides: str):
    """Run with every ambient ``MLDP_*`` variable removed, plus ``overrides``.

    A developer shell may export ``MLDP_*`` (to point integration tests elsewhere, say), and
    the precedence tests below assert exactly which source a value came from.
    """
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("MLDP_")}
    env.update(overrides)
    with patch.dict(os.environ, env, clear=True):
        yield


YAML_ALL_INGESTION = """
ingestion:
  host: yaml-host
  port: 9001
  use_tls: false
"""


class TestConfigPrecedence(unittest.TestCase):
    """Explicit > MLDP_* env > YAML file > defaults, driven through real files (issue #19)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def write_yaml(self, content: str) -> str:
        path = os.path.join(self._tmpdir.name, "mldp-config.yaml")
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_env_overrides_yaml_via_load_config(self):
        path = self.write_yaml(YAML_ALL_INGESTION)
        with mldp_env(MLDP_INGESTION_HOST="env-host"):
            config = load_config(config_file=path)
        self.assertEqual(config.ingestion.host, "env-host")
        # keys with no env var keep their YAML values
        self.assertEqual(config.ingestion.port, 9001)

    def test_env_overrides_yaml_via_from_yaml(self):
        path = self.write_yaml(YAML_ALL_INGESTION)
        with mldp_env(MLDP_INGESTION_HOST="env-host"):
            config = MldpConfig.from_yaml(path)
        self.assertEqual(config.ingestion.host, "env-host")
        self.assertEqual(config.ingestion.port, 9001)

    def test_yaml_overrides_defaults(self):
        path = self.write_yaml(YAML_ALL_INGESTION)
        with mldp_env():
            for config in (load_config(config_file=path), MldpConfig.from_yaml(path)):
                self.assertEqual(config.ingestion.host, "yaml-host")
                self.assertEqual(config.ingestion.port, 9001)

    def test_key_absent_from_yaml_falls_back_to_default_or_env(self):
        path = self.write_yaml("ingestion:\n  host: yaml-host\n")
        with mldp_env(MLDP_QUERY_HOST="env-query"):
            for config in (load_config(config_file=path), MldpConfig.from_yaml(path)):
                self.assertEqual(config.ingestion.port, 50051)  # default
                self.assertEqual(config.query.host, "env-query")  # env, key not in YAML

    def test_env_coerces_int_and_bool_over_yaml(self):
        path = self.write_yaml(YAML_ALL_INGESTION)
        with mldp_env(MLDP_INGESTION_PORT="443", MLDP_INGESTION_USE_TLS="true"):
            for config in (load_config(config_file=path), MldpConfig.from_yaml(path)):
                self.assertEqual(config.ingestion.port, 443)
                self.assertTrue(config.ingestion.use_tls)

    def test_lower_case_env_var_overrides_yaml(self):
        path = self.write_yaml(YAML_ALL_INGESTION)
        with mldp_env(mldp_ingestion_host="env-host"):
            self.assertEqual(MldpConfig.from_yaml(path).ingestion.host, "env-host")

    def test_mldp_config_file_with_env_override(self):
        path = self.write_yaml(YAML_ALL_INGESTION)
        with mldp_env(MLDP_CONFIG_FILE=path, MLDP_INGESTION_HOST="env-host"):
            config = load_config()
        self.assertEqual(config.ingestion.host, "env-host")
        self.assertEqual(config.ingestion.port, 9001)  # proves the file was the one selected

    def test_explicit_config_object_beats_env(self):
        with mldp_env(MLDP_INGESTION_HOST="env-host"):
            # Built inside the patched environment, so env was a candidate when it was constructed.
            custom_config = MldpConfig(ingestion_host="explicit")
            result = load_config(config_object=custom_config)
        self.assertIs(result, custom_config)
        self.assertEqual(result.ingestion.host, "explicit")

    def test_plain_config_after_failed_from_yaml_gets_defaults(self):
        path = self.write_yaml("ingestion:\n  host: yaml-host\n  port: not-a-port\n")
        with mldp_env():
            with self.assertRaises(ValueError):
                MldpConfig.from_yaml(path)
            config = MldpConfig()
        # the failed load's values must not leak into a later plain construction
        self.assertEqual(config.ingestion.host, "localhost")
        self.assertEqual(config.ingestion.port, 50051)

    def test_plain_config_after_successful_from_yaml_gets_defaults(self):
        path = self.write_yaml(YAML_ALL_INGESTION)
        with mldp_env():
            MldpConfig.from_yaml(path)
            self.assertEqual(MldpConfig().ingestion.host, "localhost")

    def test_invalid_yaml_value_overridden_by_env_loads(self):
        path = self.write_yaml("ingestion:\n  port: not-a-port\n")
        with mldp_env(MLDP_INGESTION_PORT="443"):
            config = MldpConfig.from_yaml(path)
        self.assertEqual(config.ingestion.port, 443)

    def test_explicit_kwarg_beats_env_and_yaml_source(self):
        with mldp_env(MLDP_INGESTION_HOST="env-host"):
            self.assertEqual(MldpConfig(ingestion_host="explicit").ingestion.host, "explicit")


if __name__ == "__main__":
    unittest.main()
