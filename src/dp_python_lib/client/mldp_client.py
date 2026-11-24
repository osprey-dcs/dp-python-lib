import logging
from typing import Any

import grpc

from dp_python_lib.client.annotation_client import AnnotationClient
from dp_python_lib.client.ingestion_client import IngestionClient
from dp_python_lib.client.query_client import QueryClient
from dp_python_lib.config import MldpConfig, load_config


def _service_timeouts(config: MldpConfig | None, service: str) -> dict[str, Any]:
    """
    The timeout keyword arguments for one service's client(s): the configured values, each with the setting it
    came from for the DEADLINE_EXCEEDED hint (#81 R7), or nothing without a config, leaving the library defaults.
    """
    if config is None:
        return {}
    timeouts: dict[str, Any] = {}
    for kwarg, key in (
        ("call_timeout", "call_timeout_seconds"),
        ("stream_idle_timeout", "stream_idle_timeout_seconds"),
    ):
        timeouts[kwarg] = getattr(config, f"{service}_{key}")
        timeouts[f"{kwarg}_source"] = config._setting_source(service, key)
    return timeouts


class MldpClient:
    """
    This is the main user-facing API client class.  It provides variables for accessing the Ingestion, Query,
    and Annotation Service API clients, respectively.
    """

    def __init__(
        self,
        ingestion_channel: grpc.Channel | None = None,
        query_channel: grpc.Channel | None = None,
        annotation_channel: grpc.Channel | None = None,
        config: MldpConfig | None = None,
        config_file: str | None = None,
    ) -> None:
        """
        Initialize MLDP client with either explicit channels or configuration.

        :param ingestion_channel: gRPC communication channel for Ingestion Service (optional)
        :param query_channel: gRPC communication channel for Query Service (optional)
        :param annotation_channel: gRPC communication channel for Annotation Service (optional)
        :param config: Configuration object (optional)
        :param config_file: Path to YAML configuration file (optional)
        """

        self.logger = logging.getLogger(__name__)
        self.logger.debug(
            "Initializing MldpClient with ingestion_channel=%s, query_channel=%s, "
            "annotation_channel=%s, config=%s, config_file=%s",
            ingestion_channel is not None,
            query_channel is not None,
            annotation_channel is not None,
            config is not None,
            config_file,
        )

        # Load configuration if not using explicit channels
        if ingestion_channel is None and config is None:
            self.logger.info("Loading default configuration with config_file: %s", config_file)
            config = load_config(config_file=config_file)
        elif config is None and config_file is not None:
            self.logger.info("Loading configuration from file: %s", config_file)
            config = load_config(config_file=config_file)

        # Create channels from config or use provided channels.
        #
        # Note the `is not None` tests below rather than truthiness: config is always a valid
        # MldpConfig once loaded, but an explicitly-passed one could in principle be falsy, and
        # a truthiness test would silently fall through to the error branch.
        if ingestion_channel is not None:
            self.logger.debug("Using explicit ingestion channel")
            self._ingestion_channel = ingestion_channel
        elif config is not None:
            self.logger.info(
                "Creating ingestion channel from config: %s:%s (TLS=%s)",
                config.ingestion.host,
                config.ingestion.port,
                config.ingestion.use_tls,
            )
            self._ingestion_channel = config.create_ingestion_channel()
        else:
            # Defensive: the branches above load a config whenever ingestion_channel is absent,
            # so this is not reachable through the public constructor.  Kept as a guard against
            # a future change to that loading logic.
            error_msg = "Either ingestion_channel or config must be provided"
            self.logger.error(error_msg)
            raise ValueError(error_msg)

        # The query and annotation channels are optional, so declare them as such up front; the
        # ingestion channel is always set (or the constructor raises).
        self._query_channel: grpc.Channel | None
        self._annotation_channel: grpc.Channel | None

        if query_channel is not None:
            self.logger.debug("Using explicit query channel")
            self._query_channel = query_channel
        elif config is not None:
            self.logger.debug(
                "Creating query channel from config: %s:%s (TLS=%s)",
                config.query.host,
                config.query.port,
                config.query.use_tls,
            )
            self._query_channel = config.create_query_channel()
        else:
            # Query channel is optional for now
            self.logger.debug("No query channel provided - will be None")
            self._query_channel = None

        if annotation_channel is not None:
            self.logger.debug("Using explicit annotation channel")
            self._annotation_channel = annotation_channel
        elif config is not None:
            self.logger.debug(
                "Creating annotation channel from config: %s:%s (TLS=%s)",
                config.annotation.host,
                config.annotation.port,
                config.annotation.use_tls,
            )
            self._annotation_channel = config.create_annotation_channel()
        else:
            # Annotation channel is optional for now
            self.logger.debug("No annotation channel provided - will be None")
            self._annotation_channel = None

        # The configured timeouts apply to every client, explicit channels included (#81 D4); keepalive cannot,
        # being a property of a channel the library did not build.  Without a config, the clients' own defaults.
        ingestion_timeouts = _service_timeouts(config, "ingestion")
        query_timeouts = _service_timeouts(config, "query")
        annotation_timeouts = _service_timeouts(config, "annotation")
        if config is not None:
            annotation_timeouts["export_timeout"] = config.annotation_export_timeout_seconds
            annotation_timeouts["export_timeout_source"] = config._setting_source(
                "annotation", "export_timeout_seconds"
            )

        # Initialize service clients
        self.logger.info("Initializing ingestion client")
        self.ingestion_client = IngestionClient(self._ingestion_channel, **ingestion_timeouts)

        # Annotation service facade (exposes .pv_metadata, etc.); only created if a channel is available
        if self._annotation_channel is not None:
            self.logger.info("Initializing annotation client")
            self.annotation: AnnotationClient | None = AnnotationClient(self._annotation_channel, **annotation_timeouts)
        else:
            self.logger.debug("No annotation channel provided - annotation client will be None")
            self.annotation = None

        # Query service client (sample-oriented v2 query API); only created if a channel is available.
        # Exposed as .query (not .query_client) to leave room for feature-scoped sub-clients later, mirroring
        # the .annotation facade.
        if self._query_channel is not None:
            self.logger.info("Initializing query client")
            self.query: QueryClient | None = QueryClient(self._query_channel, **query_timeouts)
        else:
            self.logger.debug("No query channel provided - query client will be None")
            self.query = None

        # Store config for reference
        self._config = config
        self.logger.info("MldpClient initialization completed successfully")
