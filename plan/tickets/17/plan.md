# Issue #17 — Ingestion API client

- **Ticket**: [osprey-dcs/dp-python-lib#17](https://github.com/osprey-dcs/dp-python-lib/issues/17)
- **Status**: written 2026-09-27 against dp-python-lib `b11ff12`, dp-service `7e8b2e6` (`origin/main`), and
  dp-grpc `e775244`.  The ticket is AI-drafted.  Its intent holds: Python has no way to get data *in*, and
  that blocks the closed-loop query test, the cookbook's ingestion recipe, and the sample-status filtering
  verification.  But its central premise is out of date: the "shared payload/params model" it scopes as
  Phase 1 already exists, built by #6 (T1).  It also misses what an ack actually means (T2), which is why
  `queryRequestStatus` moves into scope.  Findings are under
  [Background](#background--triage-findings).  Scope questions Q1–Q5 were resolved with Craig on
  2026-09-27 and 2026-09-28; the issue body has been updated to match Q1–Q4 (Q5 is design-level).

## Overview

Today `IngestionClient` wraps `registerProvider()` and nothing else.  Three integration tests each ingest
their own data by hand through the generated stub, and three cookbook recipes carry notes saying their
data path was never observed, because no Python caller could put data in.

This ticket delivers the ingestion side of the library:

- `ingest_data()`, the unary call.
- `ingest_data_stream()` (client-streaming) and `iter_ingest_data_bidi_stream()` (bidirectional).
- `query_request_status()` plus `await_request_statuses()`, the only way to learn whether an acked
  request actually landed (T2).
- The array, image, struct, and serialized column builders that #6 deferred here, and the
  message-level checks those column kinds need in `data_frame()` (T7).
- `split_data_frame()`, which cuts a frame into request-sized pieces under the server's 4 MB message cap
  and 1-day span cap (T6).
- Closed-loop live tests, an ingestion cookbook recipe, and re-verification of the recipes that could not
  be checked without real data.

It lands in two PRs (Q3): **PR A** is the client and its unit tests; **PR B** is the live tests and the
documentation that depends on them.

## Background / triage findings

All dp-service citations are `origin/main` @ `7e8b2e6`, paths relative to
`src/main/java/com/ospreydcs/dp/service/`.

- **T1 — The payload model the ticket scopes as Phase 1 already exists.**  #6 built `data_frame.py`
  (axis builders, typed scalar column builders, `data_frame()` assembly with client-side shape checks) and
  `data_frame_conversions.py` (read-back, pandas both ways).  Its module docstring says it is "the
  substrate issue #17 extends rather than a parallel one to fork", because `IngestDataRequest.
  ingestionDataFrame` is the same `common.DataFrame` as an annotation's calculations.  So Phase 1 is not
  "design a params model for a DataFrame".  It is: a thin `IngestDataRequestParams(provider_id, frame,
  client_request_id)`, the request/result classes, and the builders #6 left out.  `data_frame_from_pandas()`
  already gives a pandas-to-ingest path for free.

- **T2 — An ack means "passed validation", not "ingested".  Even a bad `providerId` is acked.**
  `IngestionServiceImpl.handleIngestionRequest()` (L283-303) validates, sends the ack or reject, and only
  *then* enqueues the request for `IngestDataJob`.  The job looks up the provider (`IngestDataJob.java`
  L92-106), so an unknown or malformed `providerId` is acked and then recorded asynchronously as ERROR
  `"invalid providerId: <id>"`.  This contradicts two proto comments: `IngestDataResponse` lists "invalid
  providerId" as a *reject* reason, and `RegisterProviderResponse` says the ingest methods "confirm that the
  specified providerId is valid before processing".  Other async failures are also invisible at ack time:
  bucket building errors, and Mongo insert failures such as a duplicate `(pvName, first timestamp)` (T5).
  The **only** confirmation is the `requestStatus` document, which the job writes *after* the buckets
  (L160-197).  A SUCCESS status therefore means the data is persisted and queryable.  This is why the
  ticket's exclusion of `queryRequestStatus` was reversed (Q1).

- **T3 — `queryRequestStatus` behavior, for the wrapper.**
  - Criteria are ANDed (`MongoSyncIngestionClient.executeQueryRequestStatus`, L245-337).  `providerId`,
    `providerName`, and `requestId` match exactly.  `requestId` is the `clientRequestId` (the doc is built
    from `request.getClientRequestId()`, `IngestDataJob.java:184`).  Status matches with `$in`.
  - **`RequestIdCriterion` carries one id**, and two of them AND to an empty result, so one query cannot
    ask about several requests.  A caller waiting on N requests either issues N queries or queries more
    broadly and filters client-side (D6).
  - Each criterion is validated in `IngestionServiceImpl.queryRequestStatus()` (L401-479): a blank
    `providerId` / `providerName` / `requestId`, an empty status list, a `beginTime` under 1 s, or an unset
    criterion is rejected.  The Mongo client's own `isBlank()` skips (L253-294) are therefore unreachable,
    not a silent-broadening path.
  - The time range matches the status document's `createdAt`, the moment the job *finished*, inclusive at
    both ends, at millisecond resolution.  It is not receipt time and not data time, although the proto says
    "the time indicated for the IngestDataRequest".  `end <= 0` means "now".
  - **An empty criteria list is rejected** (`IngestionServiceImpl.java:414-417`, with a missing `return`
    that makes the server log an exception after replying).  Unlike the annotation-service queries (#41),
    criteria are therefore required client-side.
  - **No limit and no paging.**  All matches come back in one message.  A broad query can exceed gRPC's
    default 4 MB *receive* limit on the client.  That is a gap in the API, not a client concern: the method
    needs paging upstream: [osprey-dcs/dp-grpc#165](https://github.com/osprey-dcs/dp-grpc/issues/165) (proto)
    and [osprey-dcs/dp-service#302](https://github.com/osprey-dcs/dp-service/issues/302) (handler), under the
    [data-platform#104](https://github.com/osprey-dcs/data-platform/issues/104) epic.  Until then the
    client bounds its own queries by time range (D6).
  - There is **no unique index on `(providerId, clientRequestId)`**: re-using an id yields several
    documents.
  - **The enum's zero value is `INGESTION_REQUEST_STATUS_SUCCESS`.**  An unset status field reads as
    SUCCESS, the same silent-zero hazard #26 met with `endTime`.  The wrapper can't prevent a caller
    reading the raw field, but it must never *default* a status, and the docs should say so.
  - A status document is written for rejected requests too (status REJECTED), with three exceptions: the
    "service is shutdown" reject, stream messages that arrive after close, and a job whose `execute()`
    throws (swallowed in `QueueHandlerBase.java:108-114`).  So a poller needs a timeout.  There is no
    config flag that disables status writes.

- **T4 — Streaming semantics differ from both the proto comments and the query client.**
  - **`ingestDataStream`** (`IngestDataStreamRequestObserver.java`, `IngestionStreamRequestObserverBase.java`)
    validates every request.  A reject does not end the stream, and every valid request is ingested.  The
    single response is sent only on the client's `onCompleted`.  `clientRequestIds` lists **every**
    received id, rejected ones included.  If anything was rejected, the payload is an `ExceptionalResult`
    with the fixed message `"one or more requests were rejected"`, and **no** `numRequests`.  But
    `rejectedRequestIds` is populated on that same error response.  So, unlike every unary result so far,
    the error result must keep the response: it is the only place the caller learns *which* requests failed.
  - If the client cancels or errors, `onError` only logs (base L87-90).  **No response is sent, and requests
    already received are still ingested.**  The proto says a response is sent "in response to an error
    handling the request stream"; it is not.
  - **`ingestDataBidiStream`** sends one response per request, in order, synchronously from `onNext`
    (`IngestDataBidiStreamRequestObserver.java:23-25`).  A reject does not close the stream.  Backpressure is
    implicit: the handler queue has capacity 1 and blocks, so a fast client is throttled by HTTP/2 flow
    control.
  - Shutdown-path bugs exist (a double `onCompleted` in both streaming observers).  They affect only a
    server that is shutting down, and are noted, not worked around.

- **T5 — Validation the server does and does not do**
  (`ingest/handler/IngestionValidationUtility.validateIngestionRequest()`, fail-first, one message).
  - Required: non-blank `providerId`, non-empty `clientRequestId` (whitespace passes), a frame, an axis, at
    least one column.  SamplingClock `count > 0`, `periodNanos > 0`, start time present.  TimestampList
    non-empty and **non-decreasing**, so duplicates are accepted.  A span cap
    (`Buckets.maxBucketSpanSeconds`, default 86400 s) applies to both axis forms.
  - Column names: non-empty only (whitespace passes), unique across all 16 arms.  Every counted kind must
    match the sample count.  Enum needs a non-empty `enumId`.  Arrays need 1–3 dims, each > 0, with
    `values == samples * prod(dims)`.  Images need a descriptor with positive width/height/channels and a
    non-empty encoding, and `images` count must match.  Structs need a non-empty `schemaId`.  Serialized
    columns need only a name and a non-empty `encoding`: **no count check**.
  - Caps: 256-char strings, 10M elements per array sample, 50 MB images, 1 MB struct values, 256-char
    metadata strings, ≤ 20 tags/attributes, and a **4,096,000-byte inbound message limit**
    (`common/server/GrpcServerBase.java:22`, configurable as `GrpcServer.incomingMessageSizeLimitBytes`).
    Over the message limit, the call fails with a gRPC status, not an `ExceptionalResult`.  On a stream it
    kills the whole stream.  There is no cap on rows or columns.
  - A legacy `DataColumn` with mixed `DataValue` types is **not** rejected; the stored type comes from the
    first value.  `DataColumn` is deprecated and slated for removal in 2.0
    (dp-service `plan/features/v2-ingestion.md:80`).
  - Re-ingesting the same PV with the same first timestamp is acked, then fails asynchronously
    (`"MongoException in insertMany: ..."`), because the bucket `_id` is `pvName-firstSeconds-firstNanos`.
    `insertMany` is ordered, so earlier columns of that request may already be written: **a partial write
    whose status says ERROR**.

- **T6 — The two caps make chunking a real need, not a nicety.**  At ~4 MB per message, a frame of
  doubles tops out around 500k values.  A day of one 10 kHz PV is 864M.  Any "ingest my pandas DataFrame"
  story hits the cap immediately, and on a stream the failure kills every request after it.  Hence
  `split_data_frame()` (Q4).

- **T7 — `data_frame()` does not enforce the per-kind rules the new builders need.**  `_check_column()`
  checks names, counts, and array divisibility.  It does not check an enum's `enumId`, an array's 1–3 dim
  count, an image's descriptor, a struct's `schemaId`, or a serialized column's `encoding`.  A hand-built
  `EnumColumn` with no `enumId` passes `data_frame()` today and is rejected by the server.  This is the
  recurring #6 defect shape: a builder enforces a rule the shared validation doesn't, so a hand-built proto
  walks past it.  The new rules belong in `_check_column()`, not only in the builders.

- **T8 — Where the client is stricter than the server, and stays so.**  `data_frame()` rejects blank
  (whitespace-only) names and duplicate timestamps; the server accepts both.  Both stay rejected:
  anything `data_frame()` accepts must be readable back, the read path rejects duplicate timestamps, and
  sample status matching assumes one sample per instant.  A caller with genuinely duplicate timestamps can
  still hand-build the request and call the stub.  The docs say so.

- **T9 — What can be verified live.**  `querySamples` is **scalar-only**.  Array, image, struct, and
  serialized buckets are rejected with `"querySamples supports scalar PVs only ... use queryBuckets"`
  (`common/utility/TabularDataUtility.java:158-172`).  So the non-scalar builders can be verified live only
  as far as *ingestion* (ack + SUCCESS status) until #16 wraps `queryBuckets`.  A PV needs only buckets to be
  queryable by name list or pattern; metadata is needed only for the `metadata` selector
  (`query/handler/QueryV2Resolver.java:218-224`).

- **T10 — gRPC Python hides exceptions raised by a request iterator.**  Verified with grpcio 1.84.0 against
  an in-process server: a generator that raises `ValueError` mid-stream reaches the caller as
  `StatusCode.UNKNOWN, "Exception iterating requests!"`, for both `stream_unary` and `stream_stream`, with
  the original exception neither chained nor re-raised.  Our streaming wrappers build each request from
  caller params *inside* that iterator, so a bad params item would surface as a meaningless gRPC error.
  Combined with T4, requests before the bad item have already been ingested.

- **T11 — Version floor.**  `ingestion.proto` has not changed since rel-1.15.0, and the typed columns exist
  in rel-1.14.0.  `ColumnMetadata` (tags, attributes, provenance) on a column is 1.16.0.  So the client
  works against older servers, but provenance does not survive ingestion into one older than 1.16.0.
  The local stubs are the rel-1.16.0 sync, and dp-grpc has made no proto change since.

- **T12 — Existing code this retires or changes.**
  - `tests/integration/test_datasets_annotations_integration.py` and
    `test_query_helper_relaxations_integration.py` each carry a hand-written stub ingest and a poll loop.
    Both move onto the client and `await_request_statuses()`.
  - `test_query_client_integration.py::test_closed_loop_round_trip` is `@unittest.skip`ped pending this
    ticket; it gets implemented.
  - #17 is named as future work in: `data_frame.py` (module docstring and `data_frame()` docstring),
    `tests/unit/test_data_frame.py:387`, `README.md` (TODO section), `CLAUDE.md` (four places),
    `doc/cookbook/README.md`, `query.md`, `sample-status.md`, and `datasets-and-annotations.md` (two places).
    `doc/release-notes/rel-1.16.0.md` names it too, but is a shipped record and stays as is.
  - **Three existing unit tests hand-build columns that T7's new checks reject**, and need fixing rather
    than deleting.  In `tests/unit/test_data_frame.py`: `test_accepts_prebuilt_image_column` and
    `test_image_column_count_is_validated_against_the_axis` build an `ImageColumn` with no descriptor.  The
    first would start failing; the second would still raise, but on the descriptor instead of the count it
    means to test.  Both need a valid descriptor.  `test_serialized_column_still_needs_a_unique_name` builds
    a `SerializedDataColumn` with no `encoding`.  It passes only while the duplicate-name check runs first,
    so give it an encoding to keep it testing what its name says.  The hand-built array tests all set 1–2
    dims, and no test hand-builds an `EnumColumn`.  The `data_frame_conversions` tests that hand-build
    image/struct columns either never call `data_frame()` or already set descriptor and `schemaId`.

## Design decisions

- **D1 — Reuse `data_frame.py`; the ingest params are thin.**
  ```python
  IngestDataRequestParams(provider_id: str, frame: common_pb2.DataFrame, client_request_id: str | None = None)
  ```
  The frame comes from `data_frame()`, `data_frame_from_pandas()`, or `split_data_frame()`.  The params
  re-validate it through the same checks `data_frame()` applies (extracted as
  `data_frame.validate_data_frame()`), so a hand-built frame gets the same client-side errors.  There is no
  separate "ingestion column" model.  *Rejected:* a column-dict params API (`{"PV": [values]}`), which would
  fork the column model that calculations already use.

- **D2 — `client_request_id` defaults to a generated `uuid4` string, surfaced on the result.**  The server
  does not check uniqueness (T3), so a generated id is the safe default, and callers who want meaningful ids
  pass their own.  A blank id raises `ValueError` (the server accepts whitespace).  `split_data_frame()`
  callers get `<base>-<n>` naming from a small helper, so chunked requests correlate.

- **D3 — Result classes.**
  - `IngestDataApiResult`: `provider_id`, `client_request_id`, and on ack `num_rows` / `num_columns`.
    Rejects are `is_error`, with the server's message.  Uses `_dispatch`.
  - `IngestDataStreamApiResult`: `client_request_ids`, `rejected_request_ids`, and `num_requests`
    (`None` on reject, since the server omits it).  **The response is kept on the error result** (T4).
    So `_send_ingest_data_stream` is hand-written, not `_dispatch`, and says why in its docstring.
  - `QueryRequestStatusApiResult`: `request_statuses`, the list of `RequestStatus` messages.
  - `RegisterProviderApiResult` gains `provider_id` / `is_new_provider` properties (`None` on error).
    Every existing caller digs these out of `.response.registrationResult`.

- **D4 — Streaming API shapes.**
  - `ingest_data_stream(requests: Iterable[IngestDataRequestParams]) -> IngestDataStreamApiResult`.  The
    iterable is consumed lazily, so a generator over `split_data_frame()` never holds every request in
    memory.  A partial rejection returns an `is_error` result with `rejected_request_ids`.  It does not raise:
    the other requests *were* accepted, and raising would hide which.
  - `iter_ingest_data_bidi_stream(requests) -> Iterator[IngestDataApiResult]`.  One result per request, in
    order.  **A per-request reject is yielded as an `is_error` result, not raised.**  This deliberately
    differs from `iter_query_samples_stream()`, where an error ends the stream: here a reject is a fact about
    one request, and the server keeps going (T4).  A transport error raises `RuntimeError`, as in the query
    client.
  - *Rejected:* a threaded or async bidi API with separate send/receive handles.  grpcio already consumes
    the request iterator on its own thread, which covers the common case of a producer generator.  An
    interactive API can be added later without breaking this one.

- **D5 — Exceptions from the caller's request iterable are re-raised as themselves** (T10).  The streaming
  senders wrap the iterable in a generator that records any exception raised while pulling or building the
  next request.  When the RPC then fails, the wrapper re-raises the recorded exception (chained from the
  `RpcError`) instead of returning a gRPC error.  Its message says how many requests had already been sent,
  because those were ingested (T4).  Unit-tested against an in-process grpcio server, since a mocked stub
  would not reproduce grpcio's swallowing.
  - *Corrected in implementation (2026-09-30).*  Two details above were wrong.  (1) "Already sent" does not
    mean "ingested", or even "received": the in-process tests show grpcio's cancellation racing the sends, so
    the server holds some prefix of the requests handed over, possibly none.  The note therefore says "any that
    reached the server are ingested -- check request status" (T10's "have already been ingested" is corrected
    the same way).  (2) The count cannot go in the exception's *message* without changing its type or mutating
    its args, so it travels as a PEP 678 note (Python 3.11+) and is always logged.
  - *Added in implementation.*  Abandoning `iter_ingest_data_bidi_stream()` early cancels the call.  Without
    that, grpcio keeps pulling and sending the caller's requests on its own thread after the caller has walked
    away; an in-process test showed all 1,000 queued requests drained into the server.

- **D6 — `queryRequestStatus` gets a criterion helper and a poller.**
  - `RequestStatusQuery` (`RS`): `provider_id(id)`, `provider_name(name)`, `request_id(id)`,
    `status(statuses)`, `time_range(begin, end)`, each rejecting blank input, like the other helpers.  The
    server rejects the same inputs (T3), so this only saves a round trip and names the offending argument.
  - `query_request_status(criteria)` requires at least one criterion (T3).
  - `IngestionRequestStatus`, a Python `IntEnum` mirroring the proto values, so code compares names, not
    numbers.
  - `await_request_statuses(provider_id, client_request_ids, *, since, timeout=30.0, poll_interval=0.25)`
    polls until every id has a status document, then returns `{client_request_id: [RequestStatus, ...]}`.
    It is a list because ids can repeat (T3).  On timeout it raises `TimeoutError` naming the ids still
    missing.  It does not judge success: callers check for ERROR/REJECTED, and the cookbook shows how.
  - **Each poll is one query: `RS.provider_id(provider_id)` + `RS.time_range(since - skew, now)`**, with the
    ids matched client-side.  One id per `RequestIdCriterion` (T3) rules out a single id-based query, and
    one query per pending id per poll is rejected: a 500-chunk `split_data_frame()` ingest would cost 500
    RPCs per interval.  There is no per-id fallback.
  - **The time floor is required, for two reasons.**  It bounds the response while the method has no paging
    (T3); a provider-only query grows with the provider's whole history until it exceeds the client's 4 MB
    receive limit.  And it keeps stale documents out: with no unique index, a caller that reuses a fixed
    `client_request_id` across runs would otherwise have the poll satisfied at once by a previous run's
    document, possibly a SUCCESS for a request that has now failed.  Only documents inside the window count.
  - `since` is a `TimestampInput` the caller captures *before* sending the first request (the cookbook shows
    the pattern).  It is keyword-only and has no default: defaulting to "when the await started" would miss
    a fast request that finished before the call.  `createdAt` is the server's clock, so the floor is backed
    off by a fixed skew margin (`REQUEST_STATUS_CLOCK_SKEW`, 60 s, module constant).  A document from an
    earlier run inside that margin can still match a reused id; the docstring says to use unique ids (the
    D2 default) when that matters.
  - The window still grows with the provider's ingest rate × duration.  A `RESOURCE_EXHAUSTED` on the
    status query is reported as the receive limit (D9) with a pointer to narrowing `since`; the real fix
    is upstream paging (Out of scope).
  - The request-status docs lead with T2: an ack is not success.
  - *Rejected:* an `ingest_and_wait()` convenience.  It hides the async model the caller has to understand
    anyway, and composes trivially from the two calls.

- **D7 — Non-scalar column builders** (Q2; kept in #17).  All take `metadata=` like the scalar builders, and
  all route through `_check_column()`'s new rules (T7), so builder and hand-built paths agree.
  - Arrays: `double_array_column`, `float_array_column`, `int32_array_column`, `int64_array_column`,
    `bool_array_column(name, samples, dims=None)`.  `samples` is one entry per sample, each either a nested
    sequence (shape inferred and required to be the same for every sample) or anything with `.shape` and
    `.ravel()` (a NumPy array, duck-typed so NumPy stays optional).  Flattening is row-major (C order), which
    is what `data_frame_conversions` assumes when it slices per sample.  Explicit `dims` is required when a
    sample is already flat and meant as multidimensional.  1–3 dims, each > 0.
  - `image_column(name, images, width, height, channels, encoding)`: one descriptor per column, as the proto
    has it.
  - `struct_column(name, values, schema_id)`.
  - `serialized_column(name, payload, encoding)`.  Opaque; carries no sample count (T5).
  - The read side needs no change: `column_values()`, `column_dimensions()`, `image_descriptor_dict()`, and
    `column_schema_id()` already read all of these.  A unit round trip (build → `data_frame()` →
    `data_frame_columns()` + accessors) pins the symmetry.  Live read-back waits for #16 (T9).
  - *Not included:* `data_frame_from_pandas()` support for array/object columns.  That is a pandas
    representation question with no consumer yet.

- **D8 — `split_data_frame(frame, *, max_rows=None, max_bytes=None, max_span_nanos=None) -> Iterator[DataFrame]`**
  (Q4).
  - Splits on the time axis only; every chunk carries every column, with its metadata.
  - A `SamplingClock` chunk gets a new start time `start + offset * periodNanos`, computed in **integer
    nanoseconds**, never floats (the #6 lesson).  A `TimestampList` is sliced.
  - Columns are sliced per kind: `values` for scalars, `dataValues` for `DataColumn`, `images` for images,
    and `prod(dims)`-sized blocks for arrays.
  - A frame with a `SerializedDataColumn` is rejected, since its payload can't be divided.
  - At least one limit is required.  `max_bytes` bounds the **whole serialized `IngestDataRequest`**, not
    just the frame, so a chunk that fits the budget fits the message.  The envelope is small and knowable:
    the request is exactly `providerId`, `clientRequestId`, and the frame.  But the ids are caller strings
    of any length, and `split_data_frame()` does not see them.  So the envelope is budgeted for ids of up
    to `MAX_BUDGETED_ID_CHARS` (256) characters each, encoded as UTF-8 (≤ 4 bytes/char), plus the tags and
    length prefixes: the overhead is `ByteSize()` of an `IngestDataRequest` holding the chunk and two
    worst-case ids, which is computed once rather than hard-coded.  `IngestDataRequestParams` rejects an id
    longer than 256 characters, so the budget can't be exceeded by anything the params accept.  A single
    row larger than the budget raises, naming the row.
  - The module exports `SERVER_DEFAULT_MAX_MESSAGE_BYTES = 4_096_000` as a *documented reference*, not a
    default: it is the dp-service default for a configurable setting, so callers opt into it explicitly.
    This keeps #6's "don't duplicate deployment caps" rule; a chunker whose purpose is the cap has to let
    the caller name it.  The same goes for `max_span_nanos` and the 1-day bucket span.
  - Lazy: yields chunks, so a large frame is never copied whole.

- **D9 — Size errors point at the cap, and only size errors.**  `RESOURCE_EXHAUSTED` is not specific to
  message size; it also covers quota and other resource limits, and a "split your frame" hint there would
  send the caller after the wrong fix.  So the hint is gated on the status details identifying a size
  violation, matched case-insensitively on the known texts: the server's inbound rejection (grpc-java,
  "gRPC message exceeds maximum size") and the client's own receive limit (grpcio, "Received message
  larger than max").  The first gets a hint naming the server's inbound limit and `split_data_frame()`, on
  an ingest call; the second, on `query_request_status()`, names the receive limit and a narrower time
  range (D6).  Any other `RESOURCE_EXHAUSTED` keeps the plain message.  The hint is appended to the
  `"gRPC error: ..."` text, never substituted, since that text is part of the contract.  The matched
  strings live in one module constant with a comment naming their sources, since neither is a stable API.

- **D10 — Exposure stays `client.ingestion_client`.**  The ticket mentions `client.ingestion`.  An alias
  would make naming consistent with `client.query` / `client.annotation`, but two names for one object is
  its own confusion, and renaming is a cross-cutting API question, not this ticket's.

- **D11 — `data_column()` stays, marked legacy in the ingestion docs.**  The server accepts mixed-type
  `DataColumn`s silently (T5), and the type is deprecated upstream.  The ingestion recipe uses typed columns
  and says why.

## Implementation tasks

### PR A — client and unit tests (`Refs #17`)

**`src/dp_python_lib/client/data_frame.py`**
- Extract `validate_data_frame(frame)` from `data_frame()`: axis via `timestamp_count()`, at least one
  column, then `_check_column()` on every column across all 16 arms.  *(As implemented, `data_frame()` does
  not call it after assembly: both share one column-list check, which `data_frame()` runs on the caller's list
  before routing, so an unsupported type is caught before it must be routed and messages keep the caller's
  index.)*
- Extend `_check_column()` with T7's rules: enum `enumId` non-blank; array dims count 1–3; image
  descriptor present with positive width/height/channels and non-blank encoding; struct `schemaId`
  non-blank; serialized `encoding` non-blank.
- Add the D7 builders and `split_data_frame()` (D8), plus `SERVER_DEFAULT_MAX_MESSAGE_BYTES` and
  `MAX_BUDGETED_ID_CHARS`.
- Update the module docstring: the builders are no longer deferred; add the chunking rationale.

**`src/dp_python_lib/client/ingestion_client.py`**
- `IngestDataRequestParams` (D1, D2), `IngestDataApiResult`, `IngestDataStreamApiResult` (D3).
- `RequestStatusQuery`, `IngestionRequestStatus`, `QueryRequestStatusApiResult` (D6).
- `_build_ingest_data_request()`; `_send_ingest_data()` via `_dispatch` with a `success_log` reporting
  rows × columns; `ingest_data()`.
- `_send_ingest_data_stream()` (hand-written; keeps the response on error), `ingest_data_stream()`.
- `_send_ingest_data_bidi_stream()` (yields, like the query stream senders),
  `iter_ingest_data_bidi_stream()`.
- The request-iterator wrapper of D5, shared by both streaming senders.
- `_send_query_request_status()` via `_dispatch`, `query_request_status()`, `await_request_statuses()`
  (provider + time-range polling, client-side id matching, `REQUEST_STATUS_CLOCK_SKEW`; D6).
- `IngestDataRequestParams` rejects ids over `MAX_BUDGETED_ID_CHARS` (D8).
- The size-gated `RESOURCE_EXHAUSTED` hint (D9).  If it fits `_dispatch` cleanly as an optional hook, add
  it there; otherwise keep it local to the ingestion senders.
- `RegisterProviderApiResult.provider_id` / `.is_new_provider` (D3).  Document re-registration: a
  description is overwritten (to `""` if omitted), but empty tags/attributes do not clear stored ones.

**`src/dp_python_lib/client/__init__.py`** — export the new names.

**Tests**
- `tests/unit/test_ingestion_client.py`: request building (generated vs explicit id, blank id rejected,
  frame re-validated); three-tier errors on each unary call; stream result keeps `rejected_request_ids` on
  error with `num_requests is None`; bidi yields rejects without raising and raises on transport error;
  `RS` helpers and empty-criteria rejection; `await_request_statuses()` success, repeated ids, timeout
  naming the missing ids, one query per poll regardless of id count, the query carrying provider id and a
  time range floored at `since - skew`, and a document for a watched id but outside the window not
  satisfying the wait (mock the clock; no real sleeps); the `RESOURCE_EXHAUSTED` hint present for each size
  text and absent for an unrelated `RESOURCE_EXHAUSTED`; an over-long id rejected.
- `tests/unit/test_ingestion_streaming_grpc.py`: D5 against an in-process `grpc.server` on `localhost:0`,
  for both streaming shapes, asserting the original exception type and the sent-count message.
- `tests/unit/test_data_frame.py`: fix the three tests T12 names; then each new builder (shape inference, NumPy duck-typing behind an
  `[analysis]` skip, dims rules, descriptor rules); each new `_check_column()` rule on a *hand-built* column
  (the T7 point); `split_data_frame()` for both axis forms, integer-nanosecond start times at present-day
  epochs, every column kind, the byte budget holding for every chunk *as a full `IngestDataRequest` with
  256-character ids, including multibyte ones*, the oversize-row and serialized
  rejections, and chunk concatenation reproducing the original frame.
- `tests/unit/test_data_frame_conversions.py`: build → read round trip for each non-scalar builder.

**Docs in PR A**
- Docstrings carry T2–T5's server behaviors where a caller meets them.
- `CLAUDE.md`: a Key Files entry for the extended `ingestion_client.py`; update the `data_frame.py` entry
  (builders, `validate_data_frame()`, `split_data_frame()`); an "Ingestion API" section with the invariants
  that outlive the ticket (ack ≠ success, async invalid-providerId, the stream-result-keeps-response rule,
  D5, the SUCCESS-is-zero enum hazard, the partial-write-on-duplicate case, the message and span caps);
  note the two new hand-written senders alongside the query stream senders.
- `README.md`: move the ingestion items out of TODO (leaving `subscribeData()`).
- `doc/release-notes/NEXT.md`: a section for #17's client surface, including the new `data_frame()`
  rejections (T7) as a behavior change for anyone passing hand-built columns.

### PR B — live tests and docs that need real data (`Closes #17`)

- `tests/integration/test_ingestion_client_integration.py`: unary, stream (including one deliberately
  rejected request, asserting `rejected_request_ids`), and bidi ingest, each confirmed through
  `await_request_statuses()`; a bad `providerId` is acked and then ERROR (T2); a non-scalar frame (array,
  image, struct) reaches SUCCESS (T9); a frame chunked by `split_data_frame()` under a small `max_bytes`
  ingests as N requests, all SUCCESS.
- `test_query_client_integration.py::test_closed_loop_round_trip`: implement the shape already sketched in
  its comments: exact values and timestamps, half-open trimming at both bounds, dense alignment, and
  multi-page paging at a small `limit`.  Remove the skip and the "Test-data note".
- Move the stub-based ingest in `test_datasets_annotations_integration.py` and
  `test_query_helper_relaxations_integration.py` onto the client, synchronizing on
  `await_request_statuses()` instead of probe loops.
- Sample status: add the live filtering test `CLAUDE.md` says is missing (label ingested samples, then assert
  `exclude()` drops exactly those rows and `include()` keeps only them).
- Cookbook: a new `doc/cookbook/ingestion.md` recipe (register, build a frame, ingest, confirm; pandas;
  chunking + streaming; reading failures), in the worked example's PVs so the later recipes query data it
  created.  Add it to the cookbook index.  Re-verify `query.md` and `sample-status.md` against real data,
  correct any illustrative output that differs, and delete their "not verified" notes.  Replace the #17
  pointers in `datasets-and-annotations.md`.  Run the snippet checker, and parse the ingestion recipe as one
  continuous script (the #6 lesson on incoherent sequences).
- `CLAUDE.md`: drop the remaining "until #17" notes; record which verification now exists.
- `doc/release-notes/NEXT.md`: extend #17's section with the verification now done.

## Out of scope

- **`subscribeData()`** — implemented server-side, but a long-lived bidi subscription with its own
  lifecycle (one subscription per stream, publishes only new ingests, no history).  Stays on the README
  TODO; no ticket yet.
- **`subscribeDataEvent()`** (`DpIngestionStreamService`) — a separate service; same.
- **Reading back non-scalar columns live** — [#16](https://github.com/osprey-dcs/dp-python-lib/issues/16)
  (`queryBuckets`).  #16 should add a live round trip for each D7 builder when it lands.
- **pandas support for array/object columns** in `data_frame_from_pandas()` — no consumer yet (D7).
- **A `client.ingestion` alias** (D10).
- **Paging for `queryRequestStatus`** — [osprey-dcs/dp-grpc#165](https://github.com/osprey-dcs/dp-grpc/issues/165)
  (proto) and [osprey-dcs/dp-service#302](https://github.com/osprey-dcs/dp-service/issues/302) (handler).  D6 bounds its queries by time range in the meantime.  Once paging
  ships, `query_request_status()` gains `iter_`/token support like the other paged queries, and the D6
  window stays for the stale-id reason.
- **Working around dp-service's bugs.**  The client documents actual behavior; fixes belong upstream.  The
  T3 empty-criteria fall-through is in dp-service#302, and the proto comments T2–T4 contradict are in
  dp-grpc#165.  Still unfiled: the T4 shutdown double-complete and the unenforced
  `(providerId, clientRequestId)` uniqueness.

## Dependencies and sequencing

- PR A depends on nothing unmerged.  Stubs are current (T11).
- PR B depends on PR A, and on a live ecosystem for its own verification.  Its tests self-skip without one,
  as all integration tests do.
- #16 does not block this ticket, and this ticket does not block #16.  #16 gains the live non-scalar round
  trip (Out of scope).
- The rel-1.16.0 server behaviors cited here are unchanged on dp-service `main`.
- Upstream `queryRequestStatus` paging does **not** block this ticket: D6 works without it.  It is a
  follow-up here once it ships (Out of scope).

## Open questions

All resolved with Craig: Q1–Q4 on 2026-09-27, Q5 in plan review on 2026-09-28.

- **Q1 — Wrap `queryRequestStatus`?**  The ticket excluded it.  *Resolved: include it* (T2, D6).  Without
  it a caller cannot tell whether an acked request landed, and the live tests need it to synchronize.
- **Q2 — Array/image/struct/serialized builders: here or a new ticket?**  They can't be read back live
  until #16 (T9).  *Resolved: keep them in #17* (D7), with unit round trips now and live ingest-to-SUCCESS
  checks in PR B.
- **Q3 — One PR or two?**  The ticket asked for one.  *Resolved: two* (PR A client, PR B live tests + docs),
  the split #6 used.
- **Q4 — Chunking helper here or a follow-up?**  *Resolved: here* (T6, D8).
- **Q5 — How does `await_request_statuses()` cover several ids?**  Raised in plan review (2026-09-28):
  one id per criterion (T3) means either a query per id or a broader query filtered client-side.
  *Resolved: provider + time range only, no per-id fallback* (D6); the unbounded-response problem goes
  upstream as request-status paging.

Decisions taken without a question, flagged for plan review: D2 (generated request ids), D4 (bidi yields
rejects rather than raising), D8 (no default byte budget; the server default exported as a reference),
D10 (no alias), and T8 (the client stays stricter than the server on blank names and duplicate timestamps).
