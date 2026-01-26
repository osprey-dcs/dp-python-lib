from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import grpc
import logging


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
    
    model_config = SettingsConfigDict(
        env_prefix='MLDP_',
        case_sensitive=False
    )
    
    @property
    def ingestion(self) -> ServiceConfig:
        """Get ingestion service configuration."""
        return ServiceConfig(
            host=self.ingestion_host,
            port=self.ingestion_port,
            use_tls=self.ingestion_use_tls
        )
    
    @property  
    def query(self) -> ServiceConfig:
        """Get query service configuration."""
        return ServiceConfig(
            host=self.query_host,
            port=self.query_port,
            use_tls=self.query_use_tls
        )
    
    @property
    def annotation(self) -> ServiceConfig:
        """Get annotation service configuration.""" 
        return ServiceConfig(
            host=self.annotation_host,
            port=self.annotation_port,
            use_tls=self.annotation_use_tls
        )
    
    @classmethod
    def from_yaml(cls, yaml_file: str) -> 'MldpConfig':
        """Load configuration from YAML file."""
        import yaml
        logger = logging.getLogger(__name__)
        
        try:
            logger.info("Loading configuration from YAML file: %s", yaml_file)
            with open(yaml_file, 'r') as f:
                data = yaml.safe_load(f)
            
            # Convert nested YAML structure to flat fields
            flat_data = {}
            
            for service in ['ingestion', 'query', 'annotation']:
                if service in data:
                    service_config = data[service]
                    if 'host' in service_config:
                        flat_data[f'{service}_host'] = service_config['host']
                        logger.debug("Loaded %s_host: %s", service, service_config['host'])
                    if 'port' in service_config:
                        flat_data[f'{service}_port'] = service_config['port']
                        logger.debug("Loaded %s_port: %s", service, service_config['port'])
                    if 'use_tls' in service_config:
                        flat_data[f'{service}_use_tls'] = service_config['use_tls']
                        logger.debug("Loaded %s_use_tls: %s", service, service_config['use_tls'])
            
            logger.debug("Successfully loaded configuration from YAML, creating MldpConfig instance")
            return cls(**flat_data)
            
        except FileNotFoundError:
            logger.warning("YAML configuration file not found: %s, using defaults", yaml_file)
            return cls()
        except Exception as e:
            logger.error("Error loading configuration from %s: %s", yaml_file, e)
            raise ValueError(f"Error loading configuration from {yaml_file}: {e}")
    
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