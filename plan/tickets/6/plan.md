# Plan: Python interface to the modernized DataSets / Annotations / Export API (issue #6)

- **Ticket**: [osprey-dcs/dp-python-lib#6](https://github.com/osprey-dcs/dp-python-lib/issues/6)
- **Epic**: [osprey-dcs/dp-python-lib#10](https://github.com/osprey-dcs/dp-python-lib/issues/10)
- **Upstream API change**: [dp-grpc#132](https://github.com/osprey-dcs/dp-grpc/issues/132), merged as
  [dp-grpc PR #145](https://github.com/osprey-dcs/dp-grpc/pull/145) (2026-08-28); design rationale in
  [`dp-grpc/plan/tickets/132/plan.md`](https://github.com/osprey-dcs/dp-grpc/blob/main/plan/tickets/132/plan.md)
  (D1–D16); staged release notes in
  [`release-notes.md`](https://github.com/osprey-dcs/dp-grpc/blob/main/plan/tickets/132/release-notes.md).
  Companions in the same breaking release: [dp-grpc#143](https://github.com/osprey-dcs/dp-grpc/issues/143)
  (`ValueStatus` removal, PR #146) and [dp-grpc#245](https://github.com/osprey-dcs/dp-grpc/issues/245)
  (empty criteria = match all, PR #147).
- **Server implementation**: [dp-service#248](https://github.com/osprey-dcs/dp-service/issues/248), landed in
  four phases — PR #256 (compile / paging), #261 (entities + CRUD), #263 (keyset tokens, AND criteria), #264
  (typed calculations columns, export) — the last merged 2026-09-09.  Design record:
  [`dp-service/plan/tickets/248/plan.md`](https://github.com/osprey-dcs/dp-service/blob/main/plan/tickets/248/plan.md)
  (D1–D33).
- **Status**: written 2026-09-09 against dp-grpc `6dfff3f` and dp-service `fddf692`; open questions Q1–Q11
  resolved the same day (every recommendation accepted — see [Open questions](#open-questions-resolved-2026-09-09)).
  Phase 0 completed the same day except for #14: the stub sync (dp-grpc run
  [34382676119](https://github.com/osprey-dcs/dp-grpc/actions/runs/34382676119)) merged as #39 (`b6d0b37`, with the
  grpcio floor raised to 1.83.1), #13 closed, follow-ups #40 / #41 filed, #14 sequenced first, and the planning
  convention, the `valueStatus` rewording, and the `limit=0` test fix landed in #42.  This is the first plan under
  the `plan/tickets/<N>/` convention in this repo (previous plans lived in the gitignored `.dev/plan/`).
  **Phase 0 is complete**: #14 merged as `a0ce121`, so nothing blocks Phase 1.
- **Re-triaged 2026-09-09** before implementation, against the merged stubs and the upstream sources at the commits
  above (unchanged since the plan was written).  Every message shape in [section 2](#2-authoritative-message-shapes-from-the-regenerated-stubs)
  was re-introspected from the committed stubs and matches; the `TextCriterion`, key-only-`attributes`, export
  one-source, `EXPORT_FORMAT_UNSPECIFIED`, and calculations-validation behaviors in
  [section 3](#3-server-behaviors-the-client-must-encode-dp-service-248-verified-against-the-merged-prs) were
  re-verified in the dp-service Java source rather than taken from the ticket.  Four things the first pass missed
  were folded in: the delete-not-found tier, the absent server-side `begin < end` check, the epoch-0 `SamplingClock`
  rejection (all three now rows in section 3), and the redundant-`count` question (resolved in D6).
- **Phase 1 implemented and merged-ready 2026-09-09.**  Three clients, criterion helpers, params/results, facade
  wiring, 149 unit tests (570 total).  The wrapper-level integration test passes against a live ecosystem built from
  dp-service `fddf692` (annotation on `localhost:50053`, ingestion on `:50051`): 18 tests, 12 subtests.  Writing it
  surfaced one further server behavior the triage had not found — `saveDataSet` requires its PVs to exist in the
  archive — now recorded in section 3 and in `CLAUDE.md`.
- **Phases 2 and 3 implemented 2026-09-09.**  `data_frame.py` (axis builders relocated from `sample_status_client`
  and re-exported, typed scalar column builders, the `data_column()` escape hatch, provenance helpers, and
  `data_frame()` assembly with the server's shape rules checked client-side) and `data_frame_conversions.py` (the
  pure-Python read side plus the pandas bridges behind `[analysis]`).  87 new unit tests; 657 total with the extra
  installed, 601 passing and 56 skipping cleanly without it (verified in a venv that has no pandas).  One naming
  collision surfaced and was resolved: the `data_frame()` *function* is deliberately not re-exported from
  `dp_python_lib.client`, because binding that name would shadow the `data_frame` *module* and break the
  `from dp_python_lib.client import data_frame as dfb` form this plan's own reference snippet uses.

## Overview

Wrap the modernized DataSet / Annotation / Calculations / Export area of `DpAnnotationService` in the house
client pattern (params class → `_build_*_request()` → `_send_*()` with three-tier error handling →
`*ApiResult`), with the same layered shape the other feature clients have:

1. **Faithful low-level wrappers** for every implemented RPC in the area, plus `iter_*` transparent paging,
   criterion helpers, and result objects — no optional dependencies.
2. **Focused conveniences** for the parts that are painful by hand: building `DataBlock`s, building the
   `Calculations` payload (a `common.DataFrame` per frame, with typed columns and column-level provenance),
   reading calculations back, and — behind the existing `[analysis]` extra — round-tripping calculations
   through pandas.

The area is the last unwrapped feature of the annotation service; with it, `client.annotation` covers every
implemented `DpAnnotationService` RPC.  Consumers are the same as for the rest of the library: facility users
who want to catalogue a region of the archive, attach analysis results to it with a record of what they were
computed from, find that work again later, and export it.

RPCs in scope (13; the two `patch*` reserved placeholders are not wrapped, per the `patchPvMetadata` /
`patchConfiguration` precedent):

| RPC | Wrapper | Notes |
|---|---|---|
| `saveDataSet` | `save_dataset(params)` | id-driven upsert; empty id = create |
| `getDataSet` | `get_dataset(dataset_id)` | not-found → `ExceptionalResult` (business error) |
| `queryDataSets` | `query_datasets(...)` / `iter_datasets(...)` | 7 criteria; paged; ordered by id asc |
| `deleteDataSet` | `delete_dataset(dataset_id)` | rejected while any annotation references it |
| `saveAnnotation` | `save_annotation(params)` | the only write path for `Calculations`; returns `calculationsId` |
| `getAnnotation` | `get_annotation(annotation_id)` | the only method returning calculations inline |
| `queryAnnotations` | `query_annotations(...)` / `iter_annotations(...)` | 8 criteria; results carry ids, not content |
| `deleteAnnotation` | `delete_annotation(annotation_id)` | cascades to its calculations; soft refs may dangle |
| `getCalculations` | `get_calculations(calculations_id)` | click-through path; keyed by `calculationsId` |
| `exportData` | `export_data(params)` | dataset id and/or inline blocks and/or calculations spec → HDF5/CSV/XLSX |
| `patchDataSet` / `patchAnnotation` | — | NOT-YET-IMPLEMENTED placeholders; not wrapped |

## Background / triage findings

Everything below was verified on 2026-09-09; nothing here is taken from the ticket as filed, which is a
placeholder.

### 1. The committed stubs predate the modernization — a stub sync is the hard prerequisite

`src/dp_python_lib/grpc/annotation_pb2_grpc.py` has 30 RPCs and none of the new ones.  The last sync
(`grpc-sync-32591624369`, PR #36, generated 2026-08-22 from dp-grpc `main`) predates dp-grpc PRs #145, #146,
and #147.  Regenerating from current dp-grpc `main` (`6dfff3f`) with the same toolchain the sync workflow uses
(grpcio-tools 1.83.0 / protobuf 7.35.1 — identical to what generated the committed stubs, so the diff is
proto-only) gives:

- **7 RPCs added** — `getDataSet`, `deleteDataSet`, `patchDataSet`, `getAnnotation`, `deleteAnnotation`,
  `patchAnnotation`, `getCalculations`; none removed; 37 total.
- **Only three files change in substance**: `annotation_pb2.py`, `annotation_pb2_grpc.py`, `common_pb2.py`.
  The actual sync run (#39, 2026-09-09) touched seven, because the runner's grpcio-tools was 1.83.1 and
  re-stamped `GRPC_GENERATED_VERSION` in every `*_pb2_grpc.py` — a two-line banner change per file that also
  raises the stubs' import-time grpcio requirement.  The `pyproject.toml` floor moved to 1.83.1 on the sync
  branch (the drift `cbe9631` fixed for the #8 sync; inspect every `grpc-sync-*` PR for it).
- **All 406 existing unit tests pass unchanged** against the regenerated stubs (run from a scratch copy of the
  repo with the stub directory swapped).  No shipped client touches datasets, annotations, or export, and the
  `ValueStatus` removal reaches this repo only as prose: `query_conversions.py:14` (docstring), `CLAUDE.md`,
  `doc/cookbook/query.md:391`, `doc/cookbook/sample-status.md:78` all describe `valueStatus` as "deprecated /
  ignored" and should now say it was removed in 1.16.0.  So the sync PR is mechanically safe to merge on its
  own, ahead of any client work.

The sync is a dp-grpc workflow (`generate-python-stubs.yml`, `workflow_dispatch` on `main` with
`dry_run=false`; the run opens a `grpc-sync-<run-id>` PR here).  Its release-only version-bump step is buggy
(dp-grpc#136) but does not fire on a manual dispatch.  See Q8 for whether to dispatch it or commit the locally
generated stubs.

### 2. Authoritative message shapes (from the regenerated stubs)

Field names below are the Python attribute names.  All responses follow `oneof result { exceptionalResult |
<typed result> }` → `HasField()`; the success field names are listed because, as with `pvMetadataResult`, they
are not all name-matched to the RPC.

```
DataSet      : id, name, ownerId, description, dataBlocks[], tags[], attributes[] (common.Attribute),
               createdTime, updatedTime (server-set), modifiedBy
DataBlock    : beginTime, endTime (two Timestamps -- NOT a TimeRange), pvNames[]
Annotation   : id, ownerId, dataSetIds[], name, annotationIds[], description, tags[], attributes[],
               calculationsId, calculations (populated by getAnnotation ONLY), createdTime, updatedTime, modifiedBy
Calculations : id (ignored on save), calculationDataFrames[] of Calculations.CalculationsDataFrame { name, frame: common.DataFrame }
common.DataFrame : dataTimestamps (SamplingClock | TimestampList), dataColumns[] (legacy DataColumn escape hatch),
               serializedDataColumns[], and 14 typed repeated fields: doubleColumns, floatColumns, int64Columns,
               int32Columns, boolColumns, stringColumns, enumColumns, imageColumns, structColumns,
               doubleArrayColumns, floatArrayColumns, int32ArrayColumns, int64ArrayColumns, boolArrayColumns
typed scalar column : name, values[], metadata (ColumnMetadata); EnumColumn adds enumId
array column : name, dimensions (ArrayDimensions{dims[]}), values[] (flat, samples x prod(dims)), metadata
ColumnMetadata : provenance (ColumnProvenance), tags[], attributes[]
ColumnProvenance : source, process, derivedFrom[] of ColumnProvenance.ColumnSource { oneof origin: pvName |
               calculationsColumn: ColumnProvenance.CalculationsColumn {calculationsId, frameName, columnName};
               timeRange (optional) }
CalculationsSpec : calculationsId, dataFrameColumns: map<frameName, CalculationsSpec.ColumnNameList{columnNames[]}>

SaveDataSetRequest    : id, name, ownerId, description, dataBlocks[], tags[], attributes[], modifiedBy   (flat)
SaveDataSetResponse   : saveDataSetResult { dataSetId }
GetDataSetRequest     : dataSetId          -> getDataSetResult { dataSet }
DeleteDataSetRequest  : dataSetId          -> deleteDataSetResult { dataSetId }
QueryDataSetsRequest  : criteria[], limit (uint32), pageToken  -> dataSetsResult { dataSets[], nextPageToken }
  QueryDataSetsCriterion oneof: idCriterion{ids[]} | ownerCriterion{ownerIds[]} | nameCriterion{exact[],prefix[],contains[]}
     | textCriterion{text} | pvNameCriterion{names[]} | tagsCriterion{values[]} | attributesCriterion{key, values[]}
SaveAnnotationRequest : id, ownerId, dataSetIds[], name, annotationIds[], description, tags[], attributes[],
                        modifiedBy, calculations
SaveAnnotationResponse: saveAnnotationResult { annotationId, calculationsId }
GetAnnotationRequest  : annotationId       -> getAnnotationResult { annotation }
DeleteAnnotationRequest: annotationId      -> deleteAnnotationResult { annotationId }
QueryAnnotationsRequest: criteria[], limit, pageToken -> annotationsResult { annotations[], nextPageToken }
  QueryAnnotationsCriterion oneof: idCriterion | ownerCriterion | dataSetsCriterion{dataSetIds[]}
     | annotationsCriterion{annotationIds[]} | nameCriterion | textCriterion | tagsCriterion | attributesCriterion
GetCalculationsRequest: calculationsId     -> getCalculationsResult { calculations }
ExportDataRequest     : dataSetId, calculationsSpec, outputFormat (enum: UNSPECIFIED=0, HDF5=1, CSV=2, XLSX=3), dataBlocks[]
ExportDataResponse    : exportDataResult { filePath, fileUrl }
DataValue             : valueStatus is GONE (field 15 reserved)
```

Two naming traps carried over from the Java cookbook: the `Calculations` list field is `calculationDataFrames`
(singular "calculation") while the message is `CalculationsDataFrame` (plural); and `Attribute` keys the pair
with `name`, whereas the query-side `AttributesCriterion` uses `key`.  A third, from the generated Python:
`CalculationsDataFrame`, `ColumnSource`, `CalculationsColumn`, and `ColumnNameList` are *nested* messages
(`annotation_pb2.Calculations.CalculationsDataFrame`, `common_pb2.ColumnProvenance.ColumnSource`,
`common_pb2.ColumnProvenance.CalculationsColumn`, `common_pb2.CalculationsSpec.ColumnNameList`), so the module-level
attributes a reader of this table might reach for do not exist.

### 3. Server behaviors the client must encode (dp-service #248, verified against the merged PRs)

| Behavior | Consequence for the client |
|---|---|
| Empty criteria list matches all records, on both queries (#245 semantics; also now true of the three metadata queries) | `criteria` can be optional — "browse all" is legal (Q6) |
| Unset/zero `limit` = server default page size (`DEFAULT_QUERY_LIMIT`, 100), never unbounded | `iter_*` is the normal way to read; `limit` is page size, not a cap (already the house contract) |
| Page tokens are **opaque keyset tokens** carrying a query discriminator; a malformed, whitespace, legacy skip-offset, or wrong-query token is **rejected** (`REJECT`) | `iter_*` surfaces that as `RuntimeError`; nothing to parse.  Note the three metadata queries still use skip tokens with silent restart (dp-service #193) — `conventions.md` must describe both behaviors |
| Criteria AND across the list, OR within a criterion; **at most one `TextCriterion` per request** (a second is a validation `REJECT`, since two `$text` clauses cannot be ANDed) | client-side `ValueError` naming the rule is cheap and matches the "fail with a message naming the problem" posture (Q7) |
| Blank criterion values are rejected server-side; `IdCriterion` ids and get/delete ids must be valid ObjectIds (malformed → `REJECT`, not "not found") | helpers reject empty inputs as usual; no ObjectId validation client-side (format is a server implementation detail) |
| **`saveDataSet` requires every PV named in a data block to already exist IN THE ARCHIVE.**  Despite the error text (`no PV metadata found for names: [...]`), the check is a `distinct` on `pvName` over the *buckets* collection (`MongoAnnotationHandler.validateSaveDataSetRequest` → `MongoSyncQueryClient.executeQueryPvExistence`) — saved PV metadata does **not** satisfy it.  Found 2026-09-09 while writing the Phase 1 integration test; neither the proto nor the ticket mentions it | nothing to validate client-side (the client cannot know what is archived), but it shapes the tests and the cookbook: a dataset can only name PVs with ingested data, so the integration test ingests its own samples first, and the cookbook's worked example must use an archived PV.  `ingestData()` acks *before* the bucket is queryable, so a save issued immediately after ingesting still fails — the test probes until it succeeds rather than sleeping a fixed interval |
| `getDataSet` / `getAnnotation` / `getCalculations` not-found → `ExceptionalResult` | same "business error" tier as `get_pv_metadata` |
| `deleteDataSet` / `deleteAnnotation` **not-found is also a `REJECT`**, not a silent success (`DeleteAnnotationDispatcher:34-36`, "no Annotation record found for id: …") | same business-error tier, so no code change — but the cookbook teardown and the integration test's delete-twice leg must expect an error result on the second delete, not a success |
| `DataBlock` validation is **only** `beginTime.epochSeconds >= 1`, `endTime.epochSeconds >= 1`, and a non-empty `pvNames` (`AnnotationValidationUtility.validateDataBlock`) — the server never checks `begin < end` | D4's client-side `begin < end` check is the *only* one there is, not a duplicate of a server check.  A reversed block is accepted today, which is also why the proto's silence on half-openness is a real ambiguity rather than a documentation gap |
| A `SamplingClock` axis must have `startTime.epochSeconds != 0` as well as non-zero `periodNanos` and `count` (`validateCalculationsDataFrame`) | an epoch-0 start time is **rejected**: fixtures must not build a calculations axis from `datetime(1970, 1, 1)`.  `sampling_clock()` does not check this (it has no reason to — the sample-status axis has no such rule), so it is a fixture discipline, not a builder change |
| `deleteDataSet` rejected while referenced; message names one referencing annotation id plus the total count | surface verbatim; no client-side cascade (decision D8) |
| `deleteAnnotation` deletes the annotation first, then its calculations; incoming `annotationIds` / `derivedFrom` links dangle | readers must tolerate dangling ids — document, do not resolve |
| `saveAnnotation` full-replace **includes calculations**: omitting them clears (and deletes) the stored object; a replaced object is deleted; the result returns the new `calculationsId` | params carry `calculations` explicitly; the cookbook's update recipe reads with `get_annotation()` and resends |
| `getAnnotation` with a `calculationsId` that resolves to nothing is an **ERROR** (corruption), never empty content | nothing to do; the empty-`calculations`-with-id case is only reachable from `queryAnnotations` |
| Tags are normalized lowercase/dedupe/sort on save (both entities); reference ids canonicalized to lowercase hex | a tag saved as `Shift-2` reads back as `shift-2` — integration test asserts it |
| **Calculations validation on save**: every column of every type has a non-blank name and non-empty values; value count = frame timestamp count (arrays: × dims product); column names unique across types within a frame; frame names unique; string values ≤ 256 chars, array dims product ≤ 10M, image ≤ 50 MB, struct ≤ 1 MB; `SerializedDataColumn` gets name checks only | the builder enforces the shape rules client-side (count match, uniqueness, non-empty) so the error names the frame/column; the size caps are left to the server |
| `exportData`: at least one of `dataSetId` / `dataBlocks` / `calculationsSpec`; sources merge; `EXPORT_FORMAT_UNSPECIFIED` rejected; client mistakes (unknown ids, bad filter names, non-scalar column in CSV/XLSX, malformed `dataSetId`) are `REJECT`; `filePath` is a **server-side** path, `fileUrl` only when the deployment publishes over HTTP | validate the one-source rule client-side; no download convenience (there is no RPC for it) |
| `updatedTime` unset on create | accessor returns the raw `Timestamp`; document |

### 4. What already exists in this repo to reuse

- `to_timestamp()` / `TimestampInput` (`machine_config_client.py`) — every `DataBlock` and `TimeRange` bound.
- `sampling_clock()` / `timestamp_list()` / `_timestamp_count()` (`sample_status_client.py`) — the
  `DataTimestamps` axis builders a calculations frame needs.  They belong in a shared module now that a second
  caller exists; re-export from `sample_status_client` so nothing breaks.
- `expand_data_timestamps()` (`sample_status_conversions.py`) — integer-nanosecond SamplingClock expansion for
  reading a frame's axis back.  The exactness argument applies unchanged.
- `data_value_to_python()` and the `Image` wrapper (`query_conversions.py`) — the legacy `DataColumn` arm of the
  read side; the `_require_pandas()` / `_require_numpy()` lazy-import pattern.
- The `iter_*` paging idiom (`RuntimeError` on a page error) and the `*ApiResult` accessor convention (`None` /
  `[]` on error; string fields surface as the proto string, so `next_page_token` is `""` on the last page).

### 5. Adjacent findings that outlive this ticket

- **#13 is already fixed.**  Commit `6827320` changed `_build_query_pv_metadata_request` to
  `if limit is not None`; the issue was filed later from the #9 review and is still open.  Close it with a
  pointer — done 2026-09-09, with the missing `limit=0` regression test added alongside this plan.
- **Key-only attribute search is unreachable through every criterion helper.**  All four `AttributesCriterion`
  messages (PV metadata, configurations, activations, and the query-service metadata query) document "empty
  `values` = key-only / existence search", but `PvMetadataQuery.attributes()`, `ConfigurationQuery.attributes()`,
  `ConfigurationActivationQuery.attributes()`, and `PvQuery.attr()` / `ConfigQuery.attr()` all raise on an
  empty list.  The new `DataSetQuery` / `AnnotationQuery` helpers accept `values=None`; back-porting the
  relaxation to the five existing helpers is [#40](https://github.com/osprey-dcs/dp-python-lib/issues/40), not
  this ticket.
- **#14 (`_dispatch` refactor)** was parked "until #6 lands so the full duplication is visible".  It is visible
  now: 18 unary `_send_*` bodies exist — 16 on the annotation-service clients, plus one each in `QueryClient` and
  `IngestionClient` in the identical three-tier shape — and this ticket adds 10 more (one per unary RPC in the
  table; `iter_*` reuses the query sender).  The two server-streaming senders stay as they are.  `_dispatch` lives
  on `ServiceApiClientBase`, so it covers all 18 rather than only the annotation clients.  It lands first (Q3;
  recorded on #14).

## Design decisions (proposed)

**D1 — Three feature clients on the existing facade, not one.**  `client.annotation.datasets`
(`DataSetClient`), `client.annotation.annotations` (`AnnotationsClient`), and `client.annotation.export`
(`ExportClient`), each a `ServiceApiClientBase` over the shared annotation channel, wired in `AnnotationClient`
exactly as `pv_metadata` / `machine_config` / `sample_status` are.  The `MachineConfigClient` precedent bundles
two entities under one umbrella name, but "machine_config" is a natural umbrella and "datasets and annotations
and export" has none — a single client would have `client.annotation.annotations.save_dataset()` or
`client.annotation.datasets.export_data()`, both of which read wrong.  Three small cohesive clients mirror the
dp-grpc README's own sectioning (Data Set API / Annotation API / Calculations Get / Export) and cost nothing at
runtime (three stubs on one channel).  `get_calculations()` lives with annotations because calculations are
owned by them.  The cookbook stays one recipe.  (Q1, resolved as proposed.)

**D2 — `dataset` is one word in Python identifiers; `DataSet` stays in class names.**  Class names keep
the proto's `DataSet` spelling (`DataSetClient`, `DataSetQuery`, `SaveDataSetRequestParams`,
`QueryDataSetsApiResult`) because they name proto concepts, while snake_case identifiers use `dataset`
(`save_dataset`, `dataset_id`, `dataset_ids`, `.datasets`) because that is the English word and the Python
ecosystem's spelling.  (Q2, resolved as proposed.)

**D3 — Criterion helpers follow the `PvMetadataQuery` shape, with two deliberate differences.**
`DataSetQuery` (suggested alias `DS`): `ids(values)`, `owners(values)`, `name(exact=, prefix=, contains=)`,
`text(text)`, `pv_names(values)`, `tags(values)`, `attributes(key, values=None)`.  `AnnotationQuery` (suggested
alias `AQ`): `ids`, `owners`, `datasets(values)` (→ `dataSetsCriterion`), `annotations(values)`
(→ `annotationsCriterion`), `name`, `text`, `tags`, `attributes(key, values=None)`.  Differences from #5/#9:
(a) `attributes()` accepts an empty/absent `values` because the proto documents key-only search (finding 5);
(b) `criteria` is optional on the query methods because an empty list is a legal match-all (finding 3).  Each
helper still raises `ValueError` on an empty required input.  (Q6, resolved as proposed.)

**D4 — Params classes mirror the flat save requests; `DataBlock` gets a builder function.**
`SaveDataSetRequestParams(name, owner_id, data_blocks, description=None, tags=None, attributes=None,
modified_by=None, dataset_id=None)` and `SaveAnnotationRequestParams(name, owner_id, dataset_ids,
annotation_ids=None, description=None, tags=None, attributes=None, modified_by=None, calculations=None,
annotation_id=None)`.  `attributes` is a `dict[str, str]` as in `SavePvMetadataRequestParams`.  The id
parameter is last and optional because presence means "replace in full" — the cookbook's read-modify-write
recipe is the only sane way to set it.  `data_block(begin_time, end_time, pv_names) ->
annotation_pb2.DataBlock` accepts `TimestampInput`, requires `begin < end` and a non-empty `pv_names`, and
documents that the proto does not state whether the interval is half-open (the dp-grpc cookbook flags this
too).  `calculations` accepts an `annotation_pb2.Calculations` — built by hand, or by the D6 builders.

**D5 — Result accessors follow the house rules, with the proto string as the value.**
`SaveDataSetApiResult.dataset_id`; `GetDataSetApiResult.dataset`; `QueryDataSetsApiResult.datasets` +
`next_page_token`; `DeleteDataSetApiResult.dataset_id`; `SaveAnnotationApiResult.annotation_id` +
`calculations_id` (`""` when the request carried no calculations, `None` only on error — the same rule as
`next_page_token`); `GetAnnotationApiResult.annotation` + `calculations` (the inline object, `None` when the
annotation has none); `QueryAnnotationsApiResult.annotations` + `next_page_token`;
`DeleteAnnotationApiResult.annotation_id`; `GetCalculationsApiResult.calculations`;
`ExportDataApiResult.file_path` + `file_url` (`""` is normal, not an error).

**D6 — A shared `common.DataFrame` builder module, sized for what calculations need and shaped for #17.**
New `client/data_frame.py` (no optional dependencies):

- axis: `sampling_clock()` / `timestamp_list()` move here (re-exported from `sample_status_client`), with their
  signatures unchanged — see the count note below;
- typed scalar columns: `double_column(name, values, metadata=None)`, `float_column`, `int64_column`,
  `int32_column`, `bool_column`, `string_column`, `enum_column(name, values, enum_id, metadata=None)`;
- `data_column(name, values, metadata=None)` — the legacy `DataColumn` escape hatch, where a `None` entry is a
  missing value (unset oneof), the only way to express a gap on a shared axis;
- `data_frame(data_timestamps, columns) -> common_pb2.DataFrame` — routes each column into its repeated
  field; validates at least one column, count-match against the axis, non-empty names/values, and column-name
  uniqueness across types (the server's D28 rules, checked client-side so the error names the column);
- provenance: `column_metadata(tags=None, attributes=None, provenance=None)`, `provenance(source=None,
  process=None, derived_from=None)`, `pv_source(pv_name, time_range=None)`, `calculations_source(calculations_id,
  frame_name, column_name, time_range=None)`; a `time_range` is a `(begin, end)` pair of `TimestampInput`.

Deliberately **not** built here: array, image, struct, and serialized column builders.  They are reachable by
constructing the proto directly and passing it through (the builder accepts pre-built column messages
alongside its own), and their ergonomics (dims, image descriptors, schema ids) are #17's problem to design
once, with ingestion data in hand.  `ingestion.proto`'s `ingestionDataFrame` *is* `common.DataFrame`, so this
module is the substrate #17 extends rather than a parallel one.  `calculations(frames: dict[str,
common_pb2.DataFrame]) -> annotation_pb2.Calculations` lives in `annotations_client.py`; taking a dict makes
frame-name uniqueness true by construction.  (Q4, resolved as proposed.)

*On the redundant `count` (triage, 2026-09-09).*  `sampling_clock(start, period, count)` makes every calculations
call site restate a number it already has — `sampling_clock(t0, period, count=len(values))` — so a mismatch between
axis and column becomes a `data_frame()` `ValueError` rather than being unrepresentable.  A `data_frame()` that took
a `(start_time, period_nanos)` pair and derived the count from the columns would remove that.  **Rejected**, for two
reasons.  It would fork the axis API by caller: `sample_status_client` genuinely knows its count independently (it
labels a subset of an archived clock the client did not produce), so the count-bearing form has to stay, and a second
derived form beside it means two ways to say the same thing.  And `SampleStatusFrame` already set the house precedent
for exactly this shape — an explicit `data_timestamps` plus per-column validation against it — so `data_frame()`
matching it is what a reader of one will expect of the other.  The redundancy is real but cheap, and it is caught
client-side with a message naming the column.  Decided now because the signature is breaking to change later.

**D7 — Read side: typed columns → Python, then → pandas, sharing one converter with #16.**
New `client/data_frame_conversions.py`: `data_frame_timestamps(frame) -> list[int]` (epoch nanos, via
`expand_data_timestamps`), `data_frame_columns(frame) -> dict[str, list]` (all 14 typed kinds plus `DataColumn`
via `data_value_to_python`; array columns yield one flat list per sample and the dims alongside), and — behind
`[analysis]` — `data_frame_to_pandas(frame)` (UTC `DatetimeIndex` built from int64 nanoseconds, so
SamplingClock axes stay exact; `ColumnMetadata` in `df.attrs["column_metadata"]` as `query_conversions` does),
`data_frame_from_pandas(df, ...)` (dtype → typed column: float64→Double, float32→Float, int64→Int64,
int32→Int32, bool→Bool, object/string→String; a `NaN`/`None` anywhere is a fail-loud `ValueError` pointing at
the sparsity rule — own frame, or `data_column()` — because dense typed columns cannot express a gap), and the
calculations wrappers `calculations_to_dataframes(calculations) -> dict[str, DataFrame]` /
`calculations_from_dataframes({name: df})`.  `DataBucket`'s typed-column oneof in the bucket query (#16) uses
the same 14 column messages, so `data_frame_columns()`'s per-column converter is written as a standalone
function #16 can call.  (Q5, resolved as proposed.)

**D8 — No cascade on `delete_dataset()`.**  A `cascade=True` that first deletes every referencing annotation
is two destructive RPC families behind one flag; the honest shape is the two-step the server designed:
`iter_annotations([AQ.datasets([dataset_id])])`, delete those, then delete the dataset.  The cookbook shows it.
(The sample-status wildcard delete needed an explicit `all_pvs=True` opt-in; this is the same instinct.)

**D9 — One batch-fetch convenience, because the proto insists on it.**  `DataSetClient.get_datasets(ids) -> dict[str,
DataSet]` issues one `query_datasets([DS.ids(ids)])` and pages through it.  It exists so the "annotation listing needs
its datasets" case in the cookbook is one call and never becomes the N+1 that D5 of the dp-grpc plan removed
server-side.  Ids are deduplicated (order preserved); an empty `ids` returns `{}` without an RPC, because the cookbook
feeds it from annotation `dataSetIds` lists that may be empty and `DS.ids([])` would otherwise raise; ids that resolve
to nothing are simply absent from the dict (a dangling `dataSetIds` entry is not an error).

**D10 — Export ergonomics: an `ExportFormat` enum and a `calculations_spec()` builder; nothing that pretends to
download.**  `ExportFormat` is a `str` enum (`HDF5`/`CSV`/`XLSX`, values `"hdf5"`/`"csv"`/`"xlsx"`) so
`ExportFormat("csv")` and `ExportFormat.CSV` both work; `ExportDataRequestParams` coerces a bare string through
`ExportFormat(...)` at construction, so a misspelling raises `ValueError` naming the valid values, and
`EXPORT_FORMAT_UNSPECIFIED` is unreachable through the params class.  `ExportDataRequestParams(output_format,
dataset_id=None, data_blocks=None, calculations_spec=None)` raises unless at least one source is
set.  `calculations_spec(calculations_id, frame_columns: dict[str, list[str]] | None = None) ->
common_pb2.CalculationsSpec`; omitting `frame_columns` means all frames and columns.  The result exposes `file_path`
and `file_url` as-is: the file lives on the server's filesystem and there is no retrieval RPC.

**D11 — Client-side validation mirrors the server's shape rules, not its resource caps.**  Empty ids, empty required
lists, a second `TextCriterion`, a zero-source export, an empty frame (no columns), count-mismatched or
duplicate-named columns, and duplicate frame names all raise `ValueError` before any RPC.  String-length / array-size /
image-size caps stay server-side: they are deployment policy, and duplicating numbers that can change is how clients
drift.  (Q7, resolved as proposed.)

**D12 — Integration tests probe for the modernized API before running.**  A pre-1.16.0 server accepts the
connection and answers `getDataSet` with `UNIMPLEMENTED`, so reachability is not a sufficient skip check (the
#8 lesson).  The export test uses a calculations-only CSV export, which needs no ingested data, and asserts on
`file_path` only when the server is on the same host.  (Q11, resolved as proposed.)

## Implementation tasks

### Phase 0 — prerequisites (own PRs, before the feature branch)

1. **Stub sync.**  Dispatch `generate-python-stubs.yml` on dp-grpc `main` with `dry_run=false` (done 2026-09-09,
   run 34382676119 → #39), verify the `grpc-sync-*` PR against a local generation and bump the grpcio floor to
   match the re-stamped banners (finding 1), merge (done 2026-09-09, `b6d0b37`).  The four `valueStatus` mentions
   are reworded ("removed in dp-grpc 1.16.0, field 15 reserved") in #42, since #39 was the bot's PR.
2. **#14 `_dispatch` extraction**, its own PR (Q3), covering all 18 unary senders (finding 5).
3. **Planning convention** (Q10): this file, `plan/README.md`, and a "Ticket Planning Workflow" section in
   `CLAUDE.md` mirroring dp-service's, in a docs-only PR (#42) that also carries the #13 regression test.  Review
   of #42 found that asserting `request.limit == 0` cannot detect the #13 bug at all — a proto3 scalar reads 0
   whether or not it was assigned — and that three older `limit=0` tests had the same vacuous shape.  #42 therefore
   adds `tests/unit/assignment_spy.py` (`watch_assignments()`), and all five `limit=0` tests now assert the
   assignment itself; Phase 1's `limit` tests use the same helper.

### Phase 1 — low-level wrappers (no optional dependencies)

- `src/dp_python_lib/client/dataset_client.py` — `DataSetQuery`, `data_block()`, `SaveDataSetRequestParams`,
  the four `*ApiResult` classes, `DataSetClient` with `save_dataset` / `get_dataset` / `query_datasets` /
  `iter_datasets` / `delete_dataset` / `get_datasets` (D9).
- `src/dp_python_lib/client/annotations_client.py` — `AnnotationQuery`, `calculations()`,
  `SaveAnnotationRequestParams`, five `*ApiResult` classes, `AnnotationsClient` with `save_annotation` /
  `get_annotation` / `query_annotations` / `iter_annotations` / `delete_annotation` / `get_calculations`.
- `src/dp_python_lib/client/export_client.py` — `ExportFormat`, `calculations_spec()`,
  `ExportDataRequestParams`, `ExportDataApiResult`, `ExportClient.export_data`.
- `annotation_client.py` — three new attributes and docstring; `client/__init__.py` — exports, sorted as now.
- Each `_send_*` in the current house style (or via `_dispatch`, per Q3); request builders set `limit` with
  `if limit is not None` (the #13 rule) and `pageToken` only when non-empty.
- Unit tests, one file per client, mirroring `test_pv_metadata_client.py`: request building for every param
  (including "omitted optional fields are absent, not empty"), every criterion helper incl. `attributes(key)`
  key-only and the empty-input `ValueError`s, the second-`TextCriterion` rejection, the zero-source export
  rejection, `ExportFormat` mapping, three-tier `_send_*` coverage per RPC (success / `exceptionalResult` /
  `RpcError` / unexpected), page-token threading and the `iter_*` `RuntimeError`, `get_datasets` dedup, empty-input, and
  absent-id behavior.
- `tests/integration/test_datasets_annotations_integration.py`, wrapper level (Q9): the API probe (D12); save
  dataset → get → query by id / owner / pv name / tag (asserting the lowercase-normalized tag) → save annotation
  with a hand-built `Calculations` → `get_annotation` (calculations inline, `dataSetIds` ids-only) →
  `get_calculations` → `query_annotations([AQ.datasets([...])])` (calculations empty, id present) →
  `delete_dataset` rejected while referenced → `delete_annotation` → `delete_dataset` → deleting either a second
  time is a business *error*, not a success (finding above) → paging across a
  run-unique tag with `limit=1` → malformed page token is a business error.  Everything written is deleted, keyed
  by a run-unique owner id.  The builder, pandas, and export legs are added in Phase 4.

### Phase 2 — calculations construction and reading (no optional dependencies)

- `src/dp_python_lib/client/data_frame.py` per D6, with `sampling_clock` / `timestamp_list` relocated and re-exported.
  - `src/dp_python_lib/client/data_frame_conversions.py` — the pure-Python half of D7. - Unit tests: each typed column
  round-trips through `data_frame()` and back through `data_frame_columns()`; an empty column list, count-mismatch,
  duplicate-name, and empty-name errors name the frame or column; `data_column()` `None` → unset oneof → `None` on
  read; SamplingClock axis reads back in exact integer nanoseconds (the sample-status test's no-float discipline);
  provenance helpers set the right `origin` arm and optional `timeRange`; `calculations()` rejects an empty dict and
  an empty frame name.

### Phase 3 — pandas bridges (`[analysis]` extra, lazily imported)

- The pandas half of D7 in `data_frame_conversions.py`.
- Unit tests skip cleanly without the extra (the `test_query_conversions.py` pattern): dtype mapping both ways,
  UTC index exactness, `NaN` fail-loud with the sparsity guidance, metadata in `attrs`, multi-frame
  `calculations_*` round trip.

### Phase 4 — integration test and documentation

- Extend `tests/integration/test_datasets_annotations_integration.py` (the wrapper-level round trip lands in
  Phase 1): save annotation with a builder-made typed `DoubleColumn` carrying `derivedFrom` → `get_annotation` →
  `get_calculations` read back through `data_frame_conversions` (exact integer-nanosecond axis) → full-replace
  re-save without calculations clears them → calculations-only CSV export (file exists on the same host;
  array-column CSV export is rejected).
- `doc/cookbook/datasets-and-annotations.md`, the Python counterpart of dp-grpc's recipe, in the continuous
  worked example (a region of the `BPMS:GUNB:314` shift, an RMS calculation over it, provenance, export);
  `doc/cookbook/README.md` table; `doc/cookbook/conventions.md` — empty criteria = match all, and the two
  page-token behaviors (rejected for datasets/annotations, silently restarted for the metadata queries until
  dp-service #193).
- `README.md` — move the three Annotation Service bullets from TODO to Current state; `CLAUDE.md` — Key Files
  entries, a "DataSets, Annotations, and Export API" usage section drafted **during Phase 1 design** (the #7
  practice: the snippet is the API review), and the `valueStatus` wording.
- Run `ruff check`, `ruff format --check`, `.dev/tools/check-cookbook-snippets.py`, and the unit suite; record
  the server commit the integration test was verified against.

## Out of scope

- `patchDataSet` / `patchAnnotation` — reserved placeholders returning "not implemented"; wrap when the field
  mask lands (same stance as the other `patch*` and the sample-status domain registry).
- Array / image / struct / serialized column builders — #17 (ingestion), where they are first needed with real
  data; pass-through of hand-built protos works today.
- A download path for exports — no RPC exists; `file_url` is what the deployment offers.
- Cascade delete — D8.
- Key-only `attributes()` for the five existing helpers — #40; optional `criteria` on the six existing query/iter
  methods — #41 (finding 5, Q6).  #13 is closed.
- Bucket-query typed-column conversions — #16, which reuses D7's converter.
- Resolving dangling `annotationIds` / `derivedFrom` links — the proto says readers tolerate them; nothing to
  build.

## Dependencies and sequencing

- **Blocks on the stub sync (Phase 0.1).**  Nothing in Phases 1–4 imports without it.  It is safe to merge
  alone (finding 1).
- **No server dependency remains**: dp-service #248 landed in full on 2026-09-09.  But **no release carries it
  yet** (dp-grpc and dp-service `main` say 1.16.0; the latest tag everywhere is `rel-1.15.0`), so the
  integration test (from PR 1 onward, Q9) needs a server built from dp-service `main` at or after `4cde755`
  (PR #264) — Q11.
- Phases 1 → 2 → 3 are sequential in code (3 imports 2, 2's `calculations()` is what 1's params accept), but
  the Phase 1 wrappers are usable and testable on their own with hand-built `Calculations` protos.
- #17 (ingestion) should start after Phase 2 lands, extending `data_frame.py` rather than forking it.  #16
  (buckets) should reuse `data_frame_conversions.py`'s per-column converter.
- Release train: the stub sync must be re-run at release time regardless (see the release-ordering note in
  project memory / `release.yml`), so the Phase 0 sync does not substitute for it.

## Open questions (resolved 2026-09-09)

Each question is kept with the context that made it a question; the resolution is the recommendation, accepted
in full by the ticket owner on 2026-09-09.

- **Q1 — Client layout.  RESOLVED: three feature clients.**  Three (`datasets` / `annotations` / `export`, D1)
  or one (`client.annotation.annotations`, as the facade docstring and `CLAUDE.md` pre-announced)?  The
  one-client form matches the `MachineConfigClient` precedent; the three-client form reads correctly at every
  call site and mirrors the dp-grpc README.  The feature class `AnnotationsClient` is one letter from the facade
  `AnnotationClient`; accepted, and both docstrings say so (users never construct the facade directly).

- **Q2 — `dataset` vs `data_set`.  RESOLVED: `dataset`.**  Identifiers use `dataset` (`save_dataset`,
  `dataset_id`); class names keep the proto's `DataSet` (`DataSetClient`, `DataSetQuery`).  Strict snake_case of
  the proto (`save_data_set`) was the alternative; cheap to decide now, breaking to change later.

- **Q3 — #14 before #6.  RESOLVED: yes, as its own small PR.**  The `_dispatch(stub_call, request, result_cls,
  success_field, op_name)` extraction is mechanical and covered by the existing 406 tests (they mock the stub
  method, which `_dispatch` still calls); doing it first makes each of the 10 new `_send_*` methods ~8 lines
  instead of ~40.  The "wait until #6 lands" rationale is satisfied by this plan's RPC table.  Recorded on #14.

- **Q4 — How much `Calculations` construction belongs in #6.  RESOLVED: D6's builder.**  Scalar typed
  columns, the `DataColumn` escape hatch, and provenance helpers in a shared `data_frame.py` that #17 extends;
  arrays / images / structs are #17's, reachable meanwhile by passing hand-built protos through.  Raw-protos-only
  was rejected because the cookbook's calculations recipe would be ~25 lines of proto building per column.

- **Q5 — pandas bridges.  RESOLVED: in scope, as Phase 3.**  `calculations_from_dataframes()` /
  `calculations_to_dataframes()` (D7) are the reason an analyst would use calculations; they reuse the
  `[analysis]` extra and the `query_conversions` conventions.  Phased last so Phases 1–2 can merge first.

- **Q6 — Optional `criteria`, and back-porting it.  RESOLVED: optional on the new clients; back-port is #41.**
  The server treats an empty list as match-all on all five annotation-service queries, so `query_datasets()`
  with no criteria is a legitimate "list everything, paged" call.  The five existing helpers get the same
  default under [#41](https://github.com/osprey-dcs/dp-python-lib/issues/41), and the key-only `attributes()`
  fix under [#40](https://github.com/osprey-dcs/dp-python-lib/issues/40) (finding 5); both are non-breaking.

- **Q7 — Client-side validation depth.  RESOLVED: shape rules only (D11).**  One `TextCriterion`, one export
  source, count-match, unique names — not the server's size caps (256-char strings, 10M-element arrays, 50 MB
  images, 1 MB structs), which are deployment policy and would drift.

- **Q8 — Stub sync mechanism.  RESOLVED: dispatch the workflow.**  Dispatched 2026-09-09 as dp-grpc run
  34382676119 (`dry_run=false`, target `osprey-dcs/dp-python-lib` `main`); the resulting `grpc-sync-*` PR is
  verified against the local generation from finding 1 and merged on its own.  Committing locally generated
  stubs stays the fallback if the bot token lapses.

- **Q9 — PR structure.  RESOLVED: two PRs.**  Phases 1+2 (wrappers and builders, with the wrapper-level
  integration test listed under Phase 1) and Phases 3+4 (pandas bridges, the integration test's builder and export
  legs, full recipe, docs), each reviewable in one sitting; the ticket stays open until the second merges, as
  dp-service #248 did.  *Correction 2026-09-09 (review of #42):* the task list originally put the whole integration
  test in Phase 4, contradicting this answer; PR 1 carries the wrapper round trip, following the #8 precedent of
  verifying against a live server before merge.

- **Q10 — Adopting the planning convention now.  RESOLVED: docs-only PR now.**  This file, `plan/README.md`
  (from dp-grpc's), the `CLAUDE.md` "Ticket Planning Workflow" section (dp-service's wording, adapted), and the
  #13 regression test — so the ticket body links to a stable URL before implementation starts.

- **Q11 — Integration test environment.  RESOLVED: the #8 model.**  Unit tests are built against mocked stubs;
  the ticket owner starts an ecosystem built from dp-service `main` at or after PR #264 on `localhost:50053`
  before PR 1 merges (the wrapper-level integration test, Q9) and again for Phase 4, and the plan records the
  server commit each was verified against.  Nothing was listening on 50051–50053 when this was written.

## Reference: proposed API surface (for the CLAUDE.md usage section)

```python
from datetime import datetime, timezone
from dp_python_lib.client import (
    MldpClient, SaveDataSetRequestParams, SaveAnnotationRequestParams, ExportDataRequestParams, ExportFormat,
    DataSetQuery as DS, AnnotationQuery as AQ, data_block, calculations, calculations_spec, sampling_clock,
)
from dp_python_lib.client import data_frame as dfb

client = MldpClient()
ds, an, ex = client.annotation.datasets, client.annotation.annotations, client.annotation.export
t0 = datetime(2026, 7, 14, 18, tzinfo=timezone.utc)
t1 = datetime(2026, 7, 14, 19, tzinfo=timezone.utc)

# a region of the archive: one DataBlock per (time range, PV list)
saved = ds.save_dataset(SaveDataSetRequestParams(
    name="2026-07-14 ramp study", owner_id="cmcchesney",
    data_blocks=[data_block(t0, t1, ["BPMS:GUNB:314:X", "BPMS:GUNB:314:Y"])],
    tags=["ramp-study"], attributes={"runNumber": "4471"}, modified_by="cmcchesney"))
dataset_id = saved.dataset_id

# derived values with column-level provenance, one frame per time axis
frame = dfb.data_frame(
    sampling_clock(start_time=t0, period_nanos=1_000_000_000, count=3),
    [dfb.double_column("x_rms", [12.7, 12.8, 12.9], metadata=dfb.column_metadata(
        provenance=dfb.provenance(process="1 Hz RMS", derived_from=[dfb.pv_source("BPMS:GUNB:314:X", (t0, t1))])))])
result = an.save_annotation(SaveAnnotationRequestParams(
    name="BPM X RMS, shift 2", owner_id="cmcchesney", dataset_ids=[dataset_id],
    tags=["reviewed"], calculations=calculations({"bpm-statistics": frame})))
annotation_id, calculations_id = result.annotation_id, result.calculations_id

# find it again; results carry ids, so fetch the datasets for a page in ONE call
for a in an.iter_annotations([AQ.tags(["reviewed"]), AQ.attributes("runNumber")]):   # key-only search
    print(a.name, a.dataSetIds, bool(a.calculationsId))
datasets = ds.get_datasets([i for a in an.iter_annotations([AQ.datasets([dataset_id])]) for i in a.dataSetIds])

# read the calculations back (pandas via the [analysis] extra)
calc = an.get_calculations(calculations_id).calculations
from dp_python_lib.client import data_frame_conversions as dfc
frames = dfc.calculations_to_dataframes(calc)                       # {"bpm-statistics": DataFrame}

# export: saved dataset + calculations to HDF5; or ad-hoc blocks without saving anything
ex.export_data(ExportDataRequestParams(ExportFormat.HDF5, dataset_id=dataset_id,
                                       calculations_spec=calculations_spec(calculations_id)))
ex.export_data(ExportDataRequestParams("csv", data_blocks=[data_block(t0, t1, ["BPMS:GUNB:314:X"])]))

# tear down: annotations first (delete_dataset is rejected while referenced)
an.delete_annotation(annotation_id)
ds.delete_dataset(dataset_id)
```
