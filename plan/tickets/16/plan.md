# Issue #16 — Bucket-oriented v2 query API (`queryBuckets` / `queryBucketsStream`)

- **Ticket**: [osprey-dcs/dp-python-lib#16](https://github.com/osprey-dcs/dp-python-lib/issues/16)
- **Status**: written 2026-09-30 against dp-python-lib `a54e028`, dp-service `08c2038` (`origin/main`), and
  dp-grpc `e775244` (`main`; no proto change since `rel-1.16.0`, which the stubs were synced from in
  `2dfa1ee`).  The ticket is AI-drafted, and filed before #6 and #17 landed.  Its request-side analysis holds.
  Its conversion plan is out of date, because most of the machinery it describes has since been built (T1).
  It also gets two server facts wrong: `sampleStatusSelector` is not "future", and `useSerializedColumns` does
  not do what the proto says (T3, T6).  Findings are under
  [Background](#background--triage-findings).  Q1–Q4 were resolved with Craig on 2026-09-30; the issue body has
  been updated to match.

## Overview

`client.query` wraps the sample-oriented v2 query (`querySamples` / `querySamplesStream`), which returns an
aligned, trimmed, **scalar-only** `ColumnTable`.  Nothing in the library reads back what #17 made writable:
array, image, struct, and serialized columns can be ingested, but not queried.  A bucket query returns the
archive's stored units whole, each with its original typed column and time axis.

This ticket delivers:

- `query_buckets()` (unary, one resumable page), `iter_query_buckets()` (transparent paging), and
  `iter_query_buckets_stream()` (server-streaming), all on the existing `QueryClient`, taking the existing
  `QueryParams`.
- A new `bucket_conversions` module.  Its pure-Python half views a `DataBucket` as a one-column
  `common.DataFrame`, so every `data_frame_conversions` reader applies unchanged.  It also trims a bucket to
  `[begin, end)` exactly at the proto level.  Its pandas half (`[analysis]` extra) assembles buckets into one
  DataFrame per PV (Q1), with trimming opt-in (Q2).
- Live read-back of every column kind #17 can ingest, which closes the gap #17's integration test and cookbook
  both point at.

It lands as two PRs (Q3).  **PR A** is the client, the conversions, and unit tests.  **PR B** is the live tests
and the documentation that depends on them.

## Background / triage findings

dp-service citations are `origin/main` @ `08c2038`, relative to `src/main/java/com/ospreydcs/dp/service/`.
Abbreviations: `RES` = `query/handler/QueryV2Resolver.java`, `MQC` =
`query/handler/mongo/client/MongoSyncQueryClient.java`, `FB` = `common/mongo/MongoQueryFilterBuilder.java`,
`UD` / `SD` = `query/handler/mongo/dispatch/QueryBuckets{Unary,Stream}Dispatcher.java`, `BD` =
`common/bson/bucket/BucketDocument.java`.  The server design record is dp-service `plan/tickets/189/plan.md`.

- **T1 — The conversion layer the ticket scopes is mostly built.**  #6 wrote `data_frame_conversions` with
  `column_values()` as a standalone per-column converter "because the bucket query (#16) carries the same 14
  typed column messages".  #17 added the non-scalar builders, plus the companion accessors for the structural
  fields: `column_dimensions()`, `image_descriptor_dict()`, and `column_schema_id()`, with `enum_ids` in
  `df.attrs`.  #6 wrote `expand_data_timestamps()` for integer-nanosecond `SamplingClock` expansion.
  `data_frame._slice_frame()` (#17, for `split_data_frame()`) already slices a frame's rows exactly, including
  a `SamplingClock` start shift in integer nanoseconds.  So the ticket's "16-arm typed-column extraction" and
  "samplingClock expansion" need no new code.  The real work is (a) wrapping a bucket as a frame, (b)
  assembling a PV's buckets across their separate axes, and (c) trimming.  The ticket also points at #7's
  `DataValue`/`Image` handling (`query_conversions`) as the thing to reuse.  That is the wrong layer: only the
  legacy `DataColumn` arm goes through `data_value_to_python()`, and `column_values()` already does that.

- **T2 — Shapes, from the regenerated stubs.**  `QueryBucketsRequest` is `querySpec = 1`,
  `executionOptions = 2`, `resultRepresentation = 3`, the same three messages as `QuerySamplesRequest`, as the
  ticket says.  `QueryBucketsResponse` is `responseTime = 1`, then oneof `result` of `exceptionalResult = 10` |
  `bucketQueryResult = 11`.  `BucketQueryResult` holds `repeated DataBucket dataBuckets = 1` and
  `nextPageToken = 2`.  `DataBucket` holds `pvName = 1`, `dataTimestamps = 2`, `providerId = 3`,
  `providerName = 4`, and `DataValues dataValues = 5`.  `DataValues` is a oneof `values` of **one** column:
  `dataColumn = 10`, `serializedDataColumn = 11`, `double/float/int64/int32/bool/string/enum/image/structColumn`
  `= 12–20`, and `double/float/int32/int64/boolArrayColumn = 30–34`.  Each column carries its own `name` and
  `metadata`.  The bucket itself has no first/last time field.

- **T3 — `sampleStatusSelector` has landed, and the server rejects it on buckets.**  The ticket calls it
  "future" and says it "would benefit both sample and bucket paths equally".  It shipped in 1.16.0 for samples
  only.  `RES` L163-168 rejects its *presence*, so an empty message is rejected too:
  `"bucket-oriented methods do not support per-sample status filtering; remove sampleStatusSelector or use
  querySamples/querySamplesStream"`.  The check runs after every other validation, so an earlier error wins.
  `_build_query_spec()` already carries a note that a bucket builder must refuse the filter rather than copy
  it through (D2).

- **T4 — Selection is an overlap test, returned whole, never trimmed.**  The ticket's claim holds.  The
  filter is `firstTime < end AND lastTime >= begin` (`FB` L143-171), which is half-open `[begin, end)`.
  `dataBucketFromDocumentV2` (`BD` L235-269) copies the stored axis and column unchanged, and dp-service pins it
  with `MongoSyncQueryBucketsV2Test.testWholeBoundaryBucketsNotTrimmed`.  The ticket misses one consequence:
  with a `configurationSelector`, each activation interval becomes a fragment of one `$or` find (`MQC`
  L864-887).  A bucket that spans a gap between intervals is returned once, whole, carrying the samples in the
  gap too.  Trimming to `[begin, end)` does not remove those (D5).

- **T5 — Order, paging, and limits.**
  - **Order** is ascending `(pvName, firstTime)` (`MQC` `bucketSort()` L995-1000; merged the same way across
    span classes by `MergedBucketCursor`).  So each PV's buckets arrive contiguous and in time order.  Neither
    the proto nor the dp-grpc cookbook promises this; the cookbook says not to rely on any order.  The client
    therefore does not depend on it (D4).
  - **`limit` counts buckets**, not rows.  `QueryParams.limit`'s docstring says rows.  0 means the default,
    10,000.  Anything over 100,000 is **silently clamped**.  Both are server configuration (`RES` L132-137).
  - **A page is also cut by bytes**: by the outbound budget (`GrpcServer.incomingMessageSizeLimitBytes`,
    default 4,096,000) summed over `DataBucket.getSerializedSize()`.  The page then ends early *with* a token
    (`UD` L111-118), so a page can hold fewer than `limit` buckets while more remain.  Every page holds at
    least one bucket.  A single bucket bigger than the whole budget is an `ERROR`: `"single bucket for pv <pv>
    exceeds the outgoing message size limit (<n> > <budget> bytes)"` (`UD` L120-129, `SD` L132-141).
  - **`nextPageToken`** is a stateless, position-only keyset token over `(pvName, firstTime)` (dp-service
    plan 189 Q2-Q3).  It is not bound to the query that produced it: only its kind (bucket vs sample) is
    checked, and a sample token is rejected with `"pageToken is not valid for this query type"`.  An empty
    token means the last page.
  - **The stream** reads the whole result, never pages, and cuts messages by `limit` and by the same byte
    budget.  `nextPageToken` is `""` on every message.  A token on the request is rejected: `"pageToken must
    not be set on a streaming query"`.  An empty result is one empty message.
  - **An empty result is success**, not an `ExceptionalResult`, as on the samples path.

- **T6 — What the server puts in a bucket.**
  - The column comes back **in its stored form**.  A `DoubleColumn` stays a `DoubleColumn`; arrays keep their
    `dimensions`, images their descriptor, structs their `schemaId`, and enums their `enumId`.  A legacy
    ingest comes back as `dataColumn`.  A column ingested as a `SerializedDataColumn` comes back as one, with
    its `encoding` and `payload`.
  - The axis is the **ingested `DataTimestamps` verbatim**, so a `SamplingClock` stays a `SamplingClock`.
  - A stored `TimestampList` is **non-decreasing, with duplicates allowed**: ingestion rejects a decreasing
    list (`ingest/handler/IngestionValidationUtility.java` L172-177, `"... is not non-decreasing"`, enforced
    since rel-1.13.0).  That is looser than this library's write path, which requires *strictly* increasing
    timestamps (`timestamp_list()`, `timestamp_count()`), so a bucket another client ingested can carry
    duplicate timestamps that the client would refuse to write.
  - The column's `name` is the ingested column name.  Ingestion keys the bucket by that name, so it equals
    `pvName`.
  - `providerId` / `providerName` are the ingesting provider *of that bucket*.  The proto's "most recent
    ingestion request" wording is loose.
  - **`excludeColumnMetadata` works on buckets** (`BD` L253-255, 277-302), unlike samples, where the server
    populates no metadata.  So a bucket query is the first path where `ColumnMetadata`, and the provenance
    #17 ingests, can be read back live.
  - **`useSerializedColumns` is inert on buckets**, contrary to `query.proto` L512-516 and the dp-grpc
    cookbook ("each DataBucket carries a SerializedDataColumn instead of a typed column").  The flag is passed
    in and never read.  A column comes back serialized only if it was *stored* serialized.  This is the
    "pass-through only, this phase" decision in dp-service plan 189 §11.2.4(ii).  The ticket's "serialized
    columns deferred, same as #7 (fail-loud)" therefore misreads the situation.  On buckets, a serialized
    column is not a representation the client opts into.  It is user data that a #17 caller wrote with
    `serialized_column()`, and the bucket query is the only way to get it back (D6).
  - A stored bucket that cannot be parsed is an `ERROR`: `"exception building bucket result: ..."`.

- **T7 — Validation the client already covers, and what is new.**  The resolver checks the time range, the
  PV selector, the configuration selector, and the PV-count cap (10,000 by default) in that order (`RES`
  L72-191).  `QueryParams` already enforces time-range ordering and a non-empty selector (#17 PR B).  The only
  bucket-specific rules are T3 (refuse the status filter) and the streaming token (the client never sends
  one).

- **T8 — Existing code and docs that name #16 as future work**, all of which go stale when this lands (the
  review lesson from #40/#41):
  - `query_client.py`: the `QueryParams` class docstring ("in a future release"), the `_build_query_spec()`
    note, and the `limit` docstring ("rows"), which becomes kind-dependent.
  - `data_frame_conversions.py`: the module docstring and `column_values()` docstring ("in future a bucket
    query").
  - Test docstrings in `tests/unit/test_data_frame_conversions.py` (L62, L830), plus
    `tests/integration/test_ingestion_client_integration.py`'s section header and
    `test_non_scalar_columns_reach_success`, which stops at SUCCESS "until #16 wraps queryBuckets" and is
    upgraded to a read-back in PR B.
  - Docs: `README.md` (TODO list L116-117), `doc/cookbook/query.md` (L389-402), `conventions.md` (L297-299),
    `ingestion.md` (L325-328), `sample-status.md` (L370-373), `doc/release-notes/NEXT.md` (L192-194), and
    `CLAUDE.md` ("until the bucket query (#16)", and the `_build_query_spec()` seam note).
  - `iter_frame_columns()` skips serialized columns and `column_values()` raises on one, both as #6 intended.
    Neither changes; D6 handles serialized columns in the bucket layer.

- **T9 — Other open tickets: nothing folds in.**
  - **#68** (paging for `queryRequestStatus`) waits on dp-grpc#165 and dp-service#302, both open as of
    2026-09-30.  It is a different service, with nothing to share with this work beyond the paging idiom.
  - **#61** step 5 (drop the stub-type suppression, type `_stub`, `py.typed`) waits for the next dp-grpc
    release to sync `.pyi` stubs.  It doesn't fold in, but it constrains PR A: per `CLAUDE.md`, code touching
    proto types must be clean against locally generated typed stubs, not only against today's `Any`.
  - The README's other unwrapped query RPCs (`queryData`, `queryTable`, `queryPvStats`, `queryProviders`)
    have no tickets and stay out of scope.

- **T10 — Version floor.**  `queryBuckets` is part of the v2 query API, which exists from `rel-1.15.0`.  The
  behaviors above (byte-budget paging, span-class merge, `excludeColumnMetadata`) are read from dp-service
  `main`.  No doc may claim they were "verified against rel-1.X.0" unless the live tests actually ran against
  that tag (the #6 lesson).

## Design decisions

- **D1 — Bucket methods live on `QueryClient` and take `QueryParams`.**  This is the ticket's proposal, and
  what #7 built the seam for.  `query_buckets(params, page_token=None) -> QueryBucketsApiResult`,
  `iter_query_buckets(params) -> Iterator[QueryBucketsApiResult]` (raises `RuntimeError` on a page error), and
  `iter_query_buckets_stream(params)` (lazy; raises `RuntimeError` on a mid-stream error).  These are the exact
  contracts of the three sample methods, so a caller switching kinds changes a method name only.  Rejected: a
  separate `BucketQueryParams`.  The request is identical, and a second params class would split every doc
  and helper.

- **D2 — The bucket builder refuses `sample_status_filter` with a `ValueError`**, raised when the request is
  built, before any RPC.  It names the samples methods as the alternative, echoing the server's text (T3).
  Rejected: dropping the filter silently (the caller would get unfiltered data believing it filtered), and
  sending it (a guaranteed server reject, one round trip later).  The check lives in
  `_build_query_buckets_request()`, not in `QueryParams`, which stays kind-neutral.

- **D3 — `QueryBucketsApiResult`** exposes `data_buckets` (a list, empty on error) and `next_page_token`.  It
  has **no `to_dataframe()`**, because a bucket page is not one table.  Instead,
  `to_dataframes(time_range=None, exclude_column_metadata=False)` delegates to
  `bucket_conversions.buckets_to_dataframes()` (D5).  `useSerializedColumns` is forced `False`, as on the
  samples path: it is inert on buckets (T6), and exposing it would advertise a behavior the server lacks.
  `exclude_column_metadata` is passed through, and on this path it matters (T6).

- **D4 — Assembly groups by `pvName`; it does not rely on arrival order** (Q1: per-PV dict).  The server's
  PV-then-time order is an implementation fact, not a contract (T5).  `buckets_by_pv()` groups while keeping
  first-seen PV order, and orders each PV's buckets by first timestamp (a stable sort, so equal starts keep
  server order).  On today's server that is a no-op, and it stays correct if the order changes.  It also
  makes a PV split across pages or stream messages assemble correctly once the pages are combined.
  **Overlapping or duplicate timestamps across a PV's buckets are kept, not deduplicated.**  Two ingests with
  overlapping ranges and different first timestamps produce two buckets, and choosing between them is not
  the client's call.  The pandas index is then non-monotonic or non-unique, and the docs say so.

- **D5 — Trimming is exact, opt-in, and done at the proto level** (Q2).  `trim_bucket(bucket, begin, end) ->
  DataBucket | None` keeps rows with `begin <= t < end`, compared as integer epoch nanoseconds, and returns
  `None` when nothing is left.  It reuses `data_frame._slice_frame()`, so a `SamplingClock` bucket stays a
  `SamplingClock` with a shifted start.  `_slice_frame()` takes one contiguous row span, which is exact here
  because a stored axis is non-decreasing (T6): the row bounds come from `bisect_left` for both `begin` and
  `end` on the expanded axis (correct with duplicate timestamps under the half-open rule), or arithmetically
  for a clock.  An axis that *is* decreasing -- corrupt, or written before rel-1.13.0 -- raises `ValueError`
  naming the PV and the first out-of-order position rather than being scanned, since no contiguous span can
  trim it exactly.  The pandas conversions take `time_range=(begin, end)`, default `None`, meaning untrimmed.
  The whole-query convenience takes `trim=False` and uses the params' own range when set.  The docs state the
  limit from T4: trimming removes samples outside `[begin, end)` but not samples in gaps between
  configuration intervals, and a caller who needs either exactly should use `querySamples`.  A serialized
  bucket cannot be trimmed (its payload is opaque), so `trim_bucket()` raises on one rather than returning it
  whole.  Rejected: trimming by default, which hides what the server returned.
  - **`begin` must be strictly before `end`**, on `trim_bucket()` and on every `time_range=`, raising
    `ValueError` otherwise.  A reversed or empty range would otherwise return `None`, indistinguishable from a
    valid range with no samples.  This reuses the check (and message) `data_frame.py` already applies to a
    provenance `time_range`, as `QueryParams` and `data_block()` do for theirs.
  - **Read-side checks only.**  A bucket is server data, not something the client is about to write, so
    `bucket_to_data_frame()`, `trim_bucket()`, and the pandas path never call `validate_data_frame()` or
    `timestamp_count()`: their strictly-increasing rule would make a bucket with duplicate timestamps (T6)
    unreadable.  Instead, `trim_bucket()` first runs the read-side alignment check `data_frame_conversions`
    uses (one sample per timestamp by the column kind's count rule, raising on absent array dims).
    `_slice_frame()` documents its input as already validated, and on an array column with no `dims` it falls
    back to a block of 1 and slices silently wrong, so this check is what makes reusing it safe.

- **D6 — Serialized buckets are passed through in pure Python and refused in pandas.**
  `bucket_column(bucket)` returns whichever column arm is set, a `SerializedDataColumn` included, so the
  payload is always reachable.  `bucket_to_data_frame()` places it in `serializedDataColumns`, where the
  `data_frame_conversions` readers skip it by design.  `bucket_values()` and the pandas conversions raise
  `ValueError` naming the PV and its `encoding`, pointing at `bucket_column()`.  This follows the #6 rule that
  a conversion must never silently drop data it was handed.  Decoding a payload stays out of scope: the
  encoding contract is the caller's.

- **D7 — The pandas result is `dict[pv_name, pandas.DataFrame]`** (Q1), one frame per PV.  Each frame has a
  UTC `DatetimeIndex` built from int64 nanoseconds, and one column named for the PV, with the narrow dtype its
  column type implies.  It is produced by running `data_frame_to_pandas()` on each wrapped bucket and
  concatenating, so dtypes, `enum_ids`, and array/image handling are the calculations path's, unchanged.
  **The combined frame's `attrs` are rebuilt from scratch after concatenation**, never inherited from it:
  how `pd.concat` propagates `attrs` has varied across pandas versions.  `enum_ids` is carried even under
  `exclude_column_metadata=True`, as `data_frame_to_pandas()` does, since without it the column cannot be
  rebuilt as an enum.
  - **Consistency across a PV's buckets is checked, fail-loud.**  A PV whose buckets differ in column kind
    (ingested as double, later as int32), or in a structural field (`enumId`, array dims, image descriptor,
    `schemaId`), raises `ValueError` naming the PV and both first timestamps, and points at the per-bucket
    readers.  Concatenating anyway would silently widen dtypes or mix incompatible payloads.
  - **Metadata is kept per bucket, because it can legitimately differ between buckets** (each ingest carries
    its own provenance).  `df.attrs["buckets"]` is always a list of per-bucket descriptors: first/last epoch
    nanos, sample count, `provider_id`, `provider_name`, and `column_metadata` (omitted under
    `exclude_column_metadata`).  `df.attrs["column_metadata"]` keeps the existing `{name: dict}` shape, set only
    when every bucket's metadata is identical.  Keeping only the first bucket's metadata would repeat the #6
    defect of carrying information and then dropping it.  This is decided without a question and flagged for
    plan review.
  - Rejected: a wide aligned frame (Q1).  It NaN-widens integers, turns non-scalars into sparse object
    columns, and is what `querySamples` already provides for scalars.

- **D8 — Whole-query convenience: unary only.**
  `query_buckets_to_dataframes(query_client, params, *, trim=False, max_buckets=None)` pages
  `iter_query_buckets()`, stops with a `ValueError` once `max_buckets` would be exceeded (mirroring
  `query_samples_to_dataframe()`'s `max_rows`), and assembles.  There is **no streaming pandas convenience**:
  a PV can span stream messages, so per-message frames would be PV fragments, which misleads.  Stream callers
  collect buckets and call `buckets_to_dataframes()` once, or convert each message knowingly.  The cookbook
  shows both.

- **D9 — One stream sender for both query kinds.**  `_send_query_samples_stream()` would otherwise be copied
  near-verbatim.  PR A factors a private `_iter_stream(stub_call, request, result_cls, success_field,
  op_name)` in `QueryClient` that both senders call.  Its behavior matches today's yield-errors contract
  exactly, including the error-message texts the tests match on.  `CLAUDE.md`'s "leave the streaming senders
  as they are" means *don't route them through `_dispatch`*, and this keeps to that.  The sample-status
  stream sender, in another module, is left alone.  Flagged for plan review; the fallback is a second copy.

- **D10 — `bucket_conversions` is a new module** (`src/dp_python_lib/client/bucket_conversions.py`), pure
  Python at import, with pandas imported lazily inside the pandas entry points, as in the two existing
  conversion modules.  Rejected: adding to `data_frame_conversions`, which is about one frame and is already
  784 lines.  The bucket module depends on it, not the other way round.

## Implementation tasks

### PR A — client, conversions, unit tests (`Refs #16`)

**`src/dp_python_lib/client/query_client.py`**
- `QueryBucketsApiResult(ApiResultBase)` with `data_buckets`, `next_page_token`, and `to_dataframes(...)`
  (D3), mirroring `QuerySamplesApiResult`.
- `_build_query_buckets_request(params, page_token=None)`: raise per D2, reuse `_build_query_spec()`, set
  `limit` / `pageToken` as the samples builder does, and force `useSerializedColumns = False` and
  `excludeColumnMetadata`.
- `_send_query_buckets()` through `_dispatch` (`"bucketQueryResult"`, `"queryBuckets"`), with a
  `success_log` reporting the bucket count.  `query_buckets()`, `iter_query_buckets()`.
- `_iter_stream()` (D9); `_send_query_samples_stream()` and a new `_send_query_buckets_stream()` both call it.
  `iter_query_buckets_stream()`.
- Docstrings: `QueryParams` (no "future release"; `limit` is rows for samples and **buckets** for buckets,
  and a bucket page may be cut short by bytes, T5), and the `_build_query_spec()` note rewritten as a pointer
  to D2's check.

**`src/dp_python_lib/client/bucket_conversions.py`** (new; D4–D8, D10)
- Pure Python:
  - `bucket_column(bucket)` (raises if `dataValues` is unset).
  - `bucket_to_data_frame(bucket) -> common.DataFrame` (one column, routed to the matching repeated field; an
    unset axis raises).
  - `bucket_timestamps(bucket) -> list[int]` and `bucket_values(bucket) -> list` (D6), with the alignment
    check shared with `data_frame_conversions`.
  - `trim_bucket(bucket, begin, end)` (D5; inputs through `to_timestamp()`), and `buckets_by_pv(buckets)`
    (D4).
- `[analysis]`: `buckets_to_dataframes(buckets, *, time_range=None, exclude_column_metadata=False)` (D7) and
  `query_buckets_to_dataframes(...)` (D8).
- `_slice_frame` is private in `data_frame.py`, so either import it privately with a comment or promote it to
  a module-level helper both callers use.  Prefer promoting (the query-support rule in `CLAUDE.md`), and keep
  the name private-by-convention if it does not belong in the public builder API.

**`src/dp_python_lib/client/__init__.py`**: export `QueryBucketsApiResult` and the public
`bucket_conversions` names, matching how `query_conversions` is exported today.

**Stale pointers (T8)**: update the `data_frame_conversions` docstrings and the two
`test_data_frame_conversions.py` docstrings.

**Tests**
- `tests/unit/test_query_client.py`:
  - request building, including the D2 refusal and that `useSerializedColumns` is forced `False`;
  - three-tier error handling for the unary call;
  - paging that follows tokens and stops on `""`;
  - the stream never setting a token, raising on a mid-stream error, and an empty stream;
  - unchanged sample-stream behavior after D9 (the existing tests must pass untouched).
- `tests/unit/test_bucket_conversions.py` (new):
  - every one of the 16 arms through `bucket_to_data_frame()` and `bucket_values()`;
  - both axis forms, with nanosecond exactness at a present-day epoch;
  - trimming at both bounds (a sample exactly at `end` excluded, one exactly at `begin` kept), a
    `SamplingClock` start shift, trim-to-nothing returning `None`, duplicate timestamps sitting exactly at
    each bound, a decreasing `TimestampList` raising, `begin >= end` raising (on `trim_bucket()` and on
    `time_range=`), an array bucket with absent dims raising rather than slicing, and a serialized bucket
    raising;
  - a bucket with duplicate timestamps converting and trimming without error (no write-side validation);
  - grouping with interleaved PVs and out-of-order buckets;
  - the kind- and structure-mismatch errors;
  - uniform versus differing metadata in `attrs`, and `exclude_column_metadata` (with `enum_ids` still
    present on an enum PV);
  - a legacy `DataColumn` gap becoming `None`;
  - a serialized bucket reachable through `bucket_column()` and refused by the pandas path;
  - `max_buckets`.
  - pandas tests skip cleanly without `[analysis]`.
- **Typed-stub check**: generate stubs locally with the `[codegen]` extra at dp-grpc's pinned versions and
  flags, and run `mypy src/` against them (T9).

**Docs in PR A**: the `NEXT.md` section (new methods, the new module, and the behaviors: untrimmed buckets,
`limit` counting buckets, the status-filter refusal, serialized pass-through), and the `CLAUDE.md` Key Files
entries.

### PR B — live tests and docs that need real data (`Closes #16`)

- `tests/integration/test_query_buckets_integration.py` (new), ingesting through `ingest_support`:
  - a `SamplingClock` bucket read back with an identical axis;
  - a sub-window query returning the whole boundary bucket, then `trim_bucket()` reducing it exactly;
  - two PVs, each contiguous, reassembled across pages at a small `limit`;
  - the stream matching the unary union;
  - `ColumnMetadata` provenance read back live, and absent under `exclude_column_metadata` (the first live
    check of column metadata, T6).
- Upgrade `test_non_scalar_columns_reach_success` to read back exact values, dims, image descriptor,
  `schemaId`, and the serialized payload and encoding.  Rename it and update its section header.
- Cookbook:
  - a bucket section in `query.md`, replacing the "not yet wrapped" note, and covering whole buckets, opt-in
    trimming and its configuration-gap limit, per-PV frames, `limit` counting buckets, and serialized
    pass-through;
  - the #16 pointers in `conventions.md`, `ingestion.md` (show the read-back), and `sample-status.md` (the
    status filter is samples-only);
  - `README.md`'s TODO and feature list;
  - the `NEXT.md` paragraph at L192-194.
- `CLAUDE.md`: a "Bucket Query API" section recording T4–T6 and D2/D5–D7 as invariants (including the
  read-side-checks-only rule and why the stored axis can carry duplicates), plus the
  `_build_query_spec()` and "until #16" lines.
- Run the cookbook snippet checker, and run the new and changed recipes as one continuous script against a
  live stack (the #6 lesson).

## Out of scope

- The legacy `queryData` / `queryTable` and the stats/provider RPCs (no tickets; README TODO).
- `useSerializedColumns=True` on either query kind (inert on buckets, T6), and decoding serialized payloads.
- A wide, aligned pandas view of buckets (Q1; `querySamples` covers scalars).
- Trimming to configuration-interval gaps (T4, D5).
- A streaming pandas convenience (D8).
- #68 and #61 (T9); PyTorch (the existing breadcrumb on `column_table_to_numpy()`).

## Dependencies and sequencing

- Nothing blocks PR A: the stubs are current (dp-grpc rel-1.16.0 = `main` for the protos).  PR B needs a live
  stack with ingestion and query, which #17 already requires.
- PR B follows PR A.  The plan PR merges first (`Refs #16`).
- Not blocked by, and not blocking: #68, #61, and dp-grpc#167 (Q4).  If a typed-stub sync (#61) lands
  mid-flight, PR A's typed-stub check already covers it.

## Open questions

All resolved with Craig on 2026-09-30.

- **Q1 — Pandas shape?**  *Resolved: a per-PV dict* (D7).
- **Q2 — Trimming?**  *Resolved: opt-in, exact, `[begin, end)` only* (D5).
- **Q3 — One PR or two?**  *Resolved: two*, as #17 did.
- **Q4 — File the upstream doc contradictions?**  These are `query.proto` L512-516 and the dp-grpc cookbook
  on `useSerializedColumns` (T6), the cookbook's claim that an unbounded unary query "can exceed the limit
  and fail" when the server ends the page instead (T5), the undocumented byte-cut stream messages, a page
  token not bound to its query, and the loose `providerId` wording.  An open-ticket check on 2026-09-30 found
  none covering them.  dp-grpc's open issues are #148, #150, #165, and #166, none on the bucket query.  In
  dp-service, #189 records pass-through as deliberate "this phase" and #195 is closed.  *Resolved: filed as
  [osprey-dcs/dp-grpc#167](https://github.com/osprey-dcs/dp-grpc/issues/167), a sub-issue of epic
  osprey-dcs/data-platform#104.*  Nothing here waits on it; the client docs follow dp-service.

Decisions taken without a question, flagged for plan review: D2 (refuse, not drop, the status filter), D4
(no dedup across overlapping buckets), D6 (serialized pass-through), D7's per-bucket `attrs["buckets"]`,
D8 (no streaming pandas convenience), and D9 (the shared stream sender).
