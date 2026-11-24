# Issue #81 — Deadlines, stream idle timeouts, and keepalive

**Status:** triaged 2026-11-24.  The four design decisions in the issue's "To decide" list were resolved with
Craig the same day (D1, D3, D5, and the corrections in Background), as were both open questions (Q1, Q2).  Plan
review on PR #82 (2026-11-24) revised D2, D3, D5, T3, T4, T6, and T9: the bidi idle clock, the watchdog's threading,
`await_request_statuses()`'s existing `timeout`, per-call `timeout=0`, the keepalive floor, a data-heavy stream in
live verification, the deadline hint's wording, and the keepalive test's time limits (R1–R8 below).  Ready for
implementation once this plan merges.

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
`"gRPC error: Deadline Exceeded"`, never replacing it (the prefix is contract).  The hint names **the deadline
actually applied and where it came from** (R7), not a fixed "300 s", because a caller who hit the configured
default most likely wants to change the config, not every call:

- from config or the client default: `" (client deadline 300 s, set by MLDP_QUERY_CALL_TIMEOUT_SECONDS /
  query.call_timeout_seconds; pass timeout= to override one call, or timeout=None to disable)"`;
- from a per-call `timeout=`: `" (deadline 30 s, passed as timeout=)"`.

The source is described by whoever set the default, not by the subclass (amended in T2): a client built directly
never reads config, so naming `MLDP_QUERY_CALL_TIMEOUT_SECONDS` there would point at a setting with no effect.
`ServiceApiClientBase.__init__` takes `call_timeout_source=` / `stream_idle_timeout_source=` strings;
`MldpClient` passes the config names (T5), and `ExportClient` names `annotation_export_timeout_seconds` /
`MLDP_ANNOTATION_EXPORT_TIMEOUT_SECONDS`.  Without one, a client given `call_timeout=` names the constructor
argument, and a bare client says `"the library default; construct the client with call_timeout= to change it"`.  `_dispatch`'s own hint goes first, then any `rpc_error_hint`
text; the two never meet today (the ingestion size hint fires only on `RESOURCE_EXHAUSTED`), but the order is
fixed so a later hint on another code composes predictably.

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
- **Client-streaming ingest** (`_send_ingest_data_stream()`): switches to `stub.ingestDataStream.future(...)`.  The
  clock runs whenever grpcio, not the caller, holds the next step: from the feed handing grpcio a request until
  grpcio asks for the next one, and from the feed's exhaustion until the summary arrives.  Time inside the caller's
  iterable never counts.  T1 showed this covers an upload stall as well as a summary that never comes: grpcio
  pulls the next request only once the previous send completes, and a send does not complete while HTTP/2 flow
  control is closed, so a server that stops reading stops the pulls (T1 results below).
  The switch must keep the existing contracts: the caller's own exception still re-raised through
  `feed.reraise()` when `future.result()` raises, a partial reject still returned with its response, and
  `test_ingestion_streaming_grpc.py` passing unchanged.

On expiry the watchdog sets a flag and calls `call.cancel()`.  The sender, seeing `CANCELLED` with the flag set (on
the client-streaming future, `FutureCancelledError`; T1), reports `"gRPC error: stream idle timeout: no message
from the server within <n> s"` instead of grpcio's "Locally cancelled by application!", so the public wrappers
raise their existing `RuntimeError` with that text.  The watchdog is always disarmed in the sender's `finally`, so
a closed generator leaves no timer behind.

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

**Done 2026-11-24** (`cs_proto.py` in the ticket scratch area; grpcio 1.84, in-process grpcio server on localhost).
**Yes: the pulls stop**, so D3 takes the form that covers the upload.

| Server handler | Request size | Pulled before the stall | Then |
|---|---|---|---|
| never reads | 1,000 B | 4,174 (4.2 MB) | no pull for 8 s, until cancelled |
| never reads | 64 KiB | 64 (4.2 MB) | same |
| never reads | 1 MiB | 4 (4.2 MB) | same |
| reads 50, then stops | 64 KiB | 128 (8.4 MB) | same |
| sleeps 4 s, then reads all | 64 KiB | 64, then all 400 at 4.05 s | completes; a slow server is not a stall |
| reads promptly | 64 KiB | all 400 | completes in 0.26 s |

- The stall shows up after about 4 MB is buffered past what the server has read, whatever the message size.  That
  is this grpcio server's window.  dp-service sets no flow-control window (`GrpcServerBase` configures the message
  size and keepalive limits only), so grpc-java's Netty default applies, and T9 checks the stall against it.  An upload smaller than the window never stalls on
  the way up; it is caught by the clock that runs after exhaustion instead.
- **`future.cancel()` makes `future.result()` raise `grpc.FutureCancelledError`**, at once, not an `RpcError` with
  `CANCELLED` as on the streaming calls.  So `_send_ingest_data_stream()` checks the watchdog's `expired` flag on
  `FutureCancelledError` and turns it into the D3 idle-timeout message.  grpcio pulled no further requests after the
  cancel.

### T2. `service_api_client_base.py`

- `__init__(channel, stub_class, *, call_timeout=DEFAULT, stream_idle_timeout=DEFAULT, call_timeout_source=None,
  stream_idle_timeout_source=None)`; resolved into `ClientTimeout(seconds, source)` pairs, validated non-negative
  (`0` and `None` → disabled).  The `*_source` strings are the D2 amendment above.
- The sentinel is `DEFAULT` (an enum member, so mypy narrows on it, with repr `DEFAULT` for `help()`), not a
  private `_DEFAULT`: it appears in 45 public signatures.  `TimeoutArg = float | _Default | None`.
- `_resolve_call_timeout(timeout, default=None)` and `_resolve_stream_timeouts(timeout, idle_timeout)` (the second
  returns `(total, idle)`, total `None` when omitted, per D2).  Both raise `ValueError` on a per-call value `<= 0`
  (or NaN / infinite), naming `None` as the way to disable, and `TypeError` on a non-number or `bool`.
- `_dispatch(..., timeout=DEFAULT, default_timeout=None)`: sends no `timeout` keyword at all when disabled, so an
  opted-out call is exactly the pre-#81 call; appends the D2 hint on `DEADLINE_EXCEEDED`, before any
  `rpc_error_hint`.  `default_timeout` is how `ExportClient` substitutes its own default (Q1).
- `_IdleWatchdog` (one daemon thread per call waiting on a `Condition`; arm / disarm / `expired` / `stop`, always
  stopped in `finally`), here because three modules use it.  It also has `attach(target)`, because the
  client-streaming feed arms it before `.future()` returns the call (an expiry before attach cancels on attach),
  and it calls its target outside its lock, since a cancel can call back into `disarm()` / `stop()`.  The bidi
  two-condition state (D3) wraps it in `ingestion_client.py`, its only user.
- `DEFAULT_CALL_TIMEOUT_SECONDS` and `DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS`, exported from `dp_python_lib.client`.
- Because `_dispatch` now applies the default to every unary sender, the 15 stub-call assertions (T6) change in
  this step, not in T3.

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

**Amended during T3:**

- `_iter_stream()` moved from `QueryClient` to `ServiceApiClientBase`, and `_send_query_sample_statuses_stream()`
  now delegates to it.  That sender was a line-for-line copy (same tiers, same texts; its logs hard-coded the
  names `_iter_stream()` derives), and wiring the watchdog into two copies was the worse option.  CLAUDE.md's "leave
  them as they are" note is updated in T7.
- The three public server-stream wrappers close their sender with `contextlib.closing`, as the bidi ingest wrapper
  already did.  Without it, a wrapper that raises leaves the sender suspended until garbage collection (the
  traceback holds it), so its `finally` -- which stops the watchdog thread -- ran late.  The in-process test that
  counts watchdog threads found this.
- A stream's total `timeout=` gets the per-call D2 hint on `DEADLINE_EXCEEDED`, like a unary call's.
- The senders are annotated `Generator[...]`, not `Iterator[...]`, since `closing()` needs `close()`.
- `test_call_timeouts.py` checks all 40 unary and paging methods (not a sample, since the keyword went in
  mechanically) and the stream methods, and pins the count at 45; `test_timeouts_grpc.py` holds the in-process
  unary-deadline, server-stream, client-streaming and bidi idle tests T6 lists.
- Ingestion streams: `_RequestFeed` reports `pulling()` / `handed_over()` / `exhausted()` to an optional
  listener on grpcio's request thread.  `_UploadClock` (client-streaming) and `_BidiIdleClock` (bidi) are the two
  listeners.  The bidi clock keeps its start while it stays running, so a request handed over while the caller
  waits does not restart the bound for older unanswered requests.  A `FutureCancelledError` the watchdog did not
  cause becomes an "Unexpected error" result, like any other unexpected exception, rather than escaping.
- Mutation checks run while writing the tests: dropping one sender's `timeout=timeout` fails
  `test_call_timeouts.py`, and a bidi clock counting outstanding requests alone (the R1 rejected form) fails
  `test_bidi_slow_consumer_with_acks_pending_does_not_trip`.
- Cost: the in-process tests run in real time with a 0.5 s bound, and take the unit suite from about 3 s to about
  13 s.  Each test asserts it finished inside 5 s.

### T4. `await_request_statuses()` honors its own timeout

The method keeps its signature: its `timeout` stays the total wait (default 30 s), and it gains no per-call
`timeout` of the D2 kind.  Each poll's deadline is `min(client default, remaining)`, so the total bounds the polls
too.  If a poll fails *and* the overall deadline has passed,
raise the existing `TimeoutError` naming the missing ids, not `RuntimeError("status query failed")`.  Unit test
against the fake clock: a poll that would hang is bounded, and the error type is `TimeoutError`.

Amended during T4 (2026-11-24):

- **A floor for the last poll.**  The loop trims its final sleep to the deadline and then polls once more, so
  "the time remaining" is zero for exactly the poll most likely to see a just-written document.  Each poll gets at
  least `AWAIT_MIN_POLL_SECONDS` (1 s), capped at the call's own `timeout`; that is also the most the wait can
  overrun, and the docstring says so.
- **`DEFAULT` when the client default is the tighter bound.**  Passing it as a number would make a
  `DEADLINE_EXCEEDED` hint say "passed as timeout=", which the caller never did.  With the default disabled, the
  remaining time bounds the poll alone.
- **Which ids the `TimeoutError` names** after a failed poll: those the last successful poll lacked, or all of them
  if none succeeded.  The message ends with the failed query's own text.  A poll that fails *before* the deadline
  (for instance, bounded by a shorter client default) still raises `RuntimeError`, as before.
- Tests: seven cases in `TestAwaitRequestStatuses` (remaining time per poll, the client default, the default
  disabled, the floor and its cap, a hung poll, a late failure after a partial result).  Mutation checks: dropping
  the per-poll timeout fails five, and dropping the `TimeoutError` conversion fails two.

### T5. `config/config.py`, `config/loader.py`, `client/mldp_client.py`, `client/annotation_client.py`

The D4 fields and their validation; the YAML flattener's per-service key list (one tuple, so the next setting is a
one-line change); `ServiceConfig.create_channel()` with `options=channel_options(...)`; `channel_options()`
exported from `dp_python_lib.config`; `MldpClient` passing the per-service timeouts; the facade forwarding them.

**Amended during T5 (2026-11-24):**

- **Each client takes only the timeouts it uses.**  The three clients with streams (`IngestionClient`,
  `QueryClient`, `SampleStatusClient`) take `call_timeout` and `stream_idle_timeout`, each with its `*_source`; the
  four unary-only annotation clients take `call_timeout` alone; `ExportClient` takes only `export_timeout` (T3).
  A `stream_idle_timeout` on a client with no streams would be accepted and do nothing.  The facade takes all three
  and forwards each where it applies.
- **`keepalive_timeout_seconds` must be positive.**  D4's "`0` disables" does not fit a ping timeout, which has
  nothing to disable; keepalive itself is turned off with `keepalive_time_seconds: 0`.  So `0` is a validation
  error at config load rather than a confusing failure at channel creation, and `channel_options()` rejects it too.
- **The config defaults are literals** in `config.py`, not imports of `DEFAULT_CALL_TIMEOUT_SECONDS` and friends:
  importing `dp_python_lib.client` from `config` would import `MldpClient`, which imports `config`.
  `test_config.py` pins them equal to the client constants.
- **MldpClient names the setting even when it holds the default** ("set by MLDP_QUERY_CALL_TIMEOUT_SECONDS /
  query.call_timeout_seconds"), since that is where a caller of `MldpClient` would change it.  Without a config
  (explicit channels only), the clients' own library-default text applies.
- **The YAML flattener is in `config.py`** (`from_yaml()`), not `loader.py`, which needed no change.  It reads a
  `_SERVICE_KEYS` tuple plus `_SERVICE_ONLY_KEYS` for `annotation.export_timeout_seconds`.
- **Also exported** from `dp_python_lib.config`: `DEFAULT_KEEPALIVE_TIME_SECONDS`, `DEFAULT_KEEPALIVE_TIMEOUT_SECONDS`
  and `MIN_SERVER_KEEPALIVE_SECONDS`; `ServiceConfig` gains a `channel_options()` method that `create_channel()`
  uses.
- **Tests moved up from T6:** the four channel assertions (`test_config.py`, `test_mldp_client.py`) now expect
  `options=channel_options()`, and the facade and `MldpClient` wiring is pinned (`TestAnnotationClientTimeouts`,
  `TestMldpClientTimeouts`).  The config field, precedence, `channel_options()` and WARNING tests stay in T6.

### T6. Tests

- **Update** the 15 stub-call assertions to `assert_called_once_with(request, timeout=DEFAULT_CALL_TIMEOUT_SECONDS)`,
  which also pins the default's propagation, and the channel tests to expect `options=channel_options()`.
- **Rewrite** the client-streaming ingest tests in `test_ingestion_client.py` for `.future()`: `_consume_then()`
  and the `ingestDataStream.side_effect` mocks become a mocked `.future` whose `result()` returns the response or
  raises, keeping every current case (accepted, partial reject, `RpcError`, unexpected error, unrecognized
  response, the caller's exception re-raised).
- `test_service_api_client_base.py`: timeout forwarded / omitted for `None`; sentinel resolution; per-call
  `timeout=0` and negative raising `ValueError`; the `DEADLINE_EXCEEDED` hint appended, naming the applied deadline and its source in each of the three cases
  (config/default, constructor argument, `timeout=`), and ordered before an `rpc_error_hint`; config `0` disabling;
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
    **Both halves have explicit time limits** (R8).  The negative half watches for exactly as long as the
    positive half's limit (keepalive interval + ping timeout + a fixed margin), then cancels the call itself and
    asserts it was still open.  Waiting any longer proves nothing more, since keepalive would already have fired
    by then.  Each half's call also carries a total gRPC deadline well above that limit as a backstop, so a
    regression fails the test instead of hanging the suite (the project has no `pytest-timeout`, and this needs
    none).  The limits come from module constants, and the test asserts that the two
    halves together take less than a stated total, so a change that makes the test slow is noticed.

**Amended during T6 (2026-11-24):**

- **Most of this list landed with the code it tests**, in T2-T5: the stub-call assertions, the `.future()` rewrite
  of the client-streaming ingest tests, the `_dispatch` / `_IdleWatchdog` tests (thread growth counted by thread
  name rather than `threading.active_count()`, which also sees threads other tests leave winding down), the
  in-process deadline and idle tests in `test_timeouts_grpc.py`, and the channel assertions.  T6 adds the rest:
  `TestTimeoutAndKeepaliveSettings` and `TestChannelOptions` in `test_config.py`, and `test_keepalive_grpc.py`.
- **The keepalive test runs in the normal suite**, not a slow-marked quality-job test: grpc-core accepts 0.5 s /
  0.5 s, and detected the blackhole 0.70 s after it in every trial.  The positive half's limit is 2.0 s (interval +
  ping timeout + 1 s margin), the negative half watches exactly that long, each call carries a 10 s backstop
  deadline, and the test asserts the two halves finish inside 5.6 s.  It takes about 3.4 s.
- **The channel comes from `ServiceConfig.create_channel()`**, not `grpc.insecure_channel(options=channel_options())`,
  so the test also covers the config-to-channel wiring; the negative half is the same config with
  `keepalive_time_seconds=0`.
- **The blackhole proxy uses threads and plain sockets**, not the prototype's asyncio, so `close()` can shut every
  socket and join every thread and the test leaves nothing pending.
- `from_yaml()` re-raises a `ValidationError` as a `ValueError` naming the file, so the YAML rejection tests match
  the field name in that message; the env and init paths raise `ValidationError` itself.
- Mutation checks: dropping `grpc.http2.ping_timeout_ms` from `channel_options()` fails the keepalive test (and
  three option tests), and `allow_inf_nan=True` fails the non-finite rejections.

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
- a client-streaming ingest into a server that stops reading stalls the feed's pulls once grpc-java's flow-control
  window is full, as T1 showed against a grpcio server, and the idle timeout then cancels it.  dp-service has no
  "stop reading" switch, so this needs a test handler or a debugger pause; if neither is practical, the PR says the
  upload-stall half was verified only in-process.

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

Findings from reviewing this plan against the code, all accepted by Craig and folded into the sections named:

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

Two smaller findings, also accepted:

- **R7. The `DEADLINE_EXCEEDED` hint hardcoded "300 s"** (D2).  It now names the deadline applied and its source:
  the per-service config setting, the constructor argument, or `timeout=`.
- **R8. The keepalive test's negative half had no time limit** (T6).  Both halves now have explicit time limits,
  and the negative half watches no longer than the positive half's limit.

The review also corrected the call-site counts in Background (36 sites: 31 unary, 3 server-streaming) and added the
client-streaming ingest test rewrite that D3's `.future()` switch implies (Background, T6).
