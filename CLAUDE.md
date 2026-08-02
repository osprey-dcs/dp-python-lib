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
- `pydantic-settings` - Type-safe configuration with environment variable support
- `PyYAML` - YAML file parsing

## Architecture Notes

- The `grpc/` directory contains auto-generated code from Protocol Buffer definitions
- These files are generated from the upstream `dp-grpc` project and should not be manually edited
- **Import Fix Process**: The gRPC generation process includes a post-processing step to fix relative import paths in the generated files (e.g., converting `import common_pb2` to `from . import common_pb2`)
- The main client entry point is `MldpClient` in `src/dp_python_lib/client/mldp_client.py`
- Client classes like `IngestionClient` provide user-friendly wrappers around gRPC service calls
- The library follows standard Python packaging conventions with `pyproject.toml`
- **Type Hints**: All framework classes use comprehensive type annotations for better IDE support and error detection
- **Logging**: Built-in logging throughout the framework using Python's standard `logging` module

## Key Files

- `src/dp_python_lib/client/mldp_client.py` - Main client wrapper for the gRPC services
- `src/dp_python_lib/client/ingestion_client.py` - Ingestion service client with methods like `register_provider()`
- `src/dp_python_lib/client/annotation_client.py` - Annotation service facade; groups feature-scoped clients sharing the one `DpAnnotationService` channel (exposes `.pv_metadata` and `.machine_config`, with room to grow `.annotations`)
- `src/dp_python_lib/client/pv_metadata_client.py` - PV metadata client (`save_pv_metadata()`, `get_pv_metadata()`, `query_pv_metadata()`, `iter_pv_metadata()`, `delete_pv_metadata()`) plus the `PvMetadataQuery` (`Q`) criterion helpers
- `src/dp_python_lib/client/machine_config_client.py` - Machine configuration client covering both configurations (`save_configuration()`, `get_configuration()`, `query_configurations()`, `iter_configurations()`, `delete_configuration()`) and their temporal activations (`save_configuration_activation()`, `get_configuration_activation()`, `query_configuration_activations()`, `iter_configuration_activations()`, `delete_configuration_activation()`, `get_active_configurations()`). Includes the `ConfigurationQuery` (`C`) and `ConfigurationActivationQuery` (`CA`) criterion helpers and the `to_timestamp()` helper (tz-aware datetime / epoch seconds / `common.Timestamp`). Get/delete activation take a composite key (`client_activation_id` XOR `configuration_name`+`start_time`)
- `src/dp_python_lib/client/query_client.py` - v2 time-series query client (sample-oriented) exposed as `client.query`. Low-level wrappers `query_samples()` (unary, one resumable page) and `iter_query_samples()` (transparent paging), plus `iter_query_samples_stream()` (server-streaming, fire-and-consume, lazy). Queries are described by a kind-neutral `QueryParams` built from the `PvQuery` (`PV`) and `ConfigQuery` (`CFG`) criterion helpers; shares a `_build_query_spec()` seam so a future bucket request builder reuses it. Results wrap the raw `ColumnTable` (`.column_table`, `.next_page_token`); `.to_dataframe()`/`.to_numpy()` delegate to `query_conversions` (Phase 2, optional `[analysis]` extra)
- `src/dp_python_lib/client/query_conversions.py` - Pythonic conversions for query results (optional `[analysis]` extra: pandas/numpy/openpyxl, imported lazily). `data_value_to_python()` (oneof extractor: scalars→native, timestamp→epoch-nanos, array→list, structure→dict, image→`Image` wrapper, fail-loud on unhandled arm), `column_table_to_dataframe()` (UTC datetime index + one column per DataColumn; dense-alignment fail-loud; ColumnMetadata in `df.attrs`), `column_table_to_numpy()` (dict-of-arrays), `dataframe_to_excel()` (thin `to_excel()` wrapper: row-limit guard, tz-drop, complex-cell stringification), and `query_samples_to_dataframe()`/`stream_query_samples_to_dataframes()` whole-query conveniences (unary concats by column name; streaming yields per-page frames lazily)
- `tests/unit/test_ingestion_client.py` - Unit tests for IngestionClient functionality
- `tests/unit/test_pv_metadata_client.py` - Unit tests for PvMetadataClient functionality
- `tests/unit/test_machine_config_client.py` - Unit tests for the Configuration side of MachineConfigClient
- `tests/unit/test_machine_config_activation_client.py` - Unit tests for the ConfigurationActivation side of MachineConfigClient (incl. composite-key validation, timestamp handling, getActiveConfigurations)
- `tests/unit/test_query_client.py` - Unit tests for QueryClient (request building, three-tier error handling, unary paging, streaming, `PvQuery`/`ConfigQuery` helpers, `QueryParams` validation)
- `tests/unit/test_query_conversions.py` - Unit tests for query_conversions (each DataValue arm, dense-alignment fail-loud, int-gap float-upcast, timestamp columns, metadata in attrs, concat-by-name, Excel row-limit/stringification; DataFrame/NumPy/Excel tests skip cleanly when the `[analysis]` extra is absent)
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
- Service clients extend `ServiceApiClientBase`, which is constructed with `(channel, stub_class)` and
  creates the gRPC stub **once** at init time, stored as `self._stub`.  `_send_*` methods reuse
  `self._stub` rather than creating a new stub per call.
- Where one gRPC service backs several feature areas (e.g. `DpAnnotationService` covers PV metadata,
  machine configuration, and annotations), use a lightweight facade (`AnnotationClient`) that owns the
  shared channel and exposes feature-scoped clients as attributes (`annotation.pv_metadata`).  This
  keeps each feature client cohesive while matching the single-service reality of the gRPC API.

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

### Type Hints and Modern Python
- **All framework classes use comprehensive type hints** with Python 3.5+ syntax
- Parameter types: `str`, `bool`, `Optional[str]`, `List[str]`, `Dict[str, str]`
- gRPC-specific types: `grpc.Channel`, `ingestion_pb2.RegisterProviderRequest`
- Return type annotations: `-> None`, `-> RegisterProviderApiResult`
- Import required types: `from typing import Optional, Dict, List`

### Logging System
**Architecture**: Uses Python's standard `logging` module with hierarchical logger names

**Implementation Pattern**:
```python
import logging

class MyClient:
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
    def my_method(self):
        self.logger.info("Starting operation")
        self.logger.debug("Technical details: %s", details)
        self.logger.warning("Recoverable issue: %s", issue)
        self.logger.error("Serious problem: %s", error, exc_info=True)
```

**Logger Hierarchy**:
- `dp_python_lib.client.mldp_client` - Main client initialization and configuration
- `dp_python_lib.client.ingestion_client` - API operations with detailed request/response logging
- `dp_python_lib.config.config` - Configuration loading and YAML processing
- `dp_python_lib.config.loader` - Config file discovery and priority handling

**Log Levels Used**:
- `DEBUG` - Technical details (request building, parameter processing)
- `INFO` - Business events (API calls, successful operations)  
- `WARNING` - Recoverable issues (business logic errors from API)
- `ERROR` - Serious problems (gRPC errors, unexpected exceptions with stack traces)

**Application Usage**:
```python
import logging

# Configure logging in your application (not the library!)
logging.basicConfig(level=logging.INFO, 
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Library will log useful operational information
client = MldpClient(config_file="config.yaml")
result = client.ingestion_client.register_provider(params)
```

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

### PV Metadata API (Annotation Service)
PV metadata methods are exposed under the `annotation` facade at `client.annotation.pv_metadata`
(available whenever an annotation channel/config is provided):
```python
from dp_python_lib.client import MldpClient, SavePvMetadataRequestParams, PvMetadataQuery as Q

client = MldpClient()
pv = client.annotation.pv_metadata

# save (dict attributes, list aliases/tags)
pv.save_pv_metadata(SavePvMetadataRequestParams(
    pv_name="ABC:1", aliases=["abc-one"], tags=["vacuum"],
    attributes={"unit": "V"}, modified_by="operator", description="Vacuum gauge"))

# get / delete by PV name OR alias
result = pv.get_pv_metadata("abc-one")
metadata = result.pv_metadata            # common_pb2.PvMetadata, or None on error
pv.delete_pv_metadata("ABC:1")

# query one page (exposes .pv_metadata_list and .next_page_token)
page = pv.query_pv_metadata([Q.pv_name(prefix=["ABC:"]), Q.tags(["vacuum"])], limit=100)

# or iterate transparently across all pages (raises RuntimeError on a page error)
for record in pv.iter_pv_metadata([Q.attributes("unit", ["V"])]):
    print(record.pvName)
```

### Machine Configuration API (Annotation Service)
Machine configuration methods are exposed under the `annotation` facade at `client.annotation.machine_config`.
The client covers named *configurations* and their temporal *activations*, plus a point-in-time active lookup:
```python
from datetime import datetime, timezone
from dp_python_lib.client import (
    MldpClient,
    SaveConfigurationRequestParams,
    SaveConfigurationActivationRequestParams,
    ConfigurationQuery as C,
    ConfigurationActivationQuery as CA,
)

client = MldpClient()
mc = client.annotation.machine_config

# configurations: save / get / query / iterate / delete
mc.save_configuration(SaveConfigurationRequestParams(
    configuration_name="beamline-optics", category="optics",
    tags=["production"], attributes={"owner": "ops"}, modified_by="operator"))
config = mc.get_configuration("beamline-optics").configuration
for cfg in mc.iter_configurations([C.name(prefix=["beamline-"]), C.tags(["production"])]):
    print(cfg.configurationName)

# activations: timestamps accept a tz-aware datetime, epoch seconds, or common.Timestamp
start = datetime(2024, 1, 1, tzinfo=timezone.utc)
end = datetime(2024, 1, 2, tzinfo=timezone.utc)
mc.save_configuration_activation(SaveConfigurationActivationRequestParams(
    configuration_name="beamline-optics", start_time=start, end_time=end,
    client_activation_id="act-001", modified_by="operator"))

# get/delete activation by client id OR by (configuration_name, start_time) composite key
mc.get_configuration_activation(client_activation_id="act-001")
mc.get_configuration_activation(configuration_name="beamline-optics", start_time=start)

# query/iterate activations (raises RuntimeError on a page error)
for a in mc.iter_configuration_activations([CA.configuration_name(["beamline-optics"])]):
    print(a.clientActivationId)

# what is active right now? (pass a timestamp for a historical instant)
active = mc.get_active_configurations().configuration_activations

mc.delete_configuration_activation(client_activation_id="act-001")
mc.delete_configuration("beamline-optics")
```

Notes:
- `to_timestamp()` (also exported) is the shared time converter; naive datetimes raise `ValueError`.
- Composite-key get/delete require exactly one key form (id XOR name+start_time); violations raise `ValueError`.
- `ConfigurationQuery` (`C`) criteria: `name`/`category`/`tags`/`attributes`/`parent`.
  `ConfigurationActivationQuery` (`CA`) criteria: `timestamp`/`time_range`/`configuration_name`/`client_activation_id`/`category`/`tags`/`attributes`.  Each helper raises `ValueError` on empty inputs.

### v2 Query API (Query Service)

The sample-oriented v2 time-series query methods are exposed at `client.query` (a `QueryClient`; `None` when no
query channel is configured).  A query is a kind-neutral `QueryParams` over a half-open time range `[begin, end)`,
built from the `PvQuery` (`PV`) and `ConfigQuery` (`CFG`) criterion helpers.  Low-level methods return the raw
protobuf `ColumnTable`; pandas/NumPy/Excel conversions (Phase 2, optional `[analysis]` extra) are reached via the
result's `.to_dataframe()` / `.to_numpy()`.

```python
from datetime import datetime, timezone
from dp_python_lib.client import MldpClient, QueryParams, PvQuery as PV, ConfigQuery as CFG

client = MldpClient()
q = client.query

begin = datetime(2024, 1, 1, tzinfo=timezone.utc)
end = datetime(2024, 1, 2, tzinfo=timezone.utc)

# Select PVs by name list, a name pattern, or a metadata query (choose at most one form).
params = QueryParams(
    begin_time=begin, end_time=end,
    pv_selector=PV.metadata([PV.pv_name(prefix=["ABC:"]), PV.tags(["vacuum"])]),
    config_criteria=[CFG.configuration_name(["beamline-optics"])],   # optional; restricts to active intervals
    limit=1000,                                                       # rows PER PAGE, not a total cap
)

# unary: one page, or transparently page over the whole range
page = q.query_samples(params)                 # QuerySamplesApiResult; .column_table, .next_page_token
for page in q.iter_query_samples(params):      # raises RuntimeError on a page error
    table = page.column_table

# server-streaming: lazy, fire-and-consume (no page tokens); raises RuntimeError on a mid-stream error
for page in q.iter_query_samples_stream(params):
    table = page.column_table

# Pythonic conversions (require the optional [analysis] extra: pip install dp-python-lib[analysis])
df = q.query_samples(params).to_dataframe()      # one page -> pandas.DataFrame (UTC datetime index)
arrays = q.query_samples(params).to_numpy()      # one page -> {"timestamps": ndarray, "<col>": ndarray, ...}

from dp_python_lib.client import query_conversions as qc
df_all = qc.query_samples_to_dataframe(q, params, max_rows=1_000_000)   # pages internally, concats by column name
for frame in qc.stream_query_samples_to_dataframes(q, params):         # lazy, one DataFrame per streamed page
    ...
qc.dataframe_to_excel(df_all, "out.xlsx")        # thin to_excel() wrapper (row-limit guard, complex cells stringified)
```

Notes:
- Time inputs accept a tz-aware datetime, epoch seconds, or `common.Timestamp` (shared `to_timestamp()`); `begin`
  must be strictly before `end`, and at least one of `pv_selector` / `config_criteria` must be present (a
  config-only query is legal).  Empty inputs raise `ValueError`.
- `PvQuery` (`PV`) selectors: `name_list(values)` / `pattern(str)` / `metadata([...])`, whose criteria are
  `pv_name(exact=, prefix=, contains=)` / `aliases(exact=, prefix=, contains=)` (each repeated & coexisting) plus
  `tags(values)` / `attr(key, values)`.  `ConfigQuery` (`CFG`) criteria:
  `configuration_name`/`client_activation_id`/`category`/`tags` each `(values)`, and `attr(key, values)`.
- Serialized columns are deferred: `useSerializedColumns` is forced `False`; a result carrying
  `serializedDataColumns` raises `NotImplementedError` in the conversion layer.
- `DataValue` mapping: scalars→native dtypes, `timestampValue`→`datetime64[ns, UTC]`, integer columns with gaps
  upcast to `float64`; complex arms preserved losslessly (`arrayValue`→list, `structureValue`→dict,
  `byteArrayValue`→bytes, `imageValue`→`Image(data, file_type)`); an unhandled oneof arm raises.  `valueStatus`
  is ignored (never populated in `querySamples()` results).  Per-column `ColumnMetadata` lands in
  `df.attrs["column_metadata"]` unless `exclude_column_metadata=True`.
- Future PyTorch support (mentioned indirectly by the customer; to be confirmed in the post-implementation
  requirements scan) is additive: `column_table_to_numpy()`'s dict-of-arrays is the intended substrate for a
  `column_table_to_torch()` behind a separate optional `[torch]` extra — no change to `QueryClient` or the NumPy
  path. Not built yet.

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