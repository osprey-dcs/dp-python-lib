# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is the `dp-python-lib` repository, a Python client library for the Machine Learning Data Platform (MLDP) gRPC API. It provides Python bindings for interacting with the MLDP services.

## Project Structure

- `src/dp_python_lib/` - Main library source code
  - `grpc/` - Auto-generated Protocol Buffer and gRPC stub files (DO NOT EDIT)
  - `client/` - Client wrapper classes (e.g., `MldpClient`)
  - `models/` - Data model definitions
- `tests/` - Test suite with `unit/` and `integration/` subdirectories
- `pyproject.toml` - Project configuration and dependencies

## Development Commands

### Testing
```bash
# Run all tests
pytest tests/

# Run unit tests only
pytest tests/unit/

# Run specific test file
pytest tests/unit/test_ingestion_client.py -v
```

### Dependencies
Core dependencies are managed in `pyproject.toml`:
- `grpcio` - gRPC runtime
- `grpcio-tools` - gRPC development tools  
- `protobuf` - Protocol Buffers runtime

## Architecture Notes

- The `grpc/` directory contains auto-generated code from Protocol Buffer definitions
- These files are generated from the upstream `dp-grpc` project and should not be manually edited
- **Import Fix Process**: The gRPC generation process includes a post-processing step to fix relative import paths in the generated files (e.g., converting `import common_pb2` to `from . import common_pb2`)
- The main client entry point is `MldpClient` in `src/dp_python_lib/client/mldp_client.py`
- Client classes like `IngestionClient` provide user-friendly wrappers around gRPC service calls
- The library follows standard Python packaging conventions with `pyproject.toml`

## Key Files

- `src/dp_python_lib/client/mldp_client.py` - Main client wrapper for the gRPC services
- `src/dp_python_lib/client/ingestion_client.py` - Ingestion service client with methods like `register_provider()`
- `tests/unit/test_ingestion_client.py` - Unit tests for IngestionClient functionality
- `pyproject.toml` - Project metadata and dependencies
- Generated gRPC stubs include services for:
  - Ingestion (`ingestion_pb2.py`, `ingestion_pb2_grpc.py`)
  - Queries (`query_pb2.py`, `query_pb2_grpc.py`) 
  - Annotations (`annotation_pb2.py`, `annotation_pb2_grpc.py`)
  - Common types (`common_pb2.py`, `common_pb2_grpc.py`)

## Development Guidelines

### Client Implementation Pattern
- Follow the standard pattern: user params → build gRPC request → send request → return wrapped result
- Always write unit tests for new client methods in `tests/unit/`
- Use parameter classes (e.g., `RegisterProviderRequestParams`) for user-friendly APIs
- Client methods should return result objects that wrap gRPC responses with error handling

### gRPC Error Handling
- Use **synchronous gRPC calls** with `DpIngestionServiceStub` for simplicity
- Implement **three-tier error handling**:
  1. **gRPC Exceptions** (`grpc.RpcError`) - network/connection errors
  2. **Business Logic Errors** - check response `exceptionalResult` field
  3. **General Exceptions** - unexpected errors
- Check protobuf union fields with `response.HasField('fieldName')`
- Return consistent result objects with `is_error` flag and appropriate messages

### Testing Best Practices
- Use `@patch` decorators to mock gRPC stubs and avoid real network calls
- Mock the response behavior with `side_effect` for conditional logic (e.g., `HasField`)
- Always verify mocks were called correctly with `assert_called_once_with()`
- Test all error scenarios: success, business errors, gRPC exceptions, and unexpected cases

## Configuration System

### Overview
The library uses a flexible configuration system supporting YAML files and environment variables with **pydantic-settings** for type safety.

### Configuration Files
**Default location**: `mldp-config.yaml` in project root
```yaml
ingestion:
  host: localhost
  port: 50051
  use_tls: false
query:
  host: localhost
  port: 50052
  use_tls: false
annotation:
  host: localhost
  port: 50053
  use_tls: false
```

### Environment Variables
Use pattern: `MLDP_<SERVICE>_<SETTING>`
```bash
# Override specific service settings  
MLDP_INGESTION_HOST=prod-ingestion.example.com
MLDP_INGESTION_PORT=443
MLDP_INGESTION_USE_TLS=true

# Custom config file location
MLDP_CONFIG_FILE=/path/to/custom-config.yaml
```

### Usage Patterns
```python
from dp_python_lib.client import MldpClient
from dp_python_lib.config import MldpConfig, ServiceConfig

# Auto-load from default locations (env vars override YAML)
client = MldpClient()

# Specify config file
client = MldpClient(config_file="custom-config.yaml")

# Direct config object
config = MldpConfig(
    ingestion=ServiceConfig(host="custom-host", port=8080, use_tls=True)
)
client = MldpClient(config=config)

# Backward compatibility - direct channels
import grpc
channel = grpc.insecure_channel("localhost:50051")
client = MldpClient(ingestion_channel=channel)
```

### Configuration Priority (High to Low)
1. **Explicit parameters** (direct channels, config objects)
2. **Environment variables** (`MLDP_*`)
3. **YAML configuration file**
4. **Built-in defaults**

### Configuration Implementation
**Architecture**: Uses **flattened pydantic-settings** approach for standard environment variable handling:

```python
class MldpConfig(BaseSettings):
    # Flat field structure for better env var support
    ingestion_host: str = "localhost"
    ingestion_port: int = 50051
    ingestion_use_tls: bool = False
    
    query_host: str = "localhost" 
    query_port: int = 50052
    query_use_tls: bool = False
    
    annotation_host: str = "localhost"
    annotation_port: int = 50053
    annotation_use_tls: bool = False
    
    model_config = SettingsConfigDict(
        env_prefix='MLDP_',
        case_sensitive=False
    )
    
    # Properties provide access to grouped ServiceConfig objects
    @property
    def ingestion(self) -> ServiceConfig:
        return ServiceConfig(
            host=self.ingestion_host,
            port=self.ingestion_port, 
            use_tls=self.ingestion_use_tls
        )
```

### Key Configuration Classes
- **`ServiceConfig`** - Individual service configuration (host, port, use_tls) with gRPC channel creation
- **`MldpConfig`** - Main config container with flattened fields for environment variable support
- **`load_config()`** - Configuration loader with priority handling
- **`find_config_file()`** - Config file discovery (explicit path > env var > project locations)

### Dependencies Added
- `pydantic-settings` - Type-safe configuration with environment variable support
- `PyYAML` - YAML file parsing