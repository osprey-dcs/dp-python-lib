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
pytest tests/
```

### Dependencies
Core dependencies are managed in `pyproject.toml`:
- `grpcio` - gRPC runtime
- `grpcio-tools` - gRPC development tools  
- `protobuf` - Protocol Buffers runtime

## Architecture Notes

- The `grpc/` directory contains auto-generated code from Protocol Buffer definitions
- These files are generated from the upstream `dp-grpc` project and should not be manually edited
- The main client entry point is `MldpClient` in `src/dp_python_lib/client/mldp_client.py`
- The library follows standard Python packaging conventions with `pyproject.toml`

## Key Files

- `src/dp_python_lib/client/mldp_client.py` - Main client wrapper for the gRPC services
- `pyproject.toml` - Project metadata and dependencies
- Generated gRPC stubs include services for:
  - Ingestion (`ingestion_pb2.py`, `ingestion_pb2_grpc.py`)
  - Queries (`query_pb2.py`, `query_pb2_grpc.py`) 
  - Annotations (`annotation_pb2.py`, `annotation_pb2_grpc.py`)
  - Common types (`common_pb2.py`, `common_pb2_grpc.py`)