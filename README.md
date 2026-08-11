# dp-python-lib

Python client library for the [Machine Learning Data Platform](https://github.com/osprey-dcs/data-platform) (MLDP).

## Overview

MLDP is a data platform for capturing, archiving, annotating, and querying scientific time-series
data — PV samples from an accelerator or similar facility, together with the metadata that gives
those samples meaning.  Its services are exposed as a gRPC API, defined in the
[dp-grpc repo](https://github.com/osprey-dcs/dp-grpc).

This repo exists so that Python applications — analysis notebooks, data pipelines, machine
learning workflows — can use that platform without writing gRPC code.  It provides two layers:

1. **A low-level client API** that wraps each MLDP service call: parameter objects in, result
   objects out, with connection handling, configuration, logging, and error handling done for you.
2. **Higher-level application features** (in progress) that turn the raw API into the operations a
   data science workflow actually wants — retrieving a labelled dataset, exporting to common
   formats, feeding a training pipeline.

The generated gRPC stubs live in [src/dp_python_lib/grpc](src/dp_python_lib/grpc).  They are
produced by an Actions workflow in the dp-grpc repo (`generate-python-stubs.yml`), which can be
run manually and fires automatically on a release tag; it opens a pull request against this repo.
**These files are generated and must not be edited by hand** — fix the generator instead.

## Goal state

The target is a library where the gRPC API is an implementation detail.

**Low-level API coverage.** A Python wrapper for every MLDP service method, following one
consistent pattern (parameter class → request → result object), across all four services:

| Service | Scope |
|---|---|
| Ingestion | Registering providers, ingesting data (unary, streaming, bidirectional), checking request status, subscribing to live data |
| Query | Retrieving time-series data as samples, buckets, or tables; retrieving PV, provider, and ingestion statistics |
| Annotation | User-defined PV metadata, machine configuration, datasets, annotations, and export |
| Ingestion Stream | Event subscriptions that fire when a data condition is triggered |

**Higher-level application features.**  An `MldpApplication` layer built on top of the API
clients, providing the things a data pipeline needs rather than the things the wire protocol
offers: dataset assembly and reuse, Pythonic data structures (pandas, NumPy, and — pending
confirmation — PyTorch tensors), export to common file formats, and end-to-end
ingest → annotate → query → train workflows.

The [current state](#current-state) below is the part of this that exists today; the
[TODO](#todo) is what remains.

## Current state

`MldpClient` is the entry point.  It reads configuration, creates the service channels, and
exposes the feature clients as attributes.  Sub-clients are `None` when no channel is configured
for their service.

**Implemented:**

- **PV metadata API** — `client.annotation.pv_metadata`.  Record what a PV *is* (aliases, tags,
  attributes, description) and find PVs by those properties: `save_pv_metadata()`,
  `get_pv_metadata()`, `query_pv_metadata()`, `iter_pv_metadata()`, `delete_pv_metadata()`, with
  the `PvMetadataQuery` criterion helpers.
- **Machine configuration API** — `client.annotation.machine_config`.  Named configurations and
  their temporal activations — which configuration was active over which interval, and what is
  active at a given instant.  Covers save/get/query/iterate/delete for both configurations and
  activations, plus `get_active_configurations()`, with the `ConfigurationQuery` and
  `ConfigurationActivationQuery` helpers.
- **v2 query API (samples)** — `client.query`.  Sample-oriented time-series retrieval over a
  half-open time range, selecting PVs by name list, name pattern, or metadata query, and
  optionally restricting to intervals where a machine configuration was active.  Unary with
  transparent paging (`query_samples()` / `iter_query_samples()`) and server-streaming
  (`iter_query_samples_stream()`).  Results convert to pandas DataFrames, NumPy arrays, and Excel
  via the optional `[analysis]` extra.
- **Provider registration** — `client.ingestion_client.register_provider()`.  The rest of the
  ingestion API is not yet implemented.

**Supporting framework:** YAML + environment-variable configuration (`MLDP_*`, via
pydantic-settings), TLS-capable channel creation, hierarchical logging, three-tier error handling
(gRPC errors, business-logic errors, unexpected exceptions), comprehensive type hints, and a unit
and integration test suite.

Note the v2 query API comes from unreleased dp-grpc work and will not work against a
`rel-1.14.0` server.

## TODO

**Low-level API coverage**

- **Ingestion Service**
  - `ingestData()` / `ingestDataStream()` / `ingestDataBidiStream()` — full ingestion client with a
    shared DataFrame payload model ([issue #17](https://github.com/osprey-dcs/dp-python-lib/issues/17);
    also unblocks the closed-loop query integration test and the cookbook's ingestion recipe)
  - `queryRequestStatus()` — async status of ingestion requests
  - `subscribeData()` — receive data for specified PVs from the ingestion stream
- **Query Service**
  - `queryBuckets()` / `queryBucketsStream()` — raw data buckets
    ([issue #16](https://github.com/osprey-dcs/dp-python-lib/issues/16))
  - `queryData()` — bucketed PV time-series data
  - `queryTable()` — PV time-series data in tabular format
  - `queryPvStats()` — archive ingestion statistics for PVs
  - `queryProviders()` / `queryProviderStats()` — provider information and ingestion statistics
- **Annotation Service**
  - `saveDataSet()` / `queryDataSets()` — datasets over collections of PVs and time ranges
  - `saveAnnotation()` / `queryAnnotations()` — annotations targeting a dataset
  - `exportData()` — export datasets to common file formats
- **Ingestion Stream Service**
  - `subscribeDataEvent()` — notification when a data condition in the ingestion stream triggers

**Higher-level features**

- Design and implement `MldpApplication` with high-level application support
- Data science conveniences beyond the current DataFrame / NumPy conversions — PyTorch tensor
  support is the likely next step, additive on top of the existing NumPy path

**Project infrastructure**

- CI workflow(s) for running regression tests and publishing release artifacts

## Installation

Python 3.10 or later.

```bash
# core client
pip install -e .

# with pandas / NumPy / Excel conversions for query results
pip install -e .[analysis]

# development tooling (pytest, mypy for the cookbook snippet checker)
pip install -e .[dev]
```

Point the client at your MLDP services with an `mldp-config.yaml` file or `MLDP_*` environment
variables — see [Creating and connecting a client](doc/cookbook/connecting.md).

## Hello, MLDP

Every call follows the same shape: build a parameter object, call a method on a feature client,
check `result_status.is_error`, then read the typed payload.

```python
from dp_python_lib.client import MldpClient, SavePvMetadataRequestParams, PvMetadataQuery as Q

client = MldpClient()                       # config file + MLDP_* env vars
pv = client.annotation.pv_metadata

# Record what a PV is
result = pv.save_pv_metadata(SavePvMetadataRequestParams(
    pv_name="BPMS:GUNB:314:X",
    tags=["beam-position"],
    attributes={"AREA": "GUNB", "TYPE": "BPMS"},
    modified_by="operator",
    description="Beam position monitor, horizontal",
))
if result.result_status.is_error:
    raise RuntimeError(result.result_status.message)

# Find it again by property rather than by name — iter_* pages transparently
for metadata in pv.iter_pv_metadata([Q.attributes("AREA", ["GUNB"])]):
    print(metadata.pvName)
```

Checking `result_status.is_error` is not ceremony: the typed accessors return `None` or `[]` on
failure, so skipping the check turns an error into silently missing data.

## How to use it

The **[cookbook](doc/cookbook/)** is the guide.  Each recipe walks through a complete task, and
the recipes share one continuous worked example drawn from an accelerator facility.

| Recipe | Covers |
|---|---|
| [API conventions](doc/cookbook/conventions.md) | Patterns every call shares: checking results, paging, criteria AND/OR rules, full-replace saves, time handling |
| [Creating and connecting a client](doc/cookbook/connecting.md) | Building an `MldpClient`, config files and environment variables, TLS, logging |
| [Cataloguing PVs](doc/cookbook/pv-metadata.md) | Recording what a PV is, then finding PVs by property instead of by name |
| [Recording machine configuration](doc/cookbook/machine-configuration.md) | Defining configurations, recording when each was active, and answering "what was the machine doing at 18:04?" |
| [Querying time-series data](doc/cookbook/query.md) | Retrieving samples by PV, metadata, or machine configuration, and converting to pandas / NumPy / Excel |

Every Python snippet in the cookbook is mechanically syntax- and type-checked against the
installed package.

For further examples, see the [integration tests](tests/integration).  For the wire protocol
beneath this library — the protobuf messages and RPC semantics, documented in Java — see the
[dp-grpc cookbook](https://github.com/osprey-dcs/dp-grpc/tree/main/doc/cookbook).
