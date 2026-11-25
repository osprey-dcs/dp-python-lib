import logging
import math
import numbers
from contextvars import ContextVar
from typing import Any

import grpc
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# The flattened values of the YAML file being loaded by MldpConfig.from_yaml(), read by
# _YamlValuesSource.  A ContextVar rather than a class attribute so concurrent loads in other
# threads or asyncio tasks cannot see each other's values; from_yaml() resets it in a
# `finally`, so a failed load cannot leak file values into a later plain MldpConfig().
_yaml_values: ContextVar[dict[str, Any] | None] = ContextVar("_yaml_values", default=None)


# The library's timeout defaults (#81 D1, D3, Q1).  Literals rather than imports of the client constants
# (DEFAULT_CALL_TIMEOUT_SECONDS and friends), because importing dp_python_lib.client from here would import
# MldpClient, which imports this module.  test_config.py pins them equal.
_DEFAULT_CALL_TIMEOUT_SECONDS = 300.0
_DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 300.0
_DEFAULT_EXPORT_TIMEOUT_SECONDS = 3600.0

DEFAULT_KEEPALIVE_TIME_SECONDS = 60.0
"""Default interval between keepalive pings on a channel with a call in flight (#81 D6): twice dp-service's limit."""

DEFAULT_KEEPALIVE_TIMEOUT_SECONDS = 20.0
"""Default wait for a keepalive ping's ack before the channel is declared dead (#81 D6)."""

MIN_SERVER_KEEPALIVE_SECONDS = 30.0
"""
dp-service's ``GrpcServer.permitKeepAliveTimeSeconds``: the shortest client ping interval it accepts.  A client
pinging more often is sent GOAWAY ``too_many_pings``, which fails every call on the connection (#81 R5).
"""

# Per-service settings, in the order the YAML flattener reads them: each is `<service>_<key>` in MldpConfig and
# `<service>: {<key>: ...}` in the file.  A new per-service setting is a field plus an entry here.
_SERVICE_KEYS = (
    "host",
    "port",
    "use_tls",
    "call_timeout_seconds",
    "stream_idle_timeout_seconds",
    "keepalive_time_seconds",
    "keepalive_timeout_seconds",
)

# Settings only one service has, by service.
_SERVICE_ONLY_KEYS = {"annotation": ("export_timeout_seconds",)}


def channel_options(
    *,
    keepalive_time_seconds: float = DEFAULT_KEEPALIVE_TIME_SECONDS,
    keepalive_timeout_seconds: float = DEFAULT_KEEPALIVE_TIMEOUT_SECONDS,
) -> list[tuple[str, int]]:
    """
    grpcio channel options that turn on keepalive pings (#81 D5), for a channel this library does not build::

        channel = grpc.secure_channel(target, credentials, options=channel_options())

    Keepalive detects a dead network path (a dropped connection with no FIN or RST) on a channel with a call in
    flight: with the defaults, within keepalive_time_seconds + keepalive_timeout_seconds (80 s) of the drop.  It
    does not detect a server that is connected but never answers, since that server still acks pings; the
    library's deadlines and stream idle timeouts cover that.

    The options are the ones measured to do what their names suggest (#81 D6): ``grpc.http2.ping_timeout_ms`` is
    what bounds the wait for an ack in grpc-core (``grpc.keepalive_timeout_ms`` is set to the same value for a
    grpcio that honors it), and ``grpc.http2.max_pings_without_data`` is 0 so a quiet stream is not throttled to
    one ping a minute.  Pings are sent only while a call is in flight.

    A non-zero interval below MIN_SERVER_KEEPALIVE_SECONDS logs a WARNING: dp-service answers pings that frequent
    with GOAWAY ``too_many_pings``, failing every call on the connection.  It is not an error, because a
    deployment can lower the server's limit.

    :param keepalive_time_seconds: Interval between pings.  0 disables keepalive and returns no options.
    :param keepalive_timeout_seconds: How long to wait for a ping's ack.  Must be positive while keepalive is on.
    :return: A list of (option name, value) pairs for the ``options=`` argument of grpc.insecure_channel() or
        grpc.secure_channel(); empty when keepalive is disabled.
    :raises ValueError: if either value is negative or not finite, or keepalive_timeout_seconds is 0 while
        keepalive is on.
    """
    _check_seconds(keepalive_time_seconds, "keepalive_time_seconds")
    _check_seconds(keepalive_timeout_seconds, "keepalive_timeout_seconds")
    if keepalive_time_seconds == 0:
        return []
    if keepalive_timeout_seconds == 0:
        raise ValueError(
            "keepalive_timeout_seconds must be positive while keepalive is on; "
            "set keepalive_time_seconds to 0 to disable keepalive"
        )
    if keepalive_time_seconds < MIN_SERVER_KEEPALIVE_SECONDS:
        logging.getLogger(__name__).warning(
            "keepalive_time_seconds=%g is below dp-service's default minimum ping interval of %g s "
            "(GrpcServer.permitKeepAliveTimeSeconds); unless the server's limit was lowered, it will answer with "
            "GOAWAY too_many_pings and fail every call on the connection",
            keepalive_time_seconds,
            MIN_SERVER_KEEPALIVE_SECONDS,
        )
    # int(...): grpcio needs a Python int, and round() of a NumPy number returns a NumPy one.
    time_ms = max(1, int(round(float(keepalive_time_seconds) * 1000)))
    timeout_ms = max(1, int(round(float(keepalive_timeout_seconds) * 1000)))
    return [
        ("grpc.keepalive_time_ms", time_ms),
        ("grpc.keepalive_timeout_ms", timeout_ms),
        ("grpc.http2.ping_timeout_ms", timeout_ms),
        ("grpc.http2.max_pings_without_data", 0),
    ]


def _check_seconds(value: float, name: str) -> None:
    # numbers.Real, so a NumPy integer is accepted like a Python int; bool is excluded explicitly.
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(f"{name} must be a number of seconds, got {type(value).__name__}")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a non-negative number of seconds (0 disables it), got {value!r}")


def _seconds_field(default: float) -> Any:
    """A timeout setting: seconds, 0 disables, negative or non-finite is a validation error (#81 D4)."""
    return Field(default=default, ge=0, allow_inf_nan=False)


def _ping_timeout_field() -> Any:
    """
    keepalive_timeout_seconds: positive, the one timeout setting where 0 is an error rather than "disabled", since
    a ping timeout has nothing to disable; keepalive itself is turned off with keepalive_time_seconds = 0.
    """
    return Field(default=DEFAULT_KEEPALIVE_TIMEOUT_SECONDS, gt=0, allow_inf_nan=False)


class _YamlValuesSource(PydanticBaseSettingsSource):
    """Settings source supplying the YAML file's values, ranked below environment variables (issue #19)."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Required by the ABC; __call__ supplies every value at once, so this is never used.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(_yaml_values.get() or {})


class ServiceConfig(BaseModel):
    """Configuration for a single gRPC service."""

    host: str = "localhost"
    port: int = 50051
    use_tls: bool = False
    call_timeout_seconds: float = _seconds_field(_DEFAULT_CALL_TIMEOUT_SECONDS)
    stream_idle_timeout_seconds: float = _seconds_field(_DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS)
    keepalive_time_seconds: float = _seconds_field(DEFAULT_KEEPALIVE_TIME_SECONDS)
    keepalive_timeout_seconds: float = _ping_timeout_field()

    def connection_string(self) -> str:
        """Generate connection string for this service."""
        return f"{self.host}:{self.port}"

    def channel_options(self) -> list[tuple[str, int]]:
        """The grpcio options create_channel() passes: this service's keepalive settings (see channel_options())."""
        return channel_options(
            keepalive_time_seconds=self.keepalive_time_seconds,
            keepalive_timeout_seconds=self.keepalive_timeout_seconds,
        )

    def create_channel(self) -> grpc.Channel:
        """Create a gRPC channel for this service, with keepalive on unless keepalive_time_seconds is 0."""
        logger = logging.getLogger(__name__)
        connection_str = self.connection_string()
        options = self.channel_options()

        if self.use_tls:
            logger.debug("Creating secure gRPC channel to %s with options %s", connection_str, options)
            return grpc.secure_channel(connection_str, grpc.ssl_channel_credentials(), options=options)
        else:
            logger.debug("Creating insecure gRPC channel to %s with options %s", connection_str, options)
            return grpc.insecure_channel(connection_str, options=options)


class MldpConfig(BaseSettings):
    """Main configuration for MLDP client with environment variable support."""

    # Ingestion service configuration
    ingestion_host: str = "localhost"
    ingestion_port: int = 50051
    ingestion_use_tls: bool = False
    ingestion_call_timeout_seconds: float = _seconds_field(_DEFAULT_CALL_TIMEOUT_SECONDS)
    ingestion_stream_idle_timeout_seconds: float = _seconds_field(_DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS)
    ingestion_keepalive_time_seconds: float = _seconds_field(DEFAULT_KEEPALIVE_TIME_SECONDS)
    ingestion_keepalive_timeout_seconds: float = _ping_timeout_field()

    # Query service configuration
    query_host: str = "localhost"
    query_port: int = 50052
    query_use_tls: bool = False
    query_call_timeout_seconds: float = _seconds_field(_DEFAULT_CALL_TIMEOUT_SECONDS)
    query_stream_idle_timeout_seconds: float = _seconds_field(_DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS)
    query_keepalive_time_seconds: float = _seconds_field(DEFAULT_KEEPALIVE_TIME_SECONDS)
    query_keepalive_timeout_seconds: float = _ping_timeout_field()

    # Annotation service configuration
    annotation_host: str = "localhost"
    annotation_port: int = 50053
    annotation_use_tls: bool = False
    annotation_call_timeout_seconds: float = _seconds_field(_DEFAULT_CALL_TIMEOUT_SECONDS)
    annotation_stream_idle_timeout_seconds: float = _seconds_field(_DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS)
    annotation_keepalive_time_seconds: float = _seconds_field(DEFAULT_KEEPALIVE_TIME_SECONDS)
    annotation_keepalive_timeout_seconds: float = _ping_timeout_field()
    # exportData replies only once the file is written, so it gets its own, longer deadline (#81 Q1).
    annotation_export_timeout_seconds: float = _seconds_field(_DEFAULT_EXPORT_TIMEOUT_SECONDS)

    # env_ignore_empty: an empty MLDP_* variable counts as unset, so it falls through to the YAML file or
    # the default.  Without it, `export MLDP_INGESTION_HOST=` (or a compose `${VAR}` that expands to "")
    # would beat the file with an empty host and connect nowhere, or fail to parse as a port.
    model_config = SettingsConfigDict(env_prefix="MLDP_", case_sensitive=False, env_ignore_empty=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority, high to low: explicit constructor arguments, MLDP_* environment variables,
        # the YAML file, field defaults.  This is pydantic-settings' default order with the YAML
        # source added last.  YAML values must never be passed as constructor arguments: init
        # kwargs outrank every other source, which is how the file came to silently beat
        # MLDP_* variables before issue #19.
        return (init_settings, env_settings, dotenv_settings, file_secret_settings, _YamlValuesSource(settings_cls))

    @property
    def ingestion(self) -> ServiceConfig:
        """Get ingestion service configuration."""
        return self._service_config("ingestion")

    @property
    def query(self) -> ServiceConfig:
        """Get query service configuration."""
        return self._service_config("query")

    @property
    def annotation(self) -> ServiceConfig:
        """Get annotation service configuration."""
        return self._service_config("annotation")

    def _service_config(self, service: str) -> ServiceConfig:
        return ServiceConfig(**{key: getattr(self, f"{service}_{key}") for key in _SERVICE_KEYS})

    @staticmethod
    def _setting_source(service: str, key: str) -> str:
        """
        How a timeout error names the setting a client default came from (#81 R7), for MldpClient to pass as a
        client's ``*_source``: "set by MLDP_QUERY_CALL_TIMEOUT_SECONDS / query.call_timeout_seconds".
        """
        return f"set by MLDP_{service.upper()}_{key.upper()} / {service}.{key}"

    @classmethod
    def from_yaml(cls, yaml_file: str) -> "MldpConfig":
        """Load configuration from YAML file.

        ``MLDP_*`` environment variables override values from the file; keys the file leaves
        out fall back to the environment, then to the field defaults.
        """
        import yaml

        logger = logging.getLogger(__name__)

        try:
            logger.info("Loading configuration from YAML file: %s", yaml_file)
            with open(yaml_file) as f:
                data = yaml.safe_load(f)

            # safe_load() returns None for an empty file, and can return any YAML type for a
            # malformed one.  Treat anything that isn't a mapping as "nothing configured" so an
            # empty config file cleanly falls back to defaults instead of raising.
            if data is None:
                logger.warning("YAML configuration file is empty: %s, using defaults", yaml_file)
                data = {}
            elif not isinstance(data, dict):
                raise ValueError(f"expected a mapping at the top level, got {type(data).__name__}")

            # Convert nested YAML structure to flat fields
            flat_data: dict[str, Any] = {}

            for service in ["ingestion", "query", "annotation"]:
                service_config = data.get(service)
                # A section present but empty (`ingestion:`) parses as None; skip it rather than
                # failing the membership tests below.
                if isinstance(service_config, dict):
                    for key in _SERVICE_KEYS + _SERVICE_ONLY_KEYS.get(service, ()):
                        if key in service_config:
                            flat_data[f"{service}_{key}"] = service_config[key]
                            logger.debug("Loaded %s_%s: %s", service, key, service_config[key])

            logger.debug("Successfully loaded configuration from YAML, creating MldpConfig instance")
            token = _yaml_values.set(flat_data)
            try:
                return cls()
            finally:
                _yaml_values.reset(token)

        except FileNotFoundError:
            logger.warning("YAML configuration file not found: %s, using defaults", yaml_file)
            return cls()
        except Exception as e:
            logger.exception("Error loading configuration from %s: %s", yaml_file, e)
            raise ValueError(f"Error loading configuration from {yaml_file}: {e}") from e

    def create_ingestion_channel(self) -> grpc.Channel:
        """Create gRPC channel for ingestion service."""
        logger = logging.getLogger(__name__)
        logger.debug("Creating ingestion channel")
        return self.ingestion.create_channel()

    def create_query_channel(self) -> grpc.Channel:
        """Create gRPC channel for query service."""
        logger = logging.getLogger(__name__)
        logger.debug("Creating query channel")
        return self.query.create_channel()

    def create_annotation_channel(self) -> grpc.Channel:
        """Create gRPC channel for annotation service."""
        logger = logging.getLogger(__name__)
        logger.debug("Creating annotation channel")
        return self.annotation.create_channel()
