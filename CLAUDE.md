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
# Run unit tests only -- no external services required.  This is what CI runs.
pytest tests/unit/

# Run specific test file
pytest tests/unit/test_ingestion_client.py -v

# Run everything, INCLUDING integration tests.  These need a live MLDP ecosystem on
# localhost:50051-50053 -- however started (docker compose, or the services run
# directly); without it they self-skip.
pytest tests/
```

### Linting and Formatting
The project uses [ruff](https://docs.astral.sh/ruff/) for both linting and formatting,
configured under `[tool.ruff]` in `pyproject.toml` (line length 120, target py310).
```bash
ruff check .              # lint
ruff check . --fix        # lint, applying safe autofixes
ruff format .             # format
ruff format --check .     # verify formatting without writing (what CI runs)
```
Two paths are excluded deliberately: `src/dp_python_lib/grpc/` (generated, regenerated
wholesale) and `*.md` (ruff would reformat the hand-wrapped Python snippets in this file
and `doc/cookbook/`; those are verified instead by `.dev/tools/check-cookbook-snippets.py`).

When a rule fires on something intentional, suppress it with a per-line `# noqa: RULE` plus
a comment saying why, rather than reshaping correct code to satisfy the linter.  Existing
examples: the naive-datetime test inputs (`DTZ001`) that exist precisely to assert a
`ValueError`, and the numpy import that doubles as the `[analysis]` availability probe (`F401`).

### Continuous Integration and Releases
GitHub Actions workflows live in `.github/workflows/`:

- **`ci.yml`** — runs on PRs targeting `main` and on pushes to `main`.  Three jobs:
  unit tests across Python 3.10–3.13; a single-interpreter quality job (ruff lint,
  ruff format check, cookbook snippet checker); and a build job that produces the
  wheel/sdist, runs `twine check --strict`, and verifies the wheel imports in a
  clean venv.  Integration tests are deselected with `-m "not integration"`.
- **`release.yml`** — runs on `rel-X.Y.Z` tag pushes.  Builds the wheel and sdist,
  asserts the setuptools-scm version matches the tag, emits a `SHA256SUMS` file,
  signs everything with keyless Sigstore, and publishes a GitHub Release.  A
  `workflow_dispatch` trigger allows rehearsing the whole path without cutting a
  tag: publishing is gated on a `rel-` tag push, so a manual run always stops after
  build/verify/sign.  A PyPI publish job is wired up but disabled (`if: false`); the
  comment block above it lists the steps to enable it.

**Action pinning**: every `uses:` reference in both workflows is pinned to a full commit
SHA with a trailing `# vX.Y.Z` comment naming the version — a tag is mutable, so whoever
controls an action's repo can repoint it and every workflow picks up the new code with no
diff and no review.  This matters most in `release.yml`, which holds an OIDC signing token
and release write access, and whose third-party actions (Sigstore, `action-gh-release`,
`gh-action-pypi-publish`) run inside that trust boundary.  Pinning does not interfere with
keyless signing or PyPI trusted publishing: both key on the workflow's OIDC identity rather
than the action version, and SHA pinning is what PyPA recommends.  Dependabot bumps the SHA
and its comment together, so pins stay current.  New references follow the same format; the
check is:
```bash
grep -rnE 'uses: *[^ ]+@' .github/workflows/ | grep -vE '@[0-9a-f]{40} # v'   # must return nothing
```

**Cutting a release**: the version comes from the git tag alone (setuptools-scm),
so there is no version to bump in a file.  Tag `rel-X.Y.Z` and push the tag.
The tag must be exactly `rel-X.Y.Z` with no suffix — prerelease shapes like
`rel-1.15.0-rc1` are rejected up front, because setuptools-scm would normalize them
(`1.15.0rc1`) and fail the tag-vs-built version assertion with a confusing error.

### Dependencies
Core dependencies are managed in `pyproject.toml`:
- `grpcio` - gRPC runtime
- `grpcio-tools` - gRPC development tools  
- `protobuf` - Protocol Buffers runtime
- `pydantic-settings` - Type-safe configuration with environment variable support
- `PyYAML` - YAML file parsing

Optional extras:
- `[analysis]` - `pandas`, `numpy`, `openpyxl` for the query-result conversions
- `[dev]` - `pytest`, `mypy`, `ruff`; install with `pip install -e ".[analysis,dev]"`

## Ticket Planning Workflow

Every non-trivial ticket gets a **version-controlled plan** at `plan/tickets/<issue>/plan.md`, the
same convention dp-grpc and dp-service use (see `plan/README.md`).  This replaces the older practice
of keeping plans in the gitignored `.dev/plan/issue-<n>/` directory — plans there were invisible to
reviewers, to CI, and to anyone working from a fresh clone.  `.dev/` remains in `.gitignore` and still
holds scratch material and the cookbook snippet checker; do not add new ticket plans there.

Scratch and draft material stays outside the repo, under `~/dp/dev/tickets/dp-python-lib/<issue>/`.
The distinction is intent, not format: a draft being iterated on is scratch; the plan the
implementation will be reviewed against belongs under `plan/tickets/`.

**Triage before planning.**  Verify the ticket's stated premises against the code, the generated
stubs, and the upstream repos before writing the plan.  Two things are specific to this repo: the
stubs in `src/dp_python_lib/grpc/` may lag dp-grpc `main` (check the last `grpc-sync-*` PR against
the upstream merge dates), and server behavior lives in dp-service, whose `plan/tickets/` records
say what the handlers actually do.  Where triage contradicts the ticket, update the issue description
and say so explicitly in the plan's Background section rather than silently planning around it.

**Plan structure** (see `plan/tickets/6/plan.md` for a worked example):

- **Overview** — what the ticket delivers, and for whom.
- **Background / triage findings** — verified facts, especially anything that contradicts the issue
  as filed; for this repo that includes the authoritative message shapes introspected from the
  regenerated stubs and the server behaviors the client must encode.
- **Design decisions** — the choices a reviewer would otherwise have to reverse-engineer, each with
  its rationale and the alternative that was rejected.
- **Implementation tasks** — per file, concrete enough to execute without re-deriving the design.
- **Out of scope** — with a pointer to the ticket that owns each excluded item.
- **Dependencies and sequencing** — what blocks on what, and explicitly what does *not*.
- **Open questions** — each with context and a recommendation, resolved in place (dated) before
  implementation starts.

Record findings that outlive the ticket in this file rather than leaving them only in the plan: a
plan documents one change, `CLAUDE.md` documents the invariant it established.

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
- `src/dp_python_lib/client/annotation_client.py` - Annotation service facade; groups feature-scoped clients sharing the one `DpAnnotationService` channel (`.pv_metadata`, `.machine_config`, `.sample_status`, `.datasets`, `.annotations`, `.export` — every implemented `DpAnnotationService` feature area)
- `src/dp_python_lib/client/pv_metadata_client.py` - PV metadata client (`save_pv_metadata()`, `get_pv_metadata()`, `query_pv_metadata()`, `iter_pv_metadata()`, `delete_pv_metadata()`) plus the `PvMetadataQuery` (`Q`) criterion helpers
- `src/dp_python_lib/client/machine_config_client.py` - Machine configuration client covering both configurations (`save_configuration()`, `get_configuration()`, `query_configurations()`, `iter_configurations()`, `delete_configuration()`) and their temporal activations (`save_configuration_activation()`, `get_configuration_activation()`, `query_configuration_activations()`, `iter_configuration_activations()`, `delete_configuration_activation()`, `get_active_configurations()`). Includes the `ConfigurationQuery` (`C`) and `ConfigurationActivationQuery` (`CA`) criterion helpers. The shared time converters it used to own now live in `time_conversions.py`. Get/delete activation take a composite key (`client_activation_id` XOR `configuration_name`+`start_time`). Activation `end_time` is optional — omit it for an open-ended activation ("still in effect"); the field is then genuinely absent on the wire
- `src/dp_python_lib/client/sample_status_client.py` - Sample status client (`save_sample_statuses()`, `query_sample_statuses()`, `iter_sample_statuses()`, `iter_sample_statuses_stream()`, `delete_sample_statuses()`) plus the `SampleStatusColumn` / `SampleStatusFrame` construction classes.  The `sampling_clock()` / `timestamp_list()` axis builders now live in `data_frame.py` (issue #6 Phase 2, once calculations frames became a second caller) and are re-exported here, so existing imports are unaffected. A status's identity key is `(pvName, timestamp, domain, layer)`; `delete_sample_statuses()` requires either `pv_names` or an explicit `all_pvs=True` opt-in for the destructive wildcard
- `src/dp_python_lib/client/sample_status_conversions.py` - Per-sample expansion of query results (no optional extras required): `expand_data_timestamps()` (SamplingClock positions computed in **integer nanoseconds**, never float seconds — the exact-match contract depends on it), `bucket_to_rows()` / `buckets_to_rows()` / `iter_rows()` yielding `SampleStatusRow` objects with absent confidence/reason surfaced as `None` rather than fabricated `0.0`/`""`
- `src/dp_python_lib/client/dataset_client.py` - DataSet client (`save_dataset()`, `get_dataset()`, `query_datasets()`, `iter_datasets()`, `delete_dataset()`, plus the `get_datasets(ids)` batch fetch that avoids the annotation-listing N+1) with the `DataSetQuery` (`DS`) criterion helpers and the `data_block()` builder. `data_block()` is the only place `begin < end` is checked — the server does not
- `src/dp_python_lib/client/annotations_client.py` - Annotations client (`save_annotation()`, `get_annotation()`, `query_annotations()`, `iter_annotations()`, `delete_annotation()`, `get_calculations()`) with the `AnnotationQuery` (`AQ`) criterion helpers and the `calculations()` builder, which takes a `dict[str, DataFrame]` so frame-name uniqueness is true by construction. Note `AnnotationsClient` (feature client) vs `AnnotationClient` (facade)
- `src/dp_python_lib/client/export_client.py` - Export client (`export_data()`) with the `ExportFormat` str enum and the `calculations_spec()` builder
- `src/dp_python_lib/client/time_conversions.py` - The two shared time converters and the `TimestampInput` alias: `to_timestamp()` (tz-aware datetime / epoch seconds / `common.Timestamp` → `Timestamp`; naive datetimes raise) and its inverse `to_epoch_nanos()` (`Timestamp` → integer epoch nanoseconds), plus `NANOS_PER_SECOND`. **The datetime path uses integer arithmetic, never `datetime.timestamp()`** — that returns a float64, which cannot hold present-day epoch seconds at sub-microsecond resolution and moved 99.7% of microsecond datetimes by up to ~119ns, breaking the exact-match contract sample status and provenance depend on. Both were defined in `machine_config_client` as the first module to need them, and six others grew imports from there — which read as though datasets, queries, and DataFrames depended on the machine configuration API; `to_epoch_nanos()` had also been written out privately three separate times. **A leaf module**: it imports only stdlib and the generated protos, so any client module can use it without an import cycle. New time conversions belong here
- `src/dp_python_lib/client/data_frame.py` - Builders for `common.DataFrame`, the shared time-series payload (also ingestion's `ingestionDataFrame`, so #17 extends this rather than forking it): the `sampling_clock()` / `timestamp_list()` / `timestamp_count()` axis helpers **relocated here from `sample_status_client`** (and re-exported from it, so existing imports keep working), the typed scalar column builders (`double_column`, `float_column`, `int64_column`, `int32_column`, `bool_column`, `string_column`, `enum_column`), the legacy `data_column()` escape hatch (a `None` entry becomes an unset oneof — the only way to express a gap on a shared axis), the provenance helpers (`column_metadata`, `provenance`, `pv_source`, `calculations_source`), and `data_frame()` assembly, which routes columns by type and enforces the server's **shape** rules client-side (non-blank names, non-empty values, count match, name uniqueness across all types) while leaving its size caps server-side. Column names must be **non-blank**, not merely non-empty, and `timestamp_count()` validates a hand-built axis the way the builders do: a `SamplingClock` needs a positive `periodNanos` as well as a non-zero count, and a `TimestampList` must be **strictly increasing** (duplicates included — two samples cannot claim one instant). The read path rejects all of these, and anything `data_frame()` accepts must be readable back. Shared with `SampleStatusFrame`, which validates the same way. Sample counts come from the right field per kind: `dataValues` for a `DataColumn`, `images` for an `ImageColumn` (which has no `values` field at all), and `len(values) / prod(dims)` for an array column. An array column's sample count is `len(values) / prod(dims)`, and a value count that is not a **whole multiple** of `prod(dims)` is rejected rather than floor-divided into a passing count — the read path applies the same rule, so a frame this accepts is always one `data_frame_conversions` can read back. `data_column()` maps integers and floats by `numbers.Integral` / `numbers.Real` (and NumPy's bool by type name, without importing NumPy), so NumPy scalars map like their Python counterparts: `np.float64` subclasses `float` but `np.int64` and `np.bool_` subclass nothing here, and matching on exact Python type would accept some and reject others. Array/image/struct/serialized builders are #17's; hand-built ones pass through
- `src/dp_python_lib/client/data_frame_conversions.py` - Reading a `DataFrame` back. Pure Python (no extras): `data_frame_timestamps()` (integer-nanosecond axis expansion), `column_values()` (standalone per-column converter, written so the bucket query #16 can reuse it; it yields exactly one entry per sample for every column kind, with the structural fields a payload cannot be interpreted without kept in companion accessors: array dims via `column_dimensions()` / `data_frame_column_dimensions()` so `[2,2]` and `[4]` stay distinguishable, an image's width/height/channels/encoding via `image_descriptor_dict()` / `data_frame_image_descriptors()`, and a struct's `schemaId` via `column_schema_id()` / `data_frame_schema_ids()`), `data_frame_columns()`, `column_metadata_dict()`. An axis that is set but empty is rejected rather than converted to a zero-row table. Behind `[analysis]`: `data_frame_to_pandas()` (UTC index built from int64 nanos, `ColumnMetadata` in `df.attrs`; each Series carries the **narrow dtype its column type implies** — `float32`/`int32`, not pandas' widened inference), `data_frame_from_pandas()` (dtype→typed column; a NaN anywhere is fail-loud, since a dense typed column cannot express a gap; rebuilds each column's `ColumnMetadata` from `df.attrs` via `column_metadata_from_dict()`). **A frame round-trips through pandas to byte equality** apart from the deliberate `SamplingClock`→`TimestampList` axis change: column types and provenance both survive. An `EnumColumn`'s codes are int32 and indistinguishable from a plain `Int32Column` by dtype, so its `enumId` rides in `df.attrs["enum_ids"]` — carried even under `exclude_column_metadata=True`, since without it the column cannot be rebuilt as an enum at all, and the `calculations_to_dataframes()` / `calculations_from_dataframes()` bridges. The pandas direction always emits a `TimestampList`, never an inferred `SamplingClock`; reads each instant as `Timestamp.value` (**always nanoseconds**, unlike a raw int64 view, which is in the index's own storage unit — a `datetime64[us]` index viewed as int64 lands 1000× too early); rejects `NaT`, whose integer form is a valid-looking instant; and rejects duplicate column names up front (a duplicated label makes `df[name]` a DataFrame, which would otherwise fail deep inside as pandas' "truth value of a Series is ambiguous"). `column_metadata_dict()` reports **only the origin arm actually set** on each provenance source — a PV source has no `calculations_column` key and vice versa — plus `time_range` as epoch nanoseconds when present; an absent range has no key rather than a fabricated `(0, 0)`. A pandas round trip preserves every value, dtype, and timestamp but **not column order**: a `DataFrame` stores each column kind in its own repeated field, so columns come back grouped by type
- `src/dp_python_lib/client/query_client.py` - v2 time-series query client (sample-oriented) exposed as `client.query`. Low-level wrappers `query_samples()` (unary, one resumable page) and `iter_query_samples()` (transparent paging), plus `iter_query_samples_stream()` (server-streaming, fire-and-consume, lazy). Queries are described by a kind-neutral `QueryParams` built from the `PvQuery` (`PV`) and `ConfigQuery` (`CFG`) criterion helpers; shares a `_build_query_spec()` seam so a future bucket request builder reuses it. Results wrap the raw `ColumnTable` (`.column_table`, `.next_page_token`); `.to_dataframe()`/`.to_numpy()` delegate to `query_conversions` (Phase 2, optional `[analysis]` extra)
- `src/dp_python_lib/client/query_conversions.py` - Pythonic conversions for query results (optional `[analysis]` extra: pandas/numpy/openpyxl, imported lazily). `data_value_to_python()` (oneof extractor: scalars→native, timestamp→epoch-nanos, array→list, structure→dict, image→`Image` wrapper, fail-loud on unhandled arm), `column_table_to_dataframe()` (UTC datetime index + one column per DataColumn; dense-alignment and duplicate-column-name fail-loud; ColumnMetadata in `df.attrs`), `column_table_to_numpy()` (dict of 1-D arrays; complex arms stay 1-D object arrays rather than collapsing to 2-D), `dataframe_to_excel()` (thin `to_excel()` wrapper: row-limit guard, tz-drop, complex-cell stringification), and `query_samples_to_dataframe()`/`stream_query_samples_to_dataframes()` whole-query conveniences (unary concats by column name; streaming yields per-page frames lazily)
- `src/dp_python_lib/client/service_api_client_base.py` - Base class for the service clients: owns the channel and the one-per-client gRPC stub, and provides `_dispatch()`, the shared three-tier sender that all 18 unary `_send_*` methods delegate to
- `src/dp_python_lib/client/query_support.py` - Helpers shared by the criteria-based query clients, currently `check_at_most_one_text_criterion()` (Mongo cannot AND two `$text` clauses). It is generic over the different criterion types because each names its oneof `criterion` and its full-text arm `textCriterion`. New shared query helpers belong here rather than in whichever feature client happened to need one first — importing a private name across feature modules makes the importing module's dependencies misleading
- `tests/unit/test_service_api_client_base.py` - Unit tests for `_dispatch` itself (success, business error, unrecognized response, `RpcError` with and without a resolvable `code()`, unexpected exception, and the `request_log`/`success_log` hooks)
- `tests/unit/test_ingestion_client.py` - Unit tests for IngestionClient functionality
- `tests/unit/test_pv_metadata_client.py` - Unit tests for PvMetadataClient functionality
- `tests/unit/test_machine_config_client.py` - Unit tests for the Configuration side of MachineConfigClient
- `tests/unit/test_machine_config_activation_client.py` - Unit tests for the ConfigurationActivation side of MachineConfigClient (incl. composite-key validation, timestamp handling, getActiveConfigurations)
- `tests/unit/test_sample_status_client.py` - Unit tests for SampleStatusClient (frame/column validation, axis builders, three-tier error handling, paging, streaming, delete opt-in, `limit=0` regression)
- `tests/unit/test_sample_status_conversions.py` - Unit tests for sample_status_conversions (nanosecond-exact axis expansion, absent-vs-zero confidence, alignment fail-loud, laziness)
- `tests/unit/test_dataset_client.py` - Unit tests for DataSetClient (request building, `DataSetQuery` incl. key-only attributes, `data_block()` validation, three-tier error handling, paging, `get_datasets()` dedup/empty/absent-id)
- `tests/unit/test_annotations_client.py` - Unit tests for AnnotationsClient (incl. `calculations()`, absent-vs-empty calculations on save, the `calculations_id` empty-string-vs-None rule, three-tier error handling, paging)
- `tests/unit/test_export_client.py` - Unit tests for ExportClient (`ExportFormat` mapping and unreachable `UNSPECIFIED`, `calculations_spec()`, the zero-source rejection, three-tier error handling)
- `tests/unit/test_annotation_client.py` - Unit tests pinning the `AnnotationClient` facade wiring (every feature client present, one shared channel, one stub apiece)
- `tests/integration/test_datasets_annotations_integration.py` - Live-server round trip for datasets/annotations/calculations; ingests its own samples first, because `saveDataSet` requires archived PVs
- `tests/unit/test_time_conversions.py` - Unit tests for the shared time converters (`to_timestamp()` input forms and the naive-datetime/bool/unsupported-type rejections; `to_epoch_nanos()` exactness and its round trip with `to_timestamp()`)
- `tests/unit/test_data_frame.py` - Unit tests for the data_frame builders (axis relocation, each typed column, `data_column()` bool-before-int and unset-oneof handling, provenance helpers, and every `data_frame()` shape rule incl. array dims and serialized-column name-only checks)
- `tests/unit/test_data_frame_conversions.py` - Unit tests for data_frame_conversions (nanosecond-exact expansion, per-column conversion incl. array reshaping, duplicate-name fail-loud, and — skipping cleanly without `[analysis]` — the pandas round trip, dtype mapping, and NaN fail-loud)
- `tests/unit/test_query_client.py` - Unit tests for QueryClient (request building, three-tier error handling, unary paging, streaming, `PvQuery`/`ConfigQuery` helpers, `QueryParams` validation)
- `tests/unit/test_query_conversions.py` - Unit tests for query_conversions (each DataValue arm, dense-alignment and duplicate-column-name fail-loud, int-gap float-upcast, timestamp columns, 1-D object arrays for complex arms, metadata in attrs, concat-by-name, Excel row-limit/stringification/native-bytes; DataFrame/NumPy/Excel tests skip cleanly when the `[analysis]` extra is absent)
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
- **Every unary `_send_*` method delegates to `ServiceApiClientBase._dispatch()`** rather than writing
  the three-tier block out by hand (issue #14; `plan/tickets/14/plan.md`).  A sender is now the call
  itself plus its log messages:
  ```python
  def _send_query_pv_metadata(self, request: annotation_pb2.QueryPvMetadataRequest) -> QueryPvMetadataApiResult:
      return self._dispatch(
          self._stub.queryPvMetadata,        # the bound stub method
          request,
          QueryPvMetadataApiResult,          # result class; all take (is_error, message, response)
          "pvMetadataResult",                # the success oneof field
          "queryPvMetadata",                 # op name, used in log and error messages
          request_log=lambda: self.logger.info(
              "Calling queryPvMetadata API with %d criteria", len(request.criteria)),
          success_log=lambda response: self.logger.info(
              "QueryPvMetadata returned %d records", len(response.pvMetadataResult.pvMetadata)),
      )
  ```
  `request_log` / `success_log` are optional: omit them and `_dispatch` logs a generic
  `"Calling <op> API"` / `"<op> completed successfully"`.  Pass one whenever the message names the
  entity or reports a count off the response — that detail is the reason the parameters exist, and
  nothing in the test suite asserts on log content, so a dropped message fails silently.
- **The server-streaming senders deliberately do not use `_dispatch`.**
  `_send_query_samples_stream()` and `_send_query_sample_statuses_stream()` *yield* one result per
  streamed message — error results included, for the public `iter_*` wrapper to convert into a
  `RuntimeError` — which is a different contract from returning a single result.  Leave them as they are.
- Where one gRPC service backs several feature areas (e.g. `DpAnnotationService` covers PV metadata,
  machine configuration, and annotations), use a lightweight facade (`AnnotationClient`) that owns the
  shared channel and exposes feature-scoped clients as attributes (`annotation.pv_metadata`).  This
  keeps each feature client cohesive while matching the single-service reality of the gRPC API.

### gRPC Error Handling
- Use **synchronous gRPC calls** with `DpIngestionServiceStub` for simplicity
- **Three-tier error handling**, implemented once in `ServiceApiClientBase._dispatch()` and inherited by
  every unary sender (see the Client Implementation Pattern above):
  1. **gRPC Exceptions** (`grpc.RpcError`) - network/connection errors
  2. **Business Logic Errors** - check response `exceptionalResult` field
  3. **General Exceptions** - unexpected errors
- A response carrying neither `exceptionalResult` nor the expected success field is itself an error, so an
  unrecognized response shape is never mistaken for a success.
- Check protobuf union fields with `response.HasField('fieldName')`
- Return consistent result objects with `is_error` flag and appropriate messages
- The error-message text is part of the contract — roughly 40 unit tests match on it with `assertIn`.
  `_dispatch` produces `f"gRPC error: {e.details()}"`, `f"Unexpected error: {e!s}"`, and
  `f"Unexpected response format: neither exceptionalResult nor {success_field} found"`.
- When logging an `RpcError`, `_dispatch` includes `e.code()` only when it resolves: a bare
  `grpc.RpcError()`, which is what the test mocks raise, has no usable code.
- `_dispatch` names the operation with the raw camelCase `op_name` in all three error-log tiers.  The
  hand-written senders capitalized it in the business-error warning only (`SavePvMetadata API returned
  business error`), while their other two error logs already used camelCase; the refactor dropped that
  one inconsistency deliberately.  Log text only -- returned messages are unchanged.

### Testing Best Practices
- Use `@patch` decorators to mock gRPC stubs and avoid real network calls
- Mock the response behavior with `side_effect` for conditional logic (e.g., `HasField`)
- Always verify mocks were called correctly with `assert_called_once_with()`
- Test all error scenarios: success, business errors, gRPC exceptions, and unexpected cases

### Type Hints and Modern Python
- **All framework classes use comprehensive type hints.**  The project targets Python 3.10+
  (`requires-python = ">=3.10"`), so use the modern built-in spellings — ruff enforces this
  via the `UP` (pyupgrade) rules:
  - `str | None`, **not** `Optional[str]`
  - `list[str]`, `dict[str, str]`, **not** `List[str]` / `Dict[str, str]`
  - `int | str` unions, **not** `Union[int, str]`
- Most of `typing` is therefore unnecessary; `from typing import Optional, Dict, List` should
  not appear in new code.  `typing` is still needed for things with no builtin equivalent
  (e.g. `Any`, `TYPE_CHECKING`), and `collections.abc` supplies `Callable`, `Iterator`, etc.
- gRPC-specific types: `grpc.Channel`, `ingestion_pb2.RegisterProviderRequest`
- Return type annotations: `-> None`, `-> RegisterProviderApiResult`

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
        try:
            ...
        except Exception as e:
            # Inside an except block use .exception(), which captures the traceback
            # automatically.  ruff's G201 rule rejects .error(..., exc_info=True).
            self.logger.exception("Serious problem: %s", str(e))
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

# end_time is optional: omit it for an open-ended activation ("still in effect").
# Close it later by re-saving with the same client_activation_id and a real end_time.
mc.save_configuration_activation(SaveConfigurationActivationRequestParams(
    configuration_name="beamline-optics", start_time=start,
    client_activation_id="act-open", modified_by="operator"))

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
  config-only query is legal).  Empty inputs raise `ValueError`, as does a negative `limit` (`limit=0` is
  meaningful — the server picks a default).
- `PvQuery` (`PV`) selectors: `name_list(values)` / `pattern(str)` / `metadata([...])`, whose criteria are
  `pv_name(exact=, prefix=, contains=)` / `aliases(exact=, prefix=, contains=)` (each repeated & coexisting) plus
  `tags(values)` / `attr(key, values)`.  `ConfigQuery` (`CFG`) criteria:
  `configuration_name`/`client_activation_id`/`category`/`tags` each `(values)`, and `attr(key, values)`.
- Serialized columns are deferred: `useSerializedColumns` is forced `False`; a result carrying
  `serializedDataColumns` raises `NotImplementedError` in the conversion layer.
- `DataValue` mapping: scalars→native dtypes, `timestampValue`→`datetime64[ns, UTC]`, integer columns with gaps
  upcast to `float64`; complex arms preserved losslessly (`arrayValue`→list, `structureValue`→dict,
  `byteArrayValue`→bytes, `imageValue`→`Image(data, file_type)`); an unhandled oneof arm raises.  `DataValue.valueStatus`
  no longer exists: it was removed in dp-grpc 1.16.0 (field 15 reserved) in favor of the sample status API, and was
  never populated in `querySamples()` results before that.  Per-column `ColumnMetadata` lands in
  `df.attrs["column_metadata"]` unless `exclude_column_metadata=True`.
- Both conversions key columns by `DataColumn.name`, so a `ColumnTable` carrying two columns with the same name
  raises `ValueError` rather than silently dropping the earlier one.  `column_table_to_numpy()` always returns
  1-D arrays: complex arms become 1-D object arrays, so an array-valued column never collapses into a 2-D array
  just because its rows happen to be equal-length.
- Future PyTorch support (mentioned indirectly by the customer; to be confirmed in the post-implementation
  requirements scan) is additive: `column_table_to_numpy()`'s dict-of-arrays is the intended substrate for a
  `column_table_to_torch()` behind a separate optional `[torch]` extra — no change to `QueryClient` or the NumPy
  path. Not built yet.

### DataSets, Annotations, and Export API (Annotation Service)

Issue #6 (`plan/tickets/6/plan.md`) added three feature clients on the `annotation` facade:
`client.annotation.datasets` (`DataSetClient`), `client.annotation.annotations` (`AnnotationsClient`), and
`client.annotation.export` (`ExportClient`).  A DataSet names a region of the archive; an Annotation describes one
or more DataSets and may own a Calculations payload of derived values; export writes any of it to a file on the
server.  Worked example: `doc/cookbook/datasets-and-annotations.md`.

```python
from datetime import datetime, timezone
from dp_python_lib.client import (
    MldpClient, SaveDataSetRequestParams, SaveAnnotationRequestParams, ExportDataRequestParams, ExportFormat,
    DataSetQuery as DS, AnnotationQuery as AQ, data_block, calculations, calculations_spec, sampling_clock,
)
from dp_python_lib.client import data_frame as dfb
from dp_python_lib.client import data_frame_conversions as dfc

client = MldpClient()
ds, an, ex = client.annotation.datasets, client.annotation.annotations, client.annotation.export
t0 = datetime(2026, 2, 2, 18, tzinfo=timezone.utc)
t1 = datetime(2026, 2, 2, 19, tzinfo=timezone.utc)

# a region of the archive: one DataBlock per (time range, PV list).  Every PV must already have
# ingested data -- see the archive-existence invariant below.
dataset_id = ds.save_dataset(SaveDataSetRequestParams(
    name="CXI shift, hour 1", owner_id="cmcchesney",
    data_blocks=[data_block(t0, t1, ["BPMS:GUNB:314:X"])],
    tags=["cxi-3443"], attributes={"EXP": "CXI_3443"}, modified_by="cmcchesney")).dataset_id

# derived values with column-level provenance, one frame per time axis
frame = dfb.data_frame(
    sampling_clock(start_time=t0, period_nanos=1_000_000_000, count=3),
    [dfb.double_column("x_rms", [0.31, 0.29, 0.33], metadata=dfb.column_metadata(
        provenance=dfb.provenance(process="1 Hz RMS",
                                  derived_from=[dfb.pv_source("BPMS:GUNB:314:X", (t0, t1))])))])
saved = an.save_annotation(SaveAnnotationRequestParams(
    name="Orbit drift", owner_id="cmcchesney", dataset_ids=[dataset_id],
    tags=["reviewed"], calculations=calculations({"orbit-rms": frame})))

# find it again; query results carry ids, so resolve a page's datasets in ONE call
for a in an.iter_annotations([AQ.tags(["reviewed"]), AQ.attributes("EXP")]):   # key-only search
    print(a.name, a.dataSetIds, bool(a.calculationsId))
datasets = ds.get_datasets([i for a in an.iter_annotations([AQ.datasets([dataset_id])])
                            for i in a.dataSetIds])

# read the calculations back (get_annotation is the only method returning them inline)
calcs = an.get_calculations(saved.calculations_id).calculations
columns = dfc.data_frame_columns(calcs.calculationDataFrames[0].frame)   # plain Python, no extras
frames = dfc.calculations_to_dataframes(calcs)                          # pandas, [analysis] extra

# export; then tear down annotations first, since delete_dataset is refused while referenced
ex.export_data(ExportDataRequestParams(ExportFormat.HDF5, dataset_id=dataset_id,
                                       calculations_spec=calculations_spec(saved.calculations_id)))
an.delete_annotation(saved.annotation_id)
ds.delete_dataset(dataset_id)
```

Invariants worth knowing before touching this code:

- **`saveDataSet` requires every PV named in a data block to already exist in the archive** — that is, to have
  *ingested data*.  The server's error text says `no PV metadata found for names: [...]`, but the check is a
  `distinct` on `pvName` over the **buckets** collection (`MongoAnnotationHandler.validateSaveDataSetRequest` →
  `MongoSyncQueryClient.executeQueryPvExistence`), so saving PV metadata does **not** satisfy it.  Nothing is
  validated client-side (the client cannot know what is archived), but any test or example must use an archived PV.
  `ingestData()` acks *before* the bucket becomes queryable, so a `saveDataSet` issued immediately after ingesting
  still fails; `tests/integration/test_datasets_annotations_integration.py` probes until it succeeds rather than
  sleeping a fixed interval, and ingests through the generated stub because `IngestionClient` wraps only
  `registerProvider()` until #17.
- **The server does not check `begin < end` on a `DataBlock`** (it checks only that each bound is non-zero and that
  `pvNames` is non-empty, and never compares the two bounds), so `data_block()`'s check is the only one there is.
- **A `DataBlock`'s range is half-open, `[begin, end)`** — the same convention as the v2 query API's `QueryParams`,
  established by reading dp-service rather than the proto, which says nothing.  `saveDataSet` never compares the
  bounds at all; the interval acquires meaning only at export.  There, bucket selection and per-sample trimming are
  literally the *same* functions `querySamples()` uses (`MongoQueryFilterBuilder.bucketOverlapsRangeFilter` and
  `TabularDataUtility.isRetained`, whose contract is "a sample exactly at an interval's end belongs to the next
  interval, not this one").  So back-to-back blocks cover the boundary sample exactly once.  **The exception is
  HDF5**: `ExportDataJobAbstractBucketed` writes every *overlapping bucket* whole and untrimmed, with no time range
  passed to the writer, so an HDF5 export can contain samples outside the requested range and can write a straddling
  bucket twice.  Nothing client-side can change that — it is a property of the format, and worth telling users.
- **Delete-not-found is a business error**, not a silent success, on both `delete_dataset()` and
  `delete_annotation()`.  `delete_dataset()` is also refused while any annotation references the dataset — delete the
  annotations first; there is deliberately no cascade.
- **`save_annotation()` replaces in full, including calculations**: omitting them clears *and deletes* the stored
  object, and a replacement returns a new `calculationsId`.  `get_annotation()` is the only method returning
  calculations inline; `query_annotations()` results carry the id with empty content.
- Two conventions these clients introduced, both following the proto, **now shared by every criteria-based client**
  after the [#40](https://github.com/osprey-dcs/dp-python-lib/issues/40) /
  [#41](https://github.com/osprey-dcs/dp-python-lib/issues/41) back-ports (`plan/tickets/40/plan.md`,
  `plan/tickets/41/plan.md`):
  - **`attributes(key)` / `attr(key)` accept an absent or empty `values` list as a key-only existence search** —
    match every record possessing the key, whatever its value.  All seven attribute helpers now do this
    (`PvMetadataQuery`, `ConfigurationQuery`, `ConfigurationActivationQuery`, `DataSetQuery`, `AnnotationQuery`,
    `PvQuery.attr`, `ConfigQuery.attr`).  It is the one exception to the "helpers reject empty input" rule, because
    unlike `tags([])` it *narrows* the result set rather than matching everything.  The **key** is still required,
    and that check is load-bearing on the two v2 query selectors, where the server does not validate it: a blank key
    would reach Mongo as an existence test on `"attributes."` and silently match nothing.  Server side, empty values
    build `Filters.exists("attributes.<key>")` (`MongoQueryFilterBuilder.attributeFilter()`), not an `$in: []`
  - **`criteria` is optional on every paged annotation-service query/iter method**, because the server treats an
    empty list as match-all — `iter_pv_metadata()` with no arguments is the browse-all form.  The server's default
    page size (100, hardcoded in `MongoSyncAnnotationClient.DEFAULT_QUERY_LIMIT`, **not** configurable) applies
    **unconditionally**: dropping the last criterion does not change the page size, so a bare `query_*()` still
    returns one page and a token.  That is why `iter_*` is the right call for browsing.  Note the v2 query methods
    are deliberately *not* included: `QueryParams` still requires a PV selector or config criteria, since a
    time-series query with no selection is unbounded rather than a browse-all
- `ExportFormat` makes the server-rejected `EXPORT_FORMAT_UNSPECIFIED` unreachable, and `ExportDataRequestParams`
  requires at least one of `dataset_id` / `data_blocks` / `calculations_spec`.  The exported file lives on the
  **server's** filesystem and there is no retrieval RPC, so there is no download convenience.
- **Empty string and `None` mean different things on two result properties**, and the distinction is load-bearing in
  both: `SaveAnnotationApiResult.calculations_id` is `""` when the request carried no calculations but `None` when
  the call *failed*, and `ExportDataApiResult.file_url` is `""` when the deployment simply does not publish exports
  over HTTP but `None` on error.  In both cases `""` is a successful outcome — test with `is None`, not truthiness.
- **The params classes validate the server's required fields client-side.**  `SaveDataSetRequestParams` requires
  `name` / `owner_id` / `data_blocks`, and `SaveAnnotationRequestParams` requires `name` / `owner_id` /
  `dataset_ids`, each raising `ValueError` naming the field rather than spending a round trip to learn it.
- **`get_datasets()` chunks its id list** (`ID_QUERY_CHUNK_SIZE`, 100) because the ids come from the `dataSetIds` of
  a whole page of annotations and are effectively unbounded; one criterion carrying all of them becomes a single
  large `$in` and an oversized request message.  A short result is logged at WARNING, since an id withheld for any
  other reason is indistinguishable from a dangling one.
- `patchDataSet` / `patchAnnotation` are reserved "not implemented" placeholders and are not wrapped.
- Calculations are built with `data_frame.py` and read back with `data_frame_conversions.py`; both are shared with
  #16/#17 rather than local to this area.  `data_frame_from_pandas()` always emits a `TimestampList`, never an
  inferred `SamplingClock`, and rejects `NaN` fail-loud: a dense typed column cannot express a gap, so use
  `data_column()` or a separate frame.

### Sample Status API (Annotation Service)

Sample status methods are exposed under the `annotation` facade at `client.annotation.sample_status`.  A sample status
assigns an int32 status code to **one PV sample at one instant**; the identity key is `(pvName, timestamp, domain, layer)`.
`domain` names the status-code semantics contract (EnumColumn-style — not validated by MLDP); `layer` names the producer
stream, so an operator override and a model's guess coexist without colliding.  This is the designated replacement for
the former `DataValue.ValueStatus` mechanism, which was removed in dp-grpc 1.16.0 (field 15 reserved).

Two model properties drive the whole design:
- **Absence means "no assertion."**  There is no implicit default status; labeling three samples says nothing about the
  rest.  Absent `confidence`/`reasons` therefore surface as `None`, never as `0.0`/`""` — `0.0` is a legitimate
  confidence value, so a fabricated default would be indistinguishable from a real one.
- **Matching is by exact timestamp at nanosecond precision.**  No nearest-sample search, no tolerance window.  This is
  why `expand_data_timestamps()` computes SamplingClock positions as `startTime + i * periodNanos` in integer
  nanoseconds: present-day epoch nanos need ~61 bits, so a float64 round-trip would silently produce timestamps that
  match nothing.

```python
from datetime import datetime, timezone
from dp_python_lib.client import (
    MldpClient, SampleStatusColumn, SampleStatusFrame, SaveSampleStatusesRequestParams,
    QuerySampleStatusesRequestParams, QueryParams, PvQuery as PV, SampleStatusFilter,
    sampling_clock, timestamp_list,
)
from dp_python_lib.client import sample_status_conversions as ssc

ss = MldpClient().annotation.sample_status
begin = datetime(2024, 2, 2, tzinfo=timezone.utc)
end = datetime(2024, 2, 3, tzinfo=timezone.utc)

# sparse labeling: name exactly the samples being labeled (timestamps taken from the data itself)
bad = [datetime(2024, 2, 2, 18, 4, 12, 250000, tzinfo=timezone.utc)]
ss.save_sample_statuses(SaveSampleStatusesRequestParams(
    frames=[SampleStatusFrame(
        domain="data_quality", layer="operator_override",
        data_timestamps=timestamp_list(bad),
        columns=[SampleStatusColumn("BPMS:GUNB:314:X", status_codes=[2], reasons=["beam loss"])],
    )],
    source="shift log", modified_by="operator"))

# dense labeling: sampling_clock() must match the archived data's clock exactly
axis = sampling_clock(start_time=begin, period_nanos=100_000, count=10_000)

# read back, expanded to one row per sample
params = QuerySampleStatusesRequestParams(begin_time=begin, end_time=end, domains=["data_quality"])
for row in ssc.iter_rows(ss.iter_sample_statuses(params)):
    print(row.pv_name, row.epoch_nanos, row.status_code, row.reason)   # reason is None if unset

# query time-series data with flagged samples removed
q = QueryParams(begin_time=begin, end_time=end, pv_selector=PV.name_list(["BPMS:GUNB:314:X"]),
                sample_status_filter=SampleStatusFilter.exclude("data_quality", status_codes=[2]))

# delete: (domain, layer) is required; the PV wildcard needs an explicit opt-in
ss.delete_sample_statuses(begin, end, domain="ml_anomaly", layer="ml_model_v1", all_pvs=True)
```

Notes:
- Saving is a per-key upsert that **replaces in full**: re-saving with `reasons` omitted clears the stored reason.
  Supply the complete desired state each time.  `source`/`modified_by` apply to the whole request, not per frame.
- A frame's optional parallel arrays (`confidence`, `reasons`) must be omitted or supply exactly one entry per
  timestamp; validated client-side so the error names the offending PV instead of bouncing the whole batch.  A PV may
  appear at most once per frame.  An all-empty `reasons` list is dropped rather than sent as empty strings.
- There is deliberately **no criterion-builder class** here (unlike `PvMetadataQuery`/`ConfigurationQuery`): the request
  takes plain repeated string filters (`pv_names`/`domains`/`layers`, ANDed across, ORed within), so plain lists are the
  honest representation.
- `SampleStatusFilter.include()`/`.exclude()` are the intended ways to build a `sampleStatusSelector`, making the
  server-rejected `MODE_UNSPECIFIED` zero value unreachable through the helpers.  Since the `sample_status_filter`
  parameter still accepts any `SampleStatusSelector`, `QueryParams` also *validates* one that is passed in (non-empty
  domain, mode not `MODE_UNSPECIFIED`), so a hand-built selector fails with a message naming the problem rather than
  as a server rejection.  Because absence means "no assertion", an *unlabeled* sample never matches: `exclude()`
  keeps it, `include()` drops it.
- `sampleStatusSelector` is supported by `querySamples()`/`querySamplesStream()` **only** — the server rejects it on a
  bucket query.  `_build_query_spec()` is shared with the future bucket client (#16) and carries a note at the seam:
  a bucket request builder must *refuse* `sample_status_filter` rather than copy it through.
- `delete_sample_statuses()` is exact at the sample axis (a straddling bucket is not deleted wholesale), and a delete
  matching nothing is a success with `deleted_count == 0`.
- The deferred domain-registry RPCs (`saveSampleStatusDomain` / `querySampleStatusDomains`) are reserved placeholders
  that return "not implemented", so they are not wrapped.
- A pandas view of statuses is deferred; `sample_status_conversions` returns plain Python objects and needs no extras.
- Verified end to end by `tests/integration/test_sample_status_client_integration.py` against a live Annotation
  Service built from dp-service `main` (the 1.16.0 API, **not yet released** — the newest tag everywhere is
  `rel-1.15.0`): exact nanosecond timestamp round-trip through both axis forms, absent-stays-absent, full-replace
  upsert, and layer independence.  The tests probe for the API first and skip with an actionable message against a
  pre-1.16.0 server, since reachability alone does not imply the RPCs exist.  Status *filtering* of query results
  is still unit-tested only — it needs ingested sample data to attach to (#17).

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