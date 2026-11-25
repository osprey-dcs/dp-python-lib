import os
import sys
import tempfile
import unittest
from unittest.mock import patch

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))


from mldp_env import isolate_mldp_env, mldp_env
from pydantic import ValidationError

from dp_python_lib.config import (
    DEFAULT_KEEPALIVE_TIME_SECONDS,
    DEFAULT_KEEPALIVE_TIMEOUT_SECONDS,
    MIN_SERVER_KEEPALIVE_SECONDS,
    MldpConfig,
    ServiceConfig,
    channel_options,
    load_config,
)
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

        mock_insecure_channel.assert_called_once_with("localhost:50051", options=channel_options())
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
        mock_secure_channel.assert_called_once_with("secure.example.com:443", mock_creds, options=channel_options())
        self.assertEqual(channel, mock_channel)


class TestTimeoutDefaults(unittest.TestCase):
    def test_config_defaults_match_the_client_constants(self):
        # config.py spells these as literals to avoid an import cycle with dp_python_lib.client (#81 T5).
        from dp_python_lib.client.export_client import DEFAULT_EXPORT_TIMEOUT_SECONDS
        from dp_python_lib.client.service_api_client_base import (
            DEFAULT_CALL_TIMEOUT_SECONDS,
            DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
        )

        isolate_mldp_env(self)
        config = MldpConfig()
        for service in ("ingestion", "query", "annotation"):
            with self.subTest(service=service):
                self.assertEqual(getattr(config, f"{service}_call_timeout_seconds"), DEFAULT_CALL_TIMEOUT_SECONDS)
                self.assertEqual(
                    getattr(config, f"{service}_stream_idle_timeout_seconds"), DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
                )
        self.assertEqual(config.annotation_export_timeout_seconds, DEFAULT_EXPORT_TIMEOUT_SECONDS)
        self.assertEqual(ServiceConfig().call_timeout_seconds, DEFAULT_CALL_TIMEOUT_SECONDS)


# (service, key) for every #81 setting.  export_timeout_seconds exists on the annotation service only.
_SERVICES = ("ingestion", "query", "annotation")
_PER_SERVICE_KEYS = (
    "call_timeout_seconds",
    "stream_idle_timeout_seconds",
    "keepalive_time_seconds",
    "keepalive_timeout_seconds",
)
_TIMEOUT_SETTINGS = [(svc, key) for svc in _SERVICES for key in _PER_SERVICE_KEYS] + [
    ("annotation", "export_timeout_seconds")
]
# Settings where 0 disables; a ping timeout has nothing to disable and must be positive (#81 T5).
_ZERO_DISABLES = [(svc, key) for svc, key in _TIMEOUT_SETTINGS if key != "keepalive_timeout_seconds"]

_CONFIG_LOGGER = "dp_python_lib.config.config"


def _value(config: MldpConfig, service: str, key: str) -> float:
    """The setting as MldpConfig stores it (the flat field every other reader derives from)."""
    return getattr(config, f"{service}_{key}")


class TestTimeoutAndKeepaliveSettings(unittest.TestCase):
    """The #81 settings through init / env / YAML, with the same precedence as host and port."""

    def setUp(self):
        isolate_mldp_env(self)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def write_yaml(self, service: str, key: str, value: object) -> str:
        path = os.path.join(self._tmpdir.name, "mldp-config.yaml")
        with open(path, "w") as f:
            f.write(f"{service}:\n  {key}: {value}\n")
        return path

    def test_yaml_sets_each_setting(self):
        for service, key in _TIMEOUT_SETTINGS:
            with self.subTest(service=service, key=key):
                path = self.write_yaml(service, key, 11.5)
                for config in (load_config(config_file=path), MldpConfig.from_yaml(path)):
                    self.assertEqual(_value(config, service, key), 11.5)

    def test_env_beats_yaml_for_each_setting(self):
        for service, key in _TIMEOUT_SETTINGS:
            with self.subTest(service=service, key=key):
                path = self.write_yaml(service, key, 11)
                with mldp_env(**{f"MLDP_{service.upper()}_{key.upper()}": "22"}):
                    for config in (load_config(config_file=path), MldpConfig.from_yaml(path)):
                        self.assertEqual(_value(config, service, key), 22)

    def test_init_kwarg_beats_env_for_each_setting(self):
        for service, key in _TIMEOUT_SETTINGS:
            with self.subTest(service=service, key=key):
                with mldp_env(**{f"MLDP_{service.upper()}_{key.upper()}": "22"}):
                    config = MldpConfig(**{f"{service}_{key}": 33})
                self.assertEqual(_value(config, service, key), 33)

    def test_service_config_carries_the_per_service_settings(self):
        for service, key in _TIMEOUT_SETTINGS:
            if key == "export_timeout_seconds":
                continue  # a deadline for one annotation method, not a channel or client-wide setting
            with self.subTest(service=service, key=key):
                config = MldpConfig(**{f"{service}_{key}": 7})
                self.assertEqual(getattr(getattr(config, service), key), 7)
                # the other services keep their defaults
                for other in _SERVICES:
                    if other != service:
                        self.assertNotEqual(getattr(getattr(config, other), key), 7)

    def test_zero_is_accepted_where_it_disables(self):
        for service, key in _ZERO_DISABLES:
            with self.subTest(service=service, key=key):
                path = self.write_yaml(service, key, 0)
                self.assertEqual(_value(MldpConfig.from_yaml(path), service, key), 0)

    def test_zero_keepalive_timeout_is_rejected(self):
        for service in _SERVICES:
            with self.subTest(service=service):
                path = self.write_yaml(service, "keepalive_timeout_seconds", 0)
                # from_yaml() re-raises a ValidationError as a ValueError naming the file; the field is in it
                with self.assertRaisesRegex(ValueError, f"{service}_keepalive_timeout_seconds"):
                    MldpConfig.from_yaml(path)
                with (
                    mldp_env(**{f"MLDP_{service.upper()}_KEEPALIVE_TIMEOUT_SECONDS": "0"}),
                    self.assertRaises(ValidationError),
                ):
                    MldpConfig()
        with self.assertRaises(ValidationError):
            ServiceConfig(keepalive_timeout_seconds=0)

    def test_negative_and_non_finite_values_are_rejected(self):
        for service, key in _TIMEOUT_SETTINGS:
            for bad in ("-1", "inf", "nan"):
                with self.subTest(service=service, key=key, value=bad):
                    path = self.write_yaml(service, key, f".{bad}" if bad != "-1" else bad)  # YAML spells .inf/.nan
                    with self.assertRaisesRegex(ValueError, f"{service}_{key}"):
                        MldpConfig.from_yaml(path)
                    with (
                        mldp_env(**{f"MLDP_{service.upper()}_{key.upper()}": bad}),
                        self.assertRaises(ValidationError),
                    ):
                        MldpConfig()
        for key in _PER_SERVICE_KEYS:
            with self.subTest(service_config=key), self.assertRaises(ValidationError):
                ServiceConfig(**{key: -1})

    def test_invalid_yaml_value_overridden_by_env_loads(self):
        # Validation runs on the merged result (#19), so a bad file value an env var replaces is not an error.
        path = self.write_yaml("query", "call_timeout_seconds", -5)
        with mldp_env(MLDP_QUERY_CALL_TIMEOUT_SECONDS="45"):
            self.assertEqual(MldpConfig.from_yaml(path).query.call_timeout_seconds, 45)

    def test_keepalive_settings_reach_the_channel_options(self):
        config = MldpConfig(query_keepalive_time_seconds=45, query_keepalive_timeout_seconds=7.5)
        self.assertEqual(
            config.query.channel_options(),
            channel_options(keepalive_time_seconds=45, keepalive_timeout_seconds=7.5),
        )
        self.assertEqual(config.ingestion.channel_options(), channel_options())
        self.assertEqual(MldpConfig(annotation_keepalive_time_seconds=0).annotation.channel_options(), [])

    @patch("grpc.insecure_channel")
    def test_create_channel_without_keepalive_passes_no_options(self, mock_insecure_channel):
        ServiceConfig(host="h", port=1, keepalive_time_seconds=0).create_channel()
        mock_insecure_channel.assert_called_once_with("h:1", options=[])


class TestChannelOptions(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(DEFAULT_KEEPALIVE_TIME_SECONDS, 60.0)
        self.assertEqual(DEFAULT_KEEPALIVE_TIMEOUT_SECONDS, 20.0)
        self.assertEqual(
            channel_options(),
            [
                ("grpc.keepalive_time_ms", 60_000),
                ("grpc.keepalive_timeout_ms", 20_000),
                # the option grpc-core actually bounds the ack wait with (#81 D6); same value as the one above
                ("grpc.http2.ping_timeout_ms", 20_000),
                # without it a quiet stream is throttled to one ping a minute after two pings
                ("grpc.http2.max_pings_without_data", 0),
            ],
        )

    def test_custom_values_convert_to_whole_milliseconds(self):
        options = dict(channel_options(keepalive_time_seconds=45.0004, keepalive_timeout_seconds=2.5))
        self.assertEqual(options["grpc.keepalive_time_ms"], 45_000)
        self.assertEqual(options["grpc.keepalive_timeout_ms"], 2_500)
        self.assertEqual(options["grpc.http2.ping_timeout_ms"], 2_500)
        for value in options.values():
            self.assertIs(type(value), int)

    def test_numpy_values_are_accepted_and_become_python_ints(self):
        # grpcio needs Python ints, and round() of a NumPy number returns a NumPy one.
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed (the [analysis] extra)")
        options = dict(channel_options(keepalive_time_seconds=np.int64(45), keepalive_timeout_seconds=np.float64(2.5)))
        self.assertEqual(options["grpc.keepalive_time_ms"], 45_000)
        self.assertEqual(options["grpc.http2.ping_timeout_ms"], 2_500)
        for value in options.values():
            self.assertIs(type(value), int)

    def test_sub_millisecond_values_round_up_to_one_millisecond(self):
        # 0 ms would mean something else to grpc-core (or be rejected), not "very short".
        with self.assertLogs(_CONFIG_LOGGER, "WARNING"):
            options = dict(channel_options(keepalive_time_seconds=0.0001, keepalive_timeout_seconds=0.0001))
        self.assertEqual(options["grpc.keepalive_time_ms"], 1)
        self.assertEqual(options["grpc.http2.ping_timeout_ms"], 1)

    def test_zero_interval_disables_keepalive(self):
        self.assertEqual(channel_options(keepalive_time_seconds=0), [])
        # the ping timeout is irrelevant once keepalive is off, so 0 there is not an error
        self.assertEqual(channel_options(keepalive_time_seconds=0, keepalive_timeout_seconds=0), [])

    def test_zero_ping_timeout_with_keepalive_on_raises(self):
        with self.assertRaisesRegex(ValueError, "keepalive_timeout_seconds must be positive"):
            channel_options(keepalive_timeout_seconds=0)

    def test_invalid_values_raise_naming_the_argument(self):
        for name in ("keepalive_time_seconds", "keepalive_timeout_seconds"):
            for bad in (-1, float("inf"), float("nan")):
                with self.subTest(name=name, value=bad), self.assertRaisesRegex(ValueError, name):
                    channel_options(**{name: bad})
            for bad in ("60", None, True):
                with self.subTest(name=name, value=bad), self.assertRaisesRegex(TypeError, name):
                    channel_options(**{name: bad})

    def test_warning_below_the_server_minimum(self):
        self.assertEqual(MIN_SERVER_KEEPALIVE_SECONDS, 30.0)
        with self.assertLogs(_CONFIG_LOGGER, "WARNING") as logs:
            channel_options(keepalive_time_seconds=10)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("too_many_pings", logs.output[0])
        self.assertIn("keepalive_time_seconds=10", logs.output[0])

    def test_no_warning_at_the_minimum_or_when_disabled(self):
        for interval in (MIN_SERVER_KEEPALIVE_SECONDS, DEFAULT_KEEPALIVE_TIME_SECONDS, 0):
            with self.subTest(interval=interval), self.assertNoLogs(_CONFIG_LOGGER, "WARNING"):
                channel_options(keepalive_time_seconds=interval)

    def test_service_config_warns_through_create_channel(self):
        with patch("grpc.insecure_channel"), self.assertLogs(_CONFIG_LOGGER, "WARNING"):
            ServiceConfig(keepalive_time_seconds=10).create_channel()


class TestMldpConfig(unittest.TestCase):
    def setUp(self):
        isolate_mldp_env(self)

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
    def setUp(self):
        isolate_mldp_env(self)

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

    def test_empty_env_var_falls_through_to_yaml(self):
        path = self.write_yaml(YAML_ALL_INGESTION)
        with mldp_env(MLDP_INGESTION_HOST="", MLDP_INGESTION_PORT="", MLDP_INGESTION_USE_TLS=""):
            for config in (load_config(config_file=path), MldpConfig.from_yaml(path)):
                self.assertEqual(config.ingestion.host, "yaml-host")
                self.assertEqual(config.ingestion.port, 9001)
                self.assertFalse(config.ingestion.use_tls)

    def test_empty_env_var_falls_through_to_default(self):
        with mldp_env(MLDP_QUERY_HOST="", MLDP_QUERY_PORT=""):
            config = MldpConfig()
        self.assertEqual(config.query.host, "localhost")
        self.assertEqual(config.query.port, 50052)

    def test_explicit_kwarg_beats_env_and_yaml_source(self):
        with mldp_env(MLDP_INGESTION_HOST="env-host"):
            self.assertEqual(MldpConfig(ingestion_host="explicit").ingestion.host, "explicit")


if __name__ == "__main__":
    unittest.main()
