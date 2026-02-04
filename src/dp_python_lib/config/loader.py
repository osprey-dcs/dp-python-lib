import os
import logging
from pathlib import Path
from typing import Optional, Union
from .config import MldpConfig


def find_config_file(config_file: Optional[str] = None) -> Optional[str]:
    """
    Find configuration file in order of priority:
    1. Explicitly provided config_file parameter
    2. MLDP_CONFIG_FILE environment variable
    3. mldp-config.yaml in current directory
    4. mldp-config.yaml in project root (where pyproject.toml is)
    """
    logger = logging.getLogger(__name__)
    
    if config_file:
        logger.debug("Checking explicitly provided config file: %s", config_file)
        if os.path.exists(config_file):
            logger.info("Found explicit config file: %s", config_file)
            return config_file
        else:
            logger.error("Specified config file not found: %s", config_file)
            raise FileNotFoundError(f"Specified config file not found: {config_file}")
    
    # Check environment variable
    env_config = os.getenv('MLDP_CONFIG_FILE')
    if env_config:
        logger.debug("Found MLDP_CONFIG_FILE environment variable: %s", env_config)
        if os.path.exists(env_config):
            logger.info("Using config file from environment variable: %s", env_config)
            return env_config
        else:
            logger.warning("Config file from environment variable does not exist: %s", env_config)
    
    # Check current directory
    current_dir_config = Path.cwd() / "mldp-config.yaml"
    logger.debug("Checking for config file in current directory: %s", current_dir_config)
    if current_dir_config.exists():
        logger.info("Found config file in current directory: %s", current_dir_config)
        return str(current_dir_config)
    
    # Check project root (look for pyproject.toml to identify project root)
    current_path = Path.cwd()
    logger.debug("Searching for config file in project root directories")
    for parent in [current_path] + list(current_path.parents):
        if (parent / "pyproject.toml").exists():
            project_config = parent / "mldp-config.yaml"
            logger.debug("Checking project root config: %s", project_config)
            if project_config.exists():
                logger.info("Found config file in project root: %s", project_config)
                return str(project_config)
    
    # No config file found
    logger.debug("No configuration file found in any searched location")
    return None


def load_config(config_file: Optional[str] = None, 
                config_object: Optional[MldpConfig] = None) -> MldpConfig:
    """
    Load MLDP configuration with priority handling:
    1. Explicit config_object parameter (highest priority)
    2. Environment variables (override YAML values)
    3. YAML configuration file
    4. Default values (lowest priority)
    
    Args:
        config_file: Optional path to YAML configuration file
        config_object: Optional pre-constructed config object
        
    Returns:
        MldpConfig object with resolved configuration
        
    Raises:
        ValueError: If configuration loading fails
    """
    logger = logging.getLogger(__name__)
    logger.debug("Loading MLDP configuration with config_file=%s, config_object=%s", 
                config_file, config_object is not None)
    
    # If explicit config object provided, use it (but still allow env var overrides)
    if config_object:
        logger.info("Using explicit config object with environment variable overrides")
        # Create a new instance that will pick up environment variables
        return MldpConfig(
            ingestion_host=config_object.ingestion.host,
            ingestion_port=config_object.ingestion.port,
            ingestion_use_tls=config_object.ingestion.use_tls,
            query_host=config_object.query.host,
            query_port=config_object.query.port,
            query_use_tls=config_object.query.use_tls,
            annotation_host=config_object.annotation.host,
            annotation_port=config_object.annotation.port,
            annotation_use_tls=config_object.annotation.use_tls
        )
    
    # Find and load from YAML file (if available)
    yaml_file = find_config_file(config_file)
    
    if yaml_file:
        logger.info("Loading configuration from YAML file: %s", yaml_file)
        # Load from YAML, with environment variable overrides
        return MldpConfig.from_yaml(yaml_file)
    else:
        logger.info("No YAML file found, using default configuration with environment variable overrides")
        # No YAML file, use defaults with environment variable overrides
        return MldpConfig()


def get_default_config() -> MldpConfig:
    """Get default configuration (useful for testing)."""
    return MldpConfig()