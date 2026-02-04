from typing import Optional
import grpc
import logging

from dp_python_lib.client.ingestion_client import IngestionClient
from dp_python_lib.config import MldpConfig, load_config


class MldpClient:

    """
    This is the main user-facing API client class.  It provides variables for accessing the Ingestion, Query,
    and Annotation Service API clients, respectively.
    """

    def __init__(self, 
                 ingestion_channel: Optional[grpc.Channel] = None,
                 query_channel: Optional[grpc.Channel] = None,
                 annotation_channel: Optional[grpc.Channel] = None,
                 config: Optional[MldpConfig] = None,
                 config_file: Optional[str] = None) -> None:
        """
        Initialize MLDP client with either explicit channels or configuration.
        
        :param ingestion_channel: gRPC communication channel for Ingestion Service (optional)
        :param query_channel: gRPC communication channel for Query Service (optional) 
        :param annotation_channel: gRPC communication channel for Annotation Service (optional)
        :param config: Configuration object (optional)
        :param config_file: Path to YAML configuration file (optional)
        """
        
        self.logger = logging.getLogger(__name__)
        self.logger.debug("Initializing MldpClient with ingestion_channel=%s, query_channel=%s, annotation_channel=%s, config=%s, config_file=%s", 
                         ingestion_channel is not None, query_channel is not None, annotation_channel is not None, 
                         config is not None, config_file)
        
        # Load configuration if not using explicit channels
        if ingestion_channel is None and config is None:
            self.logger.info("Loading default configuration with config_file: %s", config_file)
            config = load_config(config_file=config_file)
        elif config is None and config_file is not None:
            self.logger.info("Loading configuration from file: %s", config_file)
            config = load_config(config_file=config_file)
            
        # Create channels from config or use provided channels
        if ingestion_channel:
            self.logger.debug("Using explicit ingestion channel")
            self._ingestion_channel = ingestion_channel
        elif config:
            self.logger.info("Creating ingestion channel from config: %s:%s (TLS=%s)", 
                           config.ingestion.host, config.ingestion.port, config.ingestion.use_tls)
            self._ingestion_channel = config.create_ingestion_channel()
        else:
            error_msg = "Either ingestion_channel or config must be provided"
            self.logger.error(error_msg)
            raise ValueError(error_msg)
            
        if query_channel:
            self.logger.debug("Using explicit query channel")
            self._query_channel = query_channel
        elif config:
            self.logger.debug("Creating query channel from config: %s:%s (TLS=%s)", 
                            config.query.host, config.query.port, config.query.use_tls)
            self._query_channel = config.create_query_channel()
        else:
            # Query channel is optional for now
            self.logger.debug("No query channel provided - will be None")
            self._query_channel = None
            
        if annotation_channel:
            self.logger.debug("Using explicit annotation channel")
            self._annotation_channel = annotation_channel
        elif config:
            self.logger.debug("Creating annotation channel from config: %s:%s (TLS=%s)", 
                            config.annotation.host, config.annotation.port, config.annotation.use_tls)
            self._annotation_channel = config.create_annotation_channel()
        else:
            # Annotation channel is optional for now
            self.logger.debug("No annotation channel provided - will be None")
            self._annotation_channel = None
        
        # Initialize service clients
        self.logger.info("Initializing ingestion client")
        self.ingestion_client = IngestionClient(self._ingestion_channel)
        
        # TODO: Add query and annotation clients when implemented
        # self.query_client = QueryClient(self._query_channel) if self._query_channel else None
        # self.annotation_client = AnnotationClient(self._annotation_channel) if self._annotation_channel else None
        
        # Store config for reference
        self._config = config
        self.logger.info("MldpClient initialization completed successfully")
