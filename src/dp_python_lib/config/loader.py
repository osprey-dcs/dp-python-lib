import os
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
    if config_file:
        if os.path.exists(config_file):
            return config_file
        else:
            raise FileNotFoundError(f"Specified config file not found: {config_file}")
    
    # Check environment variable
    env_config = os.getenv('MLDP_CONFIG_FILE')
    if env_config and os.path.exists(env_config):
        return env_config
    
    # Check current directory
    current_dir_config = Path.cwd() / "mldp-config.yaml"
    if current_dir_config.exists():
        return str(current_dir_config)
    
    # Check project root (look for pyproject.toml to identify project root)
    current_path = Path.cwd()
    for parent in [current_path] + list(current_path.parents):
        if (parent / "pyproject.toml").exists():
            project_config = parent / "mldp-config.yaml"
            if project_config.exists():
                return str(project_config)
    
    # No config file found
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
    
    # If explicit config object provided, use it (but still allow env var overrides)
    if config_object:
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
        # Load from YAML, with environment variable overrides
        return MldpConfig.from_yaml(yaml_file)
    else:
        # No YAML file, use defaults with environment variable overrides
        return MldpConfig()


def get_default_config() -> MldpConfig:
    """Get default configuration (useful for testing)."""
    return MldpConfig()