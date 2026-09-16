# dp-python-lib 1.16.0 Release Notes

Changes since rel-1.15.0.  This repo publishes the Python client library for the MLDP gRPC API: the
generated stubs synced from [dp-grpc](https://github.com/osprey-dcs/dp-grpc) `rel-1.16.0`, and the
hand-written client wrappers over them.  The protocol changes themselves are described in
[dp-grpc's 1.16.0 notes](https://github.com/osprey-dcs/dp-grpc/blob/main/doc/release-notes/rel-1.16.0.md);
this document covers what they mean for Python callers, and the client API built on top of them.

**This is the largest release the library has had.**  It adds three new API areas — sample status,
datasets/annotations/export, and the `DataFrame` builders and conversions that both depend on — and
brings the client to full coverage of every implemented `DpAnnotationService` feature area.

**It is a breaking release, but only narrowly.**  One protocol field was removed (`DataValue.valueStatus`),
and the server-side semantics of multi-criterion queries changed underneath an unchanged client API.
Every hand-written client signature is either unchanged or widened.  Read
[Upgrading from 1.15.0](#upgrading-from-1150) before bumping the dependency.

## Contents

- [Upgrading from 1.15.0](#upgrading-from-1150)
- [Sample Status API (#8)](#sample-status-api-issue-8)
- [DataSets, Annotations, and Export (#6)](#datasets-annotations-and-export-issue-6)
- [DataFrame builders and conversions (#6)](#dataframe-builders-and-conversions-issue-6)
- [Shared time converters (#6)](#shared-time-converters-issue-6)
- [Key-only attribute search (#40)](#key-only-attribute-search-issue-40)
- [Browse-all queries (#41)](#browse-all-queries-issue-41)
- [`_dispatch` refactor (#14)](#internal-the-_dispatch-refactor-issue-14)
- [Dependency floors](#dependency-floors)
- [Documentation and process](#documentation-and-process)
- [Build and release infrastructure](#build-and-release-infrastructure)

## Upgrading from 1.15.0

Nothing in the hand-written client API was removed or narrowed, so most code moves over untouched.
The two things that can break you are both inherited from the protocol:

1. **Remove every read or write of `DataValue.valueStatus`.**  The field is gone from the stubs
   (dp-grpc #143 reserved field 15 permanently).  Python has no compile step, so this surfaces as an
   `AttributeError` at runtime rather than at build time — grep for it.  The
   [Sample Status API](#sample-status-api-issue-8) below is the designated replacement, and it is
   strictly better: statuses are queryable, correctable, and assignable after ingestion.
2. **Audit every query that passes more than one criterion.**  Criteria now combine with **AND**
   server-side, values within one criterion with **OR**.  Two `Q.tags([...])` entries used to match
   *either* tag and now match *both*.  This is **silent** — no error, a different result set.  To OR
   two tag values, put them in one criterion: `Q.tags(["a", "b"])`.

Two more worth checking, neither of which raises:

3. **`query_*` results may be shorter than they were.**  An unset `limit` now means the server's
   default page size (100, hardcoded and not configurable), not an unbounded result, on every paged
   annotation-service query.  `query_pv_metadata()` in particular was previously unbounded.  If you
   called a `query_*` method and treated the result as complete, switch to the matching `iter_*`
   method, which follows `next_page_token` for you.
4. **Raise your `grpcio` floor if you pin it.**  See [Dependency floors](#dependency-floors) — the
   regenerated stubs raise at import on grpcio older than 1.84.0.

## Sample Status API (Issue #8)

Exposed at `client.annotation.sample_status` (a `SampleStatusClient`).  A sample status assigns an
int32 status code to **one PV sample at one instant** — an ML model labeling samples as anomalous, a
rule engine flagging out-of-range values, an operator marking a handful of suspect points.  It is the
designated replacement for `DataValue.ValueStatus`, removed in this same release.

Reference: [CLAUDE.md, Sample Status API](../../CLAUDE.md#sample-status-api-annotation-service).
Worked examples: [Sample status cookbook](../cookbook/sample-status.md).

### Methods

`save_sample_statuses()`, `query_sample_statuses()`, `iter_sample_statuses()`,
`iter_sample_statuses_stream()`, and `delete_sample_statuses()`, plus the `SampleStatusColumn` /
`SampleStatusFrame` construction classes and the `sampling_clock()` / `timestamp_list()` axis builders.

A status's identity key is **(pvName, timestamp, domain, layer)**.  A `domain` names the contract
defining the semantics of the status codes; a `layer` names the producer stream, so an operator
override and a model's guess coexist without colliding.

### Two model properties the client design follows from

**Absence means "no assertion."**  There is no implicit default status, so labeling three samples says
nothing about the rest.  Absent `confidence` / `reasons` therefore surface as `None`, never as
`0.0` / `""` — `0.0` is a legitimate confidence, so a fabricated default would be indistinguishable
from a real one.  Test with `is None`, not truthiness.

**Matching is by exact timestamp at nanosecond precision.**  No nearest-sample search, no tolerance
window.  This is why `expand_data_timestamps()` computes `SamplingClock` positions as
`startTime + i * periodNanos` in **integer nanoseconds**: present-day epoch nanos need ~61 bits, so a
float64 round trip would silently produce timestamps that match nothing.  Label using timestamps
taken from query results or exact clock arithmetic.

### Reading statuses back

`sample_status_conversions` expands query results to one row per sample —
`bucket_to_rows()` / `buckets_to_rows()` / `iter_rows()` yielding `SampleStatusRow` objects.  It is
**plain Python and needs no optional extras**; a pandas view is deferred.

### Filtering query results by status

`SampleStatusFilter.include()` / `.exclude()` build a `sampleStatusSelector` for `QueryParams`, making
the server-rejected `MODE_UNSPECIFIED` zero value unreachable through the helpers.  Because absence
means "no assertion", an *unlabeled* sample never matches: `exclude()` keeps it, `include()` drops it.

`QueryParams` also validates a hand-built selector passed in directly (non-empty domain, mode not
`MODE_UNSPECIFIED`), so it fails with a message naming the problem rather than as a server rejection.
Note the selector is supported by the `querySamples()` / `querySamplesStream()` RPCs — so by all
three of `query_samples()`, `iter_query_samples()`, and `iter_query_samples_stream()` — but the
server rejects it on a bucket query.

### Other client-side rules

- Saving is a per-key upsert that **replaces in full**: re-saving with `reasons` omitted clears the
  stored reason.  Supply the complete desired state each time.
- A frame's optional parallel arrays must be omitted or supply exactly one entry per timestamp;
  validated client-side so the error names the offending PV instead of bouncing the whole batch.
- `delete_sample_statuses()` requires either `pv_names` or an explicit `all_pvs=True`.  The protocol
  treats an empty `pvNames` list as a wildcard over every PV; making that opt-in keeps a conditionally
  built list that came out empty from deleting the whole domain.
- There is deliberately **no criterion-builder class** here, unlike `PvMetadataQuery` /
  `ConfigurationQuery`: the request takes plain repeated string filters, so plain lists are the honest
  representation.
- The deferred domain-registry RPCs are not wrapped; they return "not implemented".

Verified end to end by `tests/integration/test_sample_status_client_integration.py` against a live
Annotation Service: exact nanosecond round trip through both axis forms, absent-stays-absent,
full-replace upsert, and layer independence.  Status *filtering* of query results is unit-tested only
— it needs ingested sample data to attach to (#17).

## DataSets, Annotations, and Export (Issue #6)

Three feature clients on the `annotation` facade: `client.annotation.datasets` (`DataSetClient`),
`client.annotation.annotations` (`AnnotationsClient`), and `client.annotation.export` (`ExportClient`).
A DataSet names a region of the archive; an Annotation describes one or more DataSets and may own a
Calculations payload of derived values; export writes any of it to a file on the server.

Reference: [CLAUDE.md](../../CLAUDE.md#datasets-annotations-and-export-api-annotation-service).
Worked example: [Data sets and annotations cookbook](../cookbook/datasets-and-annotations.md).
Design record: [`plan/tickets/6/plan.md`](../../plan/tickets/6/plan.md).

With this, `AnnotationClient` covers **every implemented `DpAnnotationService` feature area** —
`.pv_metadata`, `.machine_config`, `.sample_status`, `.datasets`, `.annotations`, `.export` — all
sharing one channel, each with its own stub.

### Methods

- **DataSets**: `save_dataset()`, `get_dataset()`, `query_datasets()`, `iter_datasets()`,
  `delete_dataset()`, plus `get_datasets(ids)` and the `DataSetQuery` (`DS`) helpers and `data_block()`.
- **Annotations**: `save_annotation()`, `get_annotation()`, `query_annotations()`,
  `iter_annotations()`, `delete_annotation()`, `get_calculations()`, plus the `AnnotationQuery` (`AQ`)
  helpers and the `calculations()` builder.
- **Export**: `export_data()`, the `ExportFormat` str enum, and `calculations_spec()`.

`get_datasets(ids)` is the batch fetch that avoids the annotation-listing N+1: query results carry
`dataSetIds` rather than embedded content (dp-grpc #132), so resolving a whole page of annotations
would otherwise be one call per id.  It chunks its id list (100 per request) because the ids come from
a full page of annotations and are effectively unbounded; one criterion carrying all of them becomes a
single large `$in` and an oversized request message.  A short result is logged at WARNING, since an id
withheld for any other reason is indistinguishable from a dangling one.

### Server behaviors worth knowing before you write against this

- **`save_dataset()` requires every PV named in a data block to already have *ingested data*.**  The
  server's error says `no PV metadata found for names: [...]`, but the check is a `distinct` on
  `pvName` over the **buckets** collection — so saving PV metadata does *not* satisfy it.  Nothing is
  validated client-side, because the client cannot know what is archived.  `ingestData()` also acks
  *before* the bucket becomes queryable, so a `save_dataset()` issued immediately after ingesting
  still fails; the integration test probes until it succeeds rather than sleeping a fixed interval.
- **A `DataBlock`'s range is half-open, `[begin, end)`**, matching `QueryParams`.  Back-to-back blocks
  cover the boundary sample exactly once.  **The exception is HDF5**: export writes every overlapping
  bucket whole and untrimmed, so an HDF5 export can contain samples outside the requested range and
  can write a straddling bucket twice.  Nothing client-side can change that — it is a property of the
  format.
- **The server does not check `begin < end` on a `DataBlock`**, so `data_block()`'s check is the only
  one there is.
- **Delete-not-found is a business error**, not a silent success, on both delete methods.
  `delete_dataset()` is also refused while any annotation references the dataset — delete the
  annotations first; there is deliberately no cascade.
- **`save_annotation()` replaces in full, including calculations**: omitting them clears *and deletes*
  the stored object, and a replacement returns a new `calculationsId`.  `get_annotation()` is the only
  method returning calculations inline; `query_annotations()` results carry the id with empty content.

### Empty string and `None` mean different things on two result properties

`SaveAnnotationApiResult.calculations_id` is `""` when the request carried no calculations but `None`
when the call *failed*.  `ExportDataApiResult.file_url` is `""` when the deployment simply does not
publish exports over HTTP but `None` on error.  In both cases `""` is a successful outcome — test with
`is None`, not truthiness.

### Client-side validation

The params classes validate the server's required fields rather than spending a round trip to learn
them: `SaveDataSetRequestParams` requires `name` / `owner_id` / `data_blocks`, and
`SaveAnnotationRequestParams` requires `name` / `owner_id` / `dataset_ids`, each raising `ValueError`
naming the field.  `ExportFormat` makes the server-rejected `EXPORT_FORMAT_UNSPECIFIED` unreachable,
and `ExportDataRequestParams` requires at least one of `dataset_id` / `data_blocks` /
`calculations_spec`.

The exported file lives on the **server's** filesystem and there is no retrieval RPC, so there is
deliberately no download convenience.  `patchDataSet` / `patchAnnotation` are reserved "not
implemented" placeholders and are not wrapped.

## DataFrame builders and conversions (Issue #6)

`common.DataFrame` is the shared time-series payload — Annotation Calculations now carry one
(dp-grpc #132), and it is also ingestion's `ingestionDataFrame`, so #17 extends these modules rather
than forking them.  Two new modules, both usable independently of the annotation API.

### `data_frame.py` — building

The axis helpers `sampling_clock()` / `timestamp_list()` / `timestamp_count()`, the typed scalar column
builders (`double_column`, `float_column`, `int64_column`, `int32_column`, `bool_column`,
`string_column`, `enum_column`), the legacy `data_column()` escape hatch, the provenance helpers
(`column_metadata`, `provenance`, `pv_source`, `calculations_source`), and `data_frame()` assembly.

`data_frame()` enforces the server's **shape** rules client-side — non-blank names, non-empty values,
count match, name uniqueness across all column types — while leaving its size caps server-side.  The
read path rejects all of these, so **anything `data_frame()` accepts is always readable back**.  Column
names must be non-blank, not merely non-empty; a `SamplingClock` needs a positive `periodNanos`; and a
`TimestampList` must be **strictly increasing**, duplicates included — two samples cannot claim one
instant.  An array column's sample count is `len(values) / prod(dims)`, and a value count that is not
a **whole multiple** of `prod(dims)` is rejected rather than floor-divided into a passing count.

`data_column()` maps integers and floats by `numbers.Integral` / `numbers.Real` (and NumPy's bool by
type name, without importing NumPy), so NumPy scalars map like their Python counterparts:
`np.float64` subclasses `float` but `np.int64` and `np.bool_` subclass nothing here, and matching on
exact Python type would accept some and reject others.  A `None` entry becomes an unset oneof — the
only way to express a gap on a shared axis.

### `data_frame_conversions.py` — reading back

The pure-Python half needs no extras: `data_frame_timestamps()` (integer-nanosecond axis expansion),
`column_values()`, `data_frame_columns()`, `column_metadata_dict()`.  `column_values()` yields exactly
one entry per sample for every column kind, with the structural fields a payload cannot be interpreted
without kept in companion accessors — array dims via `column_dimensions()` so `[2,2]` and `[4]` stay
distinguishable, an image's descriptor via `image_descriptor_dict()`, and a struct's `schemaId` via
`column_schema_id()`.  An axis that is set but empty is rejected rather than converted to a zero-row
table.

Behind the `[analysis]` extra: `data_frame_to_pandas()` and `data_frame_from_pandas()`, plus the
`calculations_to_dataframes()` / `calculations_from_dataframes()` bridges.  **A frame round-trips
through pandas to byte equality** apart from the deliberate `SamplingClock`→`TimestampList` axis
change: column types and provenance both survive.  Each Series carries the **narrow dtype its column
type implies** — `float32` / `int32`, not pandas' widened inference.  An `EnumColumn`'s codes are int32
and indistinguishable from a plain `Int32Column` by dtype, so its `enumId` rides in
`df.attrs["enum_ids"]`, carried even under `exclude_column_metadata=True`.

Three fail-loud rules, each guarding a silent-corruption path:

- **A `NaN` anywhere is rejected** on the pandas→proto direction.  A dense typed column cannot express
  a gap; use `data_column()` or a separate frame.
- **`NaT` is rejected**, because its integer form is a valid-looking instant.
- **Duplicate column names are rejected up front**, because a duplicated label makes `df[name]` a
  DataFrame, which would otherwise fail deep inside as pandas' "truth value of a Series is ambiguous".

Two subtleties worth stating: the pandas direction reads each instant as `Timestamp.value`, which is
**always nanoseconds**, unlike a raw int64 view, which is in the index's own storage unit — a
`datetime64[us]` index viewed as int64 lands 1000× too early.  And a pandas round trip preserves every
value, dtype, and timestamp but **not column order**: a `DataFrame` stores each column kind in its own
repeated field, so columns come back grouped by type.

## Shared time converters (Issue #6)

`to_timestamp()` and `to_epoch_nanos()` moved to a new leaf module, `time_conversions.py`, and are
re-exported from `dp_python_lib.client` — **existing imports are unaffected**.

They had been defined in `machine_config_client` as the first module to need them, and six others grew
imports from there, which read as though datasets, queries, and DataFrames depended on the machine
configuration API.  `to_epoch_nanos()` had also been written out privately three separate times.  The
new module imports only stdlib and the generated protos, so any client module can use it without an
import cycle.  New time conversions belong here.

**The datetime path uses integer arithmetic, never `datetime.timestamp()`.**  That returns a float64,
which cannot hold present-day epoch seconds at sub-microsecond resolution: it moved 99.7% of microsecond
datetimes by up to ~119ns, breaking the exact-match contract sample status and provenance depend on.
This was a real bug in the shipped 1.15.0 converter, not a hypothetical.

`sampling_clock()` / `timestamp_list()` likewise relocated from `sample_status_client` to
`data_frame.py` once calculations frames became a second caller, and are re-exported from their old
home.

## Key-only attribute search (Issue #40)

`attributes(key)` / `attr(key)` now accept an absent or empty `values` list as the protocol's key-only
**existence** search: match every record possessing the key, whatever its value.

```python
Q.attributes("S")           # every PV that has an S attribute at all
Q.attributes("S", [])       # the same thing
Q.attributes("", ["0.49"])  # ValueError: the key is still required
```

This is the one exception to the library's "helpers reject empty input" rule, because unlike
`tags([])` it *narrows* the result set rather than matching everything.  It was already available on
the #6 helpers; this back-ports it to all seven — `PvMetadataQuery`, `ConfigurationQuery`,
`ConfigurationActivationQuery`, `DataSetQuery`, `AnnotationQuery`, `PvQuery.attr`, `ConfigQuery.attr`.

The **key** check is load-bearing on the two v2 query selectors, where the server does not validate it:
a blank key would reach Mongo as an existence test on `"attributes."` and silently match nothing.

Design record: [`plan/tickets/40/plan.md`](../../plan/tickets/40/plan.md).

## Browse-all queries (Issue #41)

`criteria` is now **optional** on every paged annotation-service query and iter method, because the
server treats an empty list as match-all.  `iter_pv_metadata()` with no arguments is the browse-all
form.  The upstream provenance differs by RPC: the three metadata queries (PV metadata,
configurations, activations) got match-all from dp-service #245, while datasets and annotations got
it earlier from dp-grpc #132, implemented by dp-service #248.

The server's default page size (100, hardcoded, **not** configurable) applies **unconditionally**:
dropping the last criterion does not change the page size, so a bare `query_*()` still returns one page
and a token.  That is why `iter_*` is the right call for browsing.

The v2 query methods are deliberately **not** included: `QueryParams` still requires a PV selector or
config criteria, since a time-series query with no selection is unbounded rather than a browse-all.

Design record: [`plan/tickets/41/plan.md`](../../plan/tickets/41/plan.md).

Both #40 and #41 are covered by
`tests/integration/test_query_helper_relaxations_integration.py`, which is live-server work a unit test
cannot do: a unit test can assert the request carries `values == []`, but only a real server
distinguishes an existence filter from an `$in: []` that matches nothing.

## Internal: the `_dispatch` refactor (Issue #14)

Every unary `_send_*` method now delegates to a single `ServiceApiClientBase._dispatch()` rather than
writing the three-tier error-handling block out by hand.  The refactor converted the 18 senders that
existed at the time; the new clients in this release were written against it from the start, so there
are 28 call sites and one copy of the logic.  **No public API change and no change to any returned
error message** — the message text is contract, pinned by some 46 assertions across the unit suite.

One deliberate behavior change, in log text only: `_dispatch` names the operation with the raw
camelCase op name in all three error-log tiers.  The hand-written senders capitalized it in the
business-error warning only, while their other two error logs already used camelCase; the refactor
dropped that inconsistency.

The **server-streaming senders deliberately do not use `_dispatch`**.  They *yield* one result per
streamed message — error results included, for the public `iter_*` wrapper to convert into a
`RuntimeError` — which is a different contract from returning a single result.

`_dispatch` is itself unit-tested (`tests/unit/test_service_api_client_base.py`), which the duplicated
blocks never were.  Design record: [`plan/tickets/14/plan.md`](../../plan/tickets/14/plan.md).

## Dependency floors

| Dependency | 1.15.0 | 1.16.0 |
|---|---|---|
| `grpcio` | `>=1.82.1` | `>=1.84.0` |
| `grpcio-tools` (`[codegen]`) | `>=1.82.1` | `>=1.84.0` |
| `protobuf` | `>=7.35.0` | `>=7.35.1` |

**Raise your pin if you have one.**  The regenerated stubs embed `GRPC_GENERATED_VERSION = '1.84.0'`
and raise a `RuntimeError` *at import* on an older grpcio runtime — not at call time, and not as a
warning.  A fresh install resolves to the newest release and never sees this, which is exactly why it
is easy to miss until it reaches a pinned environment.

## Documentation and process

Two new cookbook recipes: **[Sample status](../cookbook/sample-status.md)** and
**[Data sets, annotations, export](../cookbook/datasets-and-annotations.md)**.
**[API conventions](../cookbook/conventions.md)** documents the browse-all form and the key-only
attribute exception.  The PV metadata, machine configuration, and query recipes are updated for the
relaxed helpers.

Every cookbook snippet is now checked by `.dev/tools/check-cookbook-snippets.py`, which type-checks
each example against the installed package with mypy and runs in CI.  It catches wrong attribute and
method names in the docs — the failure mode where documentation compiles in a reader's head and not in
their interpreter.

**Plan documents are now version-controlled** under `plan/tickets/<issue>/`, the convention dp-grpc and
dp-service already use, replacing the gitignored `.dev/plan/issue-<n>/`.  Plans there were invisible to
reviewers, to CI, and to anyone working from a fresh clone.  See [`plan/README.md`](../../plan/README.md).

Per-recipe "Verified against" headers are gone, matching dp-grpc #141: the convention asserts when
someone last checked a recipe, so it decays at every version bump — five of the seven recipes were
still stamped "dp-python-lib 1.15.0" while this release was being prepared.  Where a recipe uses
something added in a particular release, that is now a short note in the body ("added in 1.16.0 and
not available in earlier releases") — a durable fact about the API rather than a claim about when
someone last looked.

## Build and release infrastructure

**This release document is itself the new feature.**  Release notes are version-controlled under
`doc/release-notes/`, one per release, starting here.  `release.yml` publishes the document as the
GitHub release body: the build job concatenates it with the artifact verification and install
instructions into `dist/RELEASE_BODY.md`, which the publish job passes as `body_path` — that job never
checks out the repo, so the notes travel with the `dist/` upload.  GitHub's generated commit list is
appended after all of it.

The build job fails **early**, before building, if `doc/release-notes/<tag>.md` is missing on the tagged
commit, rather than leaving it to `action-gh-release` to fail after everything has been built, signed,
and uploaded.  So: **write the notes and merge them before pushing the `rel-*` tag.**

Also fixed here: dp-grpc's Python stub sync workflow was rewriting this repo's `pyproject.toml` on every
release sync.  Its version-bump step substituted on an unanchored `version\s*=\s*"[^"]+"`, which matched
the tail of ruff's `target-version = "py310"` and rewrote it to the release version — after which ruff
could not parse its own config and failed CI on every sync PR.  The step was removed rather than anchored
(dp-grpc #153): this repo derives its version from its own git tag via setuptools-scm, so there was never
a literal version for it to bump, and ruff's key was the only line the pattern could reach.  The step had
in fact been a no-op since before `rel-1.15.0`.
