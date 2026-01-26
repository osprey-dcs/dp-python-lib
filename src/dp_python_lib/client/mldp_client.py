from typing import Optional
import grpc

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
                 config_file: Optional[str] = None):
        """
        Initialize MLDP client with either explicit channels or configuration.
        
        :param ingestion_channel: gRPC communication channel for Ingestion Service (optional)
        :param query_channel: gRPC communication channel for Query Service (optional) 
        :param annotation_channel: gRPC communication channel for Annotation Service (optional)
        :param config: Configuration object (optional)
        :param config_file: Path to YAML configuration file (optional)
        """
        
        # Load configuration if not using explicit channels
        if ingestion_channel is None and config is None:
            config = load_config(config_file=config_file)
        elif config is None and config_file is not None:
            config = load_config(config_file=config_file)
            
        # Create channels from config or use provided channels
        if ingestion_channel:
            self._ingestion_channel = ingestion_channel
        elif config:
            self._ingestion_channel = config.create_ingestion_channel()
        else:
            raise ValueError("Either ingestion_channel or config must be provided")
            
        if query_channel:
            self._query_channel = query_channel
        elif config:
            self._query_channel = config.create_query_channel()
        else:
            # Query channel is optional for now
            self._query_channel = None
            
        if annotation_channel:
            self._annotation_channel = annotation_channel
        elif config:
            self._annotation_channel = config.create_annotation_channel()
        else:
            # Annotation channel is optional for now
            self._annotation_channel = None
        
        # Initialize service clients
        self.ingestion_client = IngestionClient(self._ingestion_channel)
        
        # TODO: Add query and annotation clients when implemented
        # self.query_client = QueryClient(self._query_channel) if self._query_channel else None
        # self.annotation_client = AnnotationClient(self._annotation_channel) if self._annotation_channel else None
        
        # Store config for reference
        self._config = config
