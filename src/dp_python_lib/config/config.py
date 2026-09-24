import logging
from contextvars import ContextVar
from typing import Any

import grpc
from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# The flattened values of the YAML file being loaded by MldpConfig.from_yaml(), read by
# _YamlValuesSource.  A ContextVar rather than a class attribute so concurrent loads in other
# threads or asyncio tasks cannot see each other's values; from_yaml() resets it in a
# `finally`, so a failed load cannot leak file values into a later plain MldpConfig().
_yaml_values: ContextVar[dict[str, Any] | None] = ContextVar("_yaml_values", default=None)


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

    def connection_string(self) -> str:
        """Generate connection string for this service."""
        return f"{self.host}:{self.port}"

    def create_channel(self) -> grpc.Channel:
        """Create a gRPC channel for this service."""
        logger = logging.getLogger(__name__)
        connection_str = self.connection_string()

        if self.use_tls:
            logger.debug("Creating secure gRPC channel to %s", connection_str)
            return grpc.secure_channel(connection_str, grpc.ssl_channel_credentials())
        else:
            logger.debug("Creating insecure gRPC channel to %s", connection_str)
            return grpc.insecure_channel(connection_str)


class MldpConfig(BaseSettings):
    """Main configuration for MLDP client with environment variable support."""

    # Ingestion service configuration
    ingestion_host: str = "localhost"
    ingestion_port: int = 50051
    ingestion_use_tls: bool = False

    # Query service configuration
    query_host: str = "localhost"
    query_port: int = 50052
    query_use_tls: bool = False

    # Annotation service configuration
    annotation_host: str = "localhost"
    annotation_port: int = 50053
    annotation_use_tls: bool = False

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
        return ServiceConfig(host=self.ingestion_host, port=self.ingestion_port, use_tls=self.ingestion_use_tls)

    @property
    def query(self) -> ServiceConfig:
        """Get query service configuration."""
        return ServiceConfig(host=self.query_host, port=self.query_port, use_tls=self.query_use_tls)

    @property
    def annotation(self) -> ServiceConfig:
        """Get annotation service configuration."""
        return ServiceConfig(host=self.annotation_host, port=self.annotation_port, use_tls=self.annotation_use_tls)

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
                    if "host" in service_config:
                        flat_data[f"{service}_host"] = service_config["host"]
                        logger.debug("Loaded %s_host: %s", service, service_config["host"])
                    if "port" in service_config:
                        flat_data[f"{service}_port"] = service_config["port"]
                        logger.debug("Loaded %s_port: %s", service, service_config["port"])
                    if "use_tls" in service_config:
                        flat_data[f"{service}_use_tls"] = service_config["use_tls"]
                        logger.debug("Loaded %s_use_tls: %s", service, service_config["use_tls"])

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
