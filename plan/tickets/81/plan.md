# Issue #81 — Deadlines, stream idle timeouts, and keepalive

**Status:** triaged 2026-11-24.  The four design decisions in the issue's "To decide" list were resolved with
Craig the same day (D1, D3, D5, and the corrections in Background), as were both open questions (Q1, Q2).  Plan
review on PR #82 (2026-11-24) revised D2, D3, D5, T3, T4, T6, and T9: the bidi idle clock, the watchdog's threading,
`await_request_statuses()`'s existing `timeout`, per-call `timeout=0`, the keepalive floor, and a data-heavy stream
in live verification (R1–R6 below).  Ready for implementation once this plan merges.

## Overview

No call made through dp-python-lib has a deadline, and the channels it builds have no keepalive, so an RPC the
server never answers waits forever.  A customer hit exactly this during the dp-service#314 outage.  This ticket
gives every call a bound by default:

- **Unary calls** get a total deadline: a generous library default, configurable per service, overridable per
  call with `timeout=`.
- **Streaming calls** get an **idle timeout** instead: the call is cancelled when the client has waited on the
  server for longer than a bound, which never caps a healthy long stream.
- **Channels built from `MldpConfig`** get HTTP/2 keepalive, tuned by measurement to detect a silently dropped
  network path, plus an exported `channel_options()` helper for callers (and libraries built on this one) that
  build their own channels.

It is for every user of the library, and for downstream libraries such as the customer's mldp-python-api, which
need the same values whether or not they use `MldpConfig`.

## Background / triage findings

Verified against `main` at `023031d`, dp-service `main` at `4ad0c1f`, and a grpcio 1.84.0 prototype: an
in-process server whose stream never answers, behind a TCP proxy that can drop the path silently (no FIN/RST).
The prototype, its outputs, and `GRPC_TRACE=http_keepalive,http2_ping` traces are in
`~/dp/dev/tickets/dp-python-lib/81/prototype/`.

**The ticket's premises hold:**

- All 36 stub call sites pass no `timeout`: 31 unary calls through `ServiceApiClientBase._dispatch()`, three
  server-streaming calls (`querySamplesStream` and `queryBucketsStream`, both through `_iter_stream()` in
  `query_client.py`, and `querySampleStatusesStream` in `_send_query_sample_statuses_stream()`), and the two
  client-streaming ingest calls.  (Corrected in review from "35 … 30 unary … four server-streaming".)  No public method accepts a
  timeout.  The only timeouts in `src/` are `await_request_statuses()`'s own polling loop.
- `ServiceConfig.create_channel()` builds `grpc.insecure_channel(target)` / `grpc.secure_channel(target, creds)`
  with no options.
- The remaining server-side hang is real.  After dp-service#318, a non-fatal `Throwable` escaping a job is logged
  and the worker continues, but "the job never dispatched, so its caller's stream gets no response"
  (`QueueHandlerBase.handleWorkerThrowable`).

**Corrections to the ticket (the issue body was updated 2026-11-24):**

- **Keepalive does not detect the failure the ticket is about.**  The ticket says keepalive "is what covers a
  streaming call that has gone quiet".  It does not: keepalive checks the *transport*, and a server that is
  connected but never answers keeps acknowledging pings.  In the prototype, a stream over a healthy connection
  with a 10 s keepalive was still open after 110 s.  That is the #314 outage shape (workers dead, transport
  healthy), and it is the non-fatal-error case above.  Keepalive detects only a dead network path.  A stream needs
  a client-side idle timeout (D3).  The customer had been given the same advice in dp-service#314; a correction
  was posted there on 2026-11-24.
- **grpcio's keepalive option names overstate what they control.**  Measured on a quiet server stream after the
  path was blackholed (the drop at 30 s unless noted):

  | client options | detected after the drop |
  |---|---|
  | none | never (cancelled at 110 s) |
  | `keepalive_time_ms=10000`, `keepalive_timeout_ms=5000` | 110 s, `UNAVAILABLE` "ping timeout" |
  | same + `http2.ping_timeout_ms=5000` | 55 s |
  | same + `http2.max_pings_without_data=0` (no `ping_timeout_ms`) | not within 65 s |
  | `keepalive_time_ms=10000` + `max_pings_without_data=0` + `ping_timeout_ms=5000` | 5 s |
  | **`keepalive_time_ms=60000`, `keepalive_timeout_ms=20000`, `max_pings_without_data=0`, `ping_timeout_ms=20000`** (D6) | **50 s** (drop at 150 s, so after the ping budget would have run out) |

  The traces show why: grpc-core armed a **60 s** ping-ack timer whatever `keepalive_timeout_ms` said, and once
  a quiet stream had spent its `max_pings_without_data` (default 2) it logged "Ping delayed: not enough time
  elapsed since last ping … minimum wait: 60000ms", throttling keepalive to one ping a minute.  The channel
  options therefore have to be chosen by measurement, and a test pins the result (D6, T6).  With both set
  (`ping_timeout_ms` for the ack wait, `max_pings_without_data=0` against the throttle), detection is bounded by
  one keepalive interval plus the ping timeout, as the last two rows show.
- **The #314 report does not say the customer "waited with no reply until we killed them".**  Its body says those
  clients "got `UNAVAILABLE` after about 2 minutes (their deadline)", and the quoted phrase is not in the thread.
  It does not change the design: callers without a deadline wait forever, as the prototype shows.

**Missing from the ticket:**

- **The server's ping policy.**  dp-service accepts a client ping every 30 s at most, with or without active calls
  (`GrpcServer.permitKeepAliveTimeSeconds: 30`, `permitKeepAliveWithoutCalls: true`, `GrpcServerBase` since
  dp-service#146, 2025-07; data-platform's `config/dp.yml` ships the same values).  grpc-java answers pings faster
  than that with GOAWAY `too_many_pings`.  A 60 s client interval leaves margin for a deployment that raises the
  limit.  The enforcement itself was not exercised (no live stack during triage); T9 does.
- **`exportData` replies only after the export file is written** (`ExportDataDispatcher` sends success once the
  job finishes), so a large export can legitimately exceed any unary default sized for queries.  See Q1.
- **`await_request_statuses(timeout=...)` does not bound its own polls.**  It checks the clock only between polls,
  so one hung `queryRequestStatus` blocks it forever, whatever `timeout` the caller passed (T4).
- **A lazy stream's consumer time is not server time.**  The public stream methods are generators: between two
  messages, the time may be the caller processing a page, or (for bidi ingest) the caller's request iterable
  producing data.  An idle timer that runs during either would cancel a healthy call.  D3 measures only time spent
  waiting on the server.
- **Blast radius in the tests:** 15 `stub.<method>.assert_called_once_with(request)` assertions (one in
  `test_service_api_client_base.py`, the rest across the feature-client tests) fail once `timeout=` is passed,
  and the channel tests in `test_config.py` / `test_mldp_client.py` assert `insecure_channel(target)` with no
  options.  The client-streaming ingest tests in `test_ingestion_client.py` (`_consume_then()` and the
  `ingestDataStream.side_effect` mocks) assume a blocking call, so D3's switch to `.future()` rewrites them (T6).
- **Wider surface than the client methods:** `config.py`'s YAML flattener hardcodes the `host` / `port` /
  `use_tls` keys, so new settings need adding there; channels passed to `MldpClient` explicitly get no keepalive;
  the whole-query conveniences (`query_conversions.query_samples_to_dataframe()` /
  `stream_query_samples_to_dataframes()`, `bucket_conversions.query_buckets_to_dataframes()`) call the client on
  the caller's behalf; and the cookbook says nothing about deadlines.
- `slaclab/mldp-python-api` is not visible to `gh` from this account, so whether it uses `MldpConfig` channels or
  builds its own is unknown.  `channel_options()` (D5) covers both.

## Design decisions

### D1. Unary calls get a default deadline: 300 s, per service, overridable per call

`DEFAULT_CALL_TIMEOUT_SECONDS = 300.0`.  It is a hang guard, not a performance budget: long enough that no
healthy unary call in the current API should approach it (a `querySamples` page is bounded by the server's byte
budget), short enough that a hung call fails within minutes.  Configurable per service (D4), and overridable on
each call with `timeout=` (D2).  `exportData` is the exception: its own 3600 s default (Q1).

*Rejected:* opt-in only (today's behavior for everyone who does not read the docs, which is the bug);
a short default such as 30 s (would fail legitimate slow calls on a loaded server, turning a hang guard into a
new source of errors).

### D2. Per-call override is a keyword on every public RPC method, with a sentinel default

Every public method that makes an RPC gains a keyword-only `timeout` parameter: 45 methods across eight clients
(listed in T3).  `await_request_statuses()` is the exception: it already has a `timeout` (its total wait, default
30 s), which keeps that meaning, and it bounds each poll itself (T4).  The default is a module sentinel, so three
cases stay distinct:

- omitted: the client's configured default (D1/D4);
- a positive number: that many seconds;
- `None`: no deadline, for a caller who deliberately wants today's behavior.

A per-call `timeout <= 0` raises `ValueError` naming `None` as the way to disable (R4).  grpcio treats `0` as a
deadline already passed, while `0` in config means "disabled" (D4); accepting it per call would leave one of those
readings silently wrong.

**Omitting `timeout` means different things on unary and stream methods, deliberately.**  On a unary or paging
method it is the configured deadline; on a stream method it is *no* total deadline, because the stream's default
bound is its idle timeout (D3) and a total deadline would cap a healthy long stream.  The stream docstrings and the
cookbook say so in those words.

On paging methods (`iter_*`, `get_datasets()`) it applies **per RPC** (per page or chunk), not to the whole
iteration: a total over an unbounded iteration is the same wrong tool as a total on a stream.  The docstrings say
so.  Stream methods take `idle_timeout` with the same three cases (D3), and also accept `timeout` as an optional
total deadline, defaulting to none.

`_dispatch()` gains a `timeout` parameter and passes it to the stub call only when it is not `None`
(`stub_call(request, timeout=t)`).  A `DEADLINE_EXCEEDED` result appends a hint to the existing
`"gRPC error: Deadline Exceeded"`, never replacing it (the prefix is contract): " (client deadline 300 s; pass
timeout= to change it, or timeout=None to disable)".

*Rejected:* a context manager or context variable (`with client.deadline(30):`) — a much smaller diff, but
invisible in signatures and IDE completion, and easy to leak across threads; a per-client setter only — cannot
give one slow call more time without affecting concurrent callers.

### D3. Streams get an idle timeout, measured only while the client is waiting on the server

`DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 300.0` (Q2).  A private `_IdleWatchdog` cancels the call if the client
spends longer than the bound **waiting on the server**.

**One watchdog thread per call, not a timer per wait** (R2).  Re-arming a `threading.Timer` around each wait would
start a thread per message: thousands for a bucket stream of small messages or a bidi ingest of many chunks.
Instead the watchdog starts one daemon thread when the call starts.  That thread waits on a `threading.Condition`
for a deadline: `arm()` sets the deadline to now + bound, `disarm()` clears it, both notify, and the thread cancels
the call when a set deadline passes.  `stop()` in the sender's `finally` ends the thread.  So the cost is one thread
per call however long the stream runs, and the interface stays arm / disarm / `expired`.

- **Server streaming** (`_iter_stream()`, `_send_query_sample_statuses_stream()`): armed immediately before each
  `next(stream)` and disarmed when it returns.  Time the caller spends processing a yielded page does not count,
  so the first message (which includes server queueing; #314 saw 22–42 s of queue wait) and each later one each
  get the full bound.
- **Bidi ingest** (`_send_ingest_data_bidi_stream()`): the clock runs only while **both** conditions hold (R1):
  the caller is blocked in `next(call)`, *and* requests are outstanding (the feed's `sent` count exceeds the
  responses received).  Neither condition alone is enough.  Outstanding requests alone would count consumer time:
  with 10 requests sent and one ack processed for 400 s, the other nine acks may already be buffered client-side,
  yet outstanding stays at nine.  Being in `next()` alone would count the producer's time: a caller waiting while
  its own iterable builds the next request has nothing to wait for from the server.  The two conditions change on
  different threads (the feed on grpcio's request thread, `next()` on the caller's), so a small state object holds
  `waiting` and `outstanding` under one lock, and re-evaluates arm/disarm on every transition: entering and leaving
  `next()`, the feed handing over a request, and a response arriving.
- **Client-streaming ingest** (`_send_ingest_data_stream()`): switches to `stub.ingestDataStream.future(...)`, and
  arms the watchdog from the moment the feed is exhausted until the summary arrives.  Whether grpcio's request
  consumption also exposes a flow-control stall during upload (the server not reading) is to be prototyped in T1;
  if it does not, an upload stall is left to keepalive and to an explicit `timeout`, and the docstring says so.
  The switch must keep the existing contracts: the caller's own exception still re-raised through
  `feed.reraise()` when `future.result()` raises, a partial reject still returned with its response, and
  `test_ingestion_streaming_grpc.py` passing unchanged.

On expiry the watchdog sets a flag and calls `call.cancel()`.  The sender, seeing `CANCELLED` with the flag set,
reports `"gRPC error: stream idle timeout: no message from the server within <n> s"` instead of grpcio's "Locally
cancelled by application!", so the public wrappers raise their existing `RuntimeError` with that text.  The
watchdog is always disarmed in the sender's `finally`, so a closed generator leaves no timer behind.

The prototype's watchdog already shows `cancel()` from another thread ends a blocked stream iteration at once,
with `CANCELLED`.

*Rejected:* a total deadline on streams by default (the ticket itself calls it the wrong tool: a healthy stream
can run arbitrarily long); keepalive alone (cannot see a hung server, Background).

### D4. Settings are per service in `MldpConfig`, through the normal precedence; `0` disables

New flat fields, following the #19 precedence (init kwargs > `MLDP_*` > YAML > defaults):

| field (per `ingestion` / `query` / `annotation`) | default | env example |
|---|---|---|
| `<svc>_call_timeout_seconds: float` | 300 | `MLDP_QUERY_CALL_TIMEOUT_SECONDS` |
| `<svc>_stream_idle_timeout_seconds: float` | 300 | `MLDP_QUERY_STREAM_IDLE_TIMEOUT_SECONDS` |
| `<svc>_keepalive_time_seconds: float` | 60 | `MLDP_QUERY_KEEPALIVE_TIME_SECONDS` |
| `<svc>_keepalive_timeout_seconds: float` | 20 | `MLDP_QUERY_KEEPALIVE_TIMEOUT_SECONDS` |
| `annotation_export_timeout_seconds: float` (annotation only, Q1) | 3600 | `MLDP_ANNOTATION_EXPORT_TIMEOUT_SECONDS` |

`0` disables (rather than `None`, which cannot be spelled the same way in an env variable and in YAML); negative
values are a validation error.  The YAML keys drop the service prefix (`query: {call_timeout_seconds: 600}`), like
the existing ones.  `ServiceConfig` grows the same four fields, and its `create_channel()` passes
`channel_options(...)` (D5).

The service clients take `call_timeout` and `stream_idle_timeout` keyword arguments
(`ServiceApiClientBase.__init__`, defaulting to the library constants), and `MldpClient` passes the configured
values; the `AnnotationClient` facade forwards them to all six feature clients.  So a client built directly
(`QueryClient(channel)`) or through `MldpClient` with explicit channels and no config gets the library defaults,
and deadlines apply to explicit channels too.  Keepalive cannot: it is a property of a channel the library did not
build, which is what `channel_options()` is for.

*Rejected:* one global setting (ingestion and query workloads differ, and a per-service field costs nothing in
this flattened schema); a separate config section for timeouts (breaks the established per-service shape).

### D5. Keepalive is on by default for `MldpConfig`-built channels, with an exported `channel_options()`

`dp_python_lib.config.channel_options(*, keepalive_time_seconds=60, keepalive_timeout_seconds=20) ->
list[tuple[str, int]]` returns the grpcio options D6 settles on, or `[]` when `keepalive_time_seconds` is 0.
`ServiceConfig.create_channel()` uses it, and it is exported so a caller building their own channel writes:

```python
channel = grpc.secure_channel(target, credentials, options=channel_options())
```

`keepalive_permit_without_calls` stays off: dp-service would allow it, but a channel with no call in flight has
nothing to protect, and pinging idle connections only adds load.

**A keepalive interval below the server's limit is worse than none** (R5).  If `keepalive_time_seconds` is under
dp-service's 30 s `permitKeepAliveTimeSeconds`, the server answers with GOAWAY `too_many_pings`, which fails every
call on that connection: a guard against hangs becomes an outage.  So `channel_options()` (and through it
`ServiceConfig`) logs a WARNING for a non-zero value below `MIN_SERVER_KEEPALIVE_SECONDS = 30`, naming the
dp-service setting.  It warns rather than rejects, because a deployment can lower the server's limit and the
client cannot know.  The cookbook states the floor next to the defaults.

*Rejected:* opt-in (the silent network drop is as much a hang as the server one, and the cost is one ping a
minute per active connection); documenting raw grpcio option names for callers to copy (they are exactly the
names that turned out not to mean what they say).

### D6. Keepalive values: chosen by measurement, pinned by a test

Measured in the Background table: `grpc.keepalive_time_ms = 60000`,
`grpc.keepalive_timeout_ms = 20000`, `grpc.http2.ping_timeout_ms = 20000` (the knob that actually bounded the
ack wait), and `grpc.http2.max_pings_without_data = 0` (otherwise a quiet stream is throttled to one ping a
minute).  60 s is twice dp-service's 30 s minimum; 20 s matches the timeout dp-service uses for its own client and
server keepalive.  `keepalive_timeout_ms` is set to the same value although grpc-core 1.84 did not use it for
the ack wait, so a grpcio that does honors the same bound.  Detection of a dropped path is bounded by
60 s + 20 s = 80 s after the drop; measured at 50 s with the drop landing mid-interval.  The cookbook states the
80 s bound, and T6 pins the mechanism with small values so the unit suite stays fast.

## Implementation tasks

### T1. Prototype the client-streaming idle case (before the rest)

Extend the prototype: does grpcio's request-iterator thread stop pulling while the server is not reading (HTTP/2
flow control), so that "time since the feed last handed grpcio a request, excluding time inside the caller's
iterable" measures an upload stall?  Record the answer in D3 and choose between the two forms there.

### T2. `service_api_client_base.py`

- `__init__(channel, stub_class, *, call_timeout=DEFAULT_CALL_TIMEOUT_SECONDS,
  stream_idle_timeout=DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS)`; store both, validate non-negative (`0` → `None`).
- The `_DEFAULT` sentinel and `_resolve_timeout(timeout)` (sentinel → client default).
- `_dispatch(..., timeout=...)`: forward when not `None`; append the D2 hint on `DEADLINE_EXCEEDED`.
- A per-call `timeout <= 0` raises `ValueError` (D2); `None` passes through as "no deadline".
- `_IdleWatchdog` (one daemon thread per call waiting on a `Condition`; arm / disarm / `expired` / `stop`, always
  stopped in `finally`), here because three modules use it.  The bidi two-condition state (D3) wraps it in
  `ingestion_client.py`, its only user.
- The constants, exported from `dp_python_lib.client`.

### T3. The 45 public RPC methods and their senders

Add `timeout=` (unary, paging) or `idle_timeout=` + `timeout=` (streams), and thread it to `_dispatch()` /
the stream senders:

- `PvMetadataClient`: `save_pv_metadata`, `get_pv_metadata`, `query_pv_metadata`, `iter_pv_metadata`,
  `delete_pv_metadata`
- `MachineConfigClient`: `save_configuration`, `get_configuration`, `query_configurations`, `iter_configurations`,
  `delete_configuration`, `save_configuration_activation`, `get_configuration_activation`,
  `query_configuration_activations`, `iter_configuration_activations`, `delete_configuration_activation`,
  `get_active_configurations`
- `SampleStatusClient`: `save_sample_statuses`, `query_sample_statuses`, `iter_sample_statuses`,
  `iter_sample_statuses_stream`, `delete_sample_statuses`
- `DataSetClient`: `save_dataset`, `get_dataset`, `query_datasets`, `iter_datasets`, `get_datasets`,
  `delete_dataset`
- `AnnotationsClient`: `save_annotation`, `get_annotation`, `query_annotations`, `iter_annotations`,
  `delete_annotation`, `get_calculations`
- `ExportClient`: `export_data` (defaults to the 3600 s export timeout, Q1; `ExportClient` takes an
  `export_timeout` keyword that the facade forwards)
- `QueryClient`: `query_samples`, `iter_query_samples`, `iter_query_samples_stream`, `query_buckets`,
  `iter_query_buckets`, `iter_query_buckets_stream`
- `IngestionClient`: `register_provider`, `ingest_data`, `ingest_data_stream`, `iter_ingest_data_bidi_stream`,
  `query_request_status`.  Not `await_request_statuses()`: its existing `timeout` stays the total wait (D2, T4).

The stream senders get the D3 watchdog.  `_iter_stream()` keeps its yield-errors contract and error texts; only
the idle case adds a new text.

### T4. `await_request_statuses()` honors its own timeout

The method keeps its signature: its `timeout` stays the total wait (default 30 s), and it gains no per-call
`timeout` of the D2 kind.  Each poll's deadline is `min(client default, remaining)`, so the total bounds the polls
too.  If a poll fails *and* the overall deadline has passed,
raise the existing `TimeoutError` naming the missing ids, not `RuntimeError("status query failed")`.  Unit test
against the fake clock: a poll that would hang is bounded, and the error type is `TimeoutError`.

### T5. `config/config.py`, `config/loader.py`, `client/mldp_client.py`, `client/annotation_client.py`

The D4 fields and their validation; the YAML flattener's per-service key list (one tuple, so the next setting is a
one-line change); `ServiceConfig.create_channel()` with `options=channel_options(...)`; `channel_options()`
exported from `dp_python_lib.config`; `MldpClient` passing the per-service timeouts; the facade forwarding them.

### T6. Tests

- **Update** the 15 stub-call assertions to `assert_called_once_with(request, timeout=DEFAULT_CALL_TIMEOUT_SECONDS)`,
  which also pins the default's propagation, and the channel tests to expect `options=channel_options()`.
- **Rewrite** the client-streaming ingest tests in `test_ingestion_client.py` for `.future()`: `_consume_then()`
  and the `ingestDataStream.side_effect` mocks become a mocked `.future` whose `result()` returns the response or
  raises, keeping every current case (accepted, partial reject, `RpcError`, unexpected error, unrecognized
  response, the caller's exception re-raised).
- `test_service_api_client_base.py`: timeout forwarded / omitted for `None`; sentinel resolution; per-call
  `timeout=0` and negative raising `ValueError`; the `DEADLINE_EXCEEDED` hint appended; config `0` disabling;
  negative config rejected.  `_IdleWatchdog`: expiry cancels and sets `expired`; disarm before expiry does not;
  many arm/disarm cycles run on one thread (assert `threading.active_count()` does not grow with the cycles);
  `stop()` ends the thread.
- `test_config.py`: each field through init / env / YAML precedence (with `isolate_mldp_env`), `0` disables,
  negative rejected, `channel_options()` contents and the `[]` case, and the WARNING for a non-zero keepalive below
  30 s (`assertLogs`), with none at 30 s or 0.
- **In-process grpcio tests** (alongside `test_ingestion_streaming_grpc.py`), with second-scale values so the suite
  stays fast:
  - a unary call to a hanging handler fails with `DEADLINE_EXCEEDED` and the hint;
  - a server stream that never sends fails with the idle-timeout `RuntimeError` within the bound, and one that
    sends slowly but within the bound completes, with the consumer sleeping longer than the bound between
    messages (the consumer-time exclusion);
  - bidi ingest with a slow producer does not trip; a **slow consumer with acks pending** (several requests sent,
    the caller sleeping past the bound after the first ack) does not trip either; and an unanswered request does;
  - the client-streaming case as T1 settles it;
  - **keepalive**: through the blackhole proxy, a channel built with `channel_options()` at small values fails a
    quiet stream with `UNAVAILABLE` within a bound, and the same channel without options does not.  If grpc-core
    refuses second-scale keepalive values, mark the test slow and run it in the quality job only.

### T7. Docs

- `doc/cookbook/conventions.md`: a "Deadlines, idle timeouts, and keepalive" section: the defaults, the three
  meanings of `timeout`, per-page semantics, why streams use an idle timeout, that keepalive detects only a dead
  network path, the measured detection time, and `channel_options()` for own channels.
- `doc/cookbook/query.md` / `ingestion.md`: one line each where long streams and exports are discussed, pointing
  at conventions.
- `doc/release-notes/NEXT.md`: an entry led by the silent behavior changes for upgraders: unary calls now fail
  after 300 s (exports after 3600 s), streams after 300 s waiting on the server, and channels now send keepalive pings; how to restore
  the old behavior (`0` in config, `timeout=None`).
- `CLAUDE.md`: the invariants (consumer time excluded from the idle clock, keepalive vs hung server, grpcio's
  misleading option names, per-page timeout semantics, `0` disables) in gRPC Error Handling and the configuration
  section; the Key Files entries for the changed modules.

### T8. Whole-query conveniences

`query_samples_to_dataframe()`, `stream_query_samples_to_dataframes()`, and `query_buckets_to_dataframes()` pass
`timeout` / `idle_timeout` through to the client methods they call.

### T9. Live verification (recorded in the PR, not a CI test)

Against a local dp-service stack, with `GRPC_TRACE=http_keepalive,http2_ping`:

- a 60 s keepalive on a long-quiet stream draws no GOAWAY, and a 10 s one does draw `too_many_pings`, which
  confirms the enforcement the 60 s value is sized against;
- a **data-heavy** stream (a bucket stream over enough ingested data to run several minutes) with the D6 options
  draws no GOAWAY either (R6).  `max_pings_without_data=0` changes when pings go out while data flows, and how
  grpc-core's other pings (BDP probes) count against grpc-java's ping limit is not settled by the quiet case.
  If it does draw GOAWAY, D6 is revisited before the implementation PR merges.

Run the changed cookbook recipes as one script.

## Out of scope

- The service-side fixes: dp-service#314 (PRs #318 and #323).
- Retries and a gRPC service config: a retry on `DEADLINE_EXCEEDED` is not safe for the non-idempotent saves and
  ingests, and wants its own design.
- Whether dp-service stops work when a client's deadline expires.  The #314 follow-up made the streams check
  cancellation; the unary handlers are dp-service's to audit.
- Warning on unknown YAML keys (today they are silently ignored, so a misspelled `call_timeout_second` does
  nothing).  Worth a separate ticket.
- An async client.

## Dependencies and sequencing

- T1 comes first; it only decides the client-streaming form of D3, so the rest does not wait on it.
- No dp-grpc or stub dependency: everything is client-side grpcio.
- One implementation PR, `Closes #81`, after this plan PR (which `Refs #81`) merges.
- T9 needs a local dp-service stack; it does not block the merge of anything except the claim of live
  verification, which must not be made without it.

## Open questions

### Q1. What bound does `exportData` get?

It replies only after writing the whole file, so its legitimate duration grows with the export.
Options: (a) no default deadline for `export_data()` (callers pass `timeout=`; a hung export then still hangs);
(b) its own longer default, e.g. 3600 s, as a separate `annotation_export_timeout_seconds` field;
(c) the same 300 s default, documented.
**Recommendation: (b).**  It keeps the "every call is bounded by default" property while not failing large
exports, and a separate field means raising it does not loosen every other annotation call.

**Resolved 2026-11-24 (Craig): (b)**, 3600 s, as `annotation_export_timeout_seconds` (D4).

### Q2. Default stream idle timeout: 300 s, or shorter?

The bound covers server queueing before the first message (22–42 s observed under the #314 load) and the
server's work per message.  Shorter (e.g. 120 s) surfaces a hung stream sooner; 300 s matches the unary default,
so there is one number to explain.
**Recommendation: 300 s**, matching D1, revisited if field reports show hung streams lingering.

**Resolved 2026-11-24 (Craig): 300 s.**

## Plan review (PR #82, 2026-11-24)

Six findings from reviewing this plan against the code, all accepted by Craig and folded into the sections named:

- **R1. The bidi idle clock counted consumer time** (D3).  "Armed while requests are outstanding" cancels a healthy
  call whose caller is slow to process acks that have already arrived.  The clock now runs only while the caller is
  blocked in `next()` *and* requests are outstanding.  T6 adds the slow-consumer case.
- **R2. A `threading.Timer` per wait starts a thread per message** (D3, T2).  Replaced by one watchdog thread per
  call waiting on a `Condition`.
- **R3. `await_request_statuses()` already has a `timeout`** (D2, T3, T4), meaning the total wait.  It keeps that
  meaning and is not one of the D2 methods, so they number 45, not 46.
- **R4. `timeout=0` per call was unspecified, and omitting `timeout` differs between unary and stream methods**
  (D2).  A per-call `timeout <= 0` now raises `ValueError`; the unary/stream difference is stated as deliberate.
- **R5. A keepalive interval below the server's 30 s limit draws GOAWAY `too_many_pings`** (D5).  It now logs a
  WARNING, not an error, since a deployment can lower the server's limit.
- **R6. Live verification checked only a quiet stream** (T9).  It adds a data-heavy bucket stream under
  `GRPC_TRACE`.

The review also corrected the call-site counts in Background (36 sites: 31 unary, 3 server-streaming) and added the
client-streaming ingest test rewrite that D3's `.future()` switch implies (Background, T6).
