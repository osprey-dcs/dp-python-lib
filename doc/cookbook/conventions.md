# API Conventions

Patterns that recur throughout `dp_python_lib`.  Recipes in this cookbook link here rather than
repeating them.

> This package's version tracks the dp-grpc version its stubs were generated from, so a server
> older than your `dp_python_lib` will not implement everything documented here.  Where a recipe
> uses something added in a particular release, it says so.

For the wire-level view of these same conventions — the protobuf messages and the `oneof result`
pattern this library wraps — see the
[dp-grpc cookbook](https://github.com/osprey-dcs/dp-grpc/blob/main/doc/cookbook/conventions.md).

### Imports used by the examples

```python
from dp_python_lib.client import (
    MldpClient,
    SavePvMetadataRequestParams,
    PvMetadataQuery as Q,
    to_timestamp,
)
```

## Contents

- [Checking results](#checking-results) — the one pattern every call shares
- [Empty is not an error](#empty-is-not-an-error)
- [Paging](#paging) — and why `iter_*` is usually the right choice
- [Query criteria](#query-criteria) — AND/OR rules
- [Save semantics: full replace](#save-semantics-full-replace)
- [Time](#time) — accepted inputs, and why naive datetimes are rejected
- [Deadlines, idle timeouts, and keepalive](#deadlines-idle-timeouts-and-keepalive) — how long a call may wait
- [Optional dependencies](#optional-dependencies)

## Checking results

Every client method returns a result object rather than raising.  Each one carries a
`result_status` with an error flag and a message:

```python
# cookbook:partial
result = client.annotation.pv_metadata.get_pv_metadata("BPMS:GUNB:314:X")

if result.result_status.is_error:
    print(f"lookup failed: {result.result_status.message}")
else:
    metadata = result.pv_metadata
    if metadata is not None:
        print(metadata.pvName)
```

The accessors are typed `Optional`, so a type checker will require the `None` check even inside
the success branch — it cannot infer that `is_error == False` implies a payload.  Recipes in this
cookbook keep the check for that reason.

**Three different failures collapse into `is_error`.**  The client catches all of them and
reports them the same way:

1. Network and connection failures (`grpc.RpcError`)
2. Business-logic errors returned by the server (the `exceptionalResult` field)
3. Unexpected client-side errors

`result_status.message` distinguishes them in prose — gRPC failures are prefixed `gRPC error:`
and unexpected ones `Unexpected error:` — but there is no separate status code to branch on.  If
your caller needs to retry only on transport failures, inspect the message text.

### Typed accessors return None or empty on error

Each result class exposes accessors for its payload — `get_pv_metadata()` gives `.pv_metadata`,
`query_pv_metadata()` gives `.pv_metadata_list`, `save_configuration_activation()` gives
`.client_activation_id`, and so on.

These are **safe to read after a failure, and that is exactly what makes them dangerous**:
single-value accessors return `None` and list accessors return `[]`.  A loop that skips the check
does nothing at all on failure, and looks identical to a successful query that matched nothing:

```python
# cookbook:partial
# WRONG -- a connection failure silently prints nothing
page = client.annotation.pv_metadata.query_pv_metadata([Q.attributes("AREA", ["GUNB"])])
for record in page.pv_metadata_list:
    print(record.pvName)

# RIGHT -- distinguish failure from no matches
page = client.annotation.pv_metadata.query_pv_metadata([Q.attributes("AREA", ["GUNB"])])
if page.result_status.is_error:
    raise RuntimeError(page.result_status.message)
for record in page.pv_metadata_list:
    print(record.pvName)
```

Check `is_error` before reading the payload.  Every time.

## Empty is not an error

A query that matches nothing returns **success with an empty list**, not an error.  Reserve error
handling for rejected requests and server failures.

This is why the distinction above matters: `is_error == False` with an empty
`pv_metadata_list` means "nothing matched", while `is_error == True` with the same empty list
means "we never found out".

## Paging

Query methods that can return many records come in two flavours.

**`query_*` returns a single page.**  You drive the paging yourself with `page_token` and
`next_page_token`, where an empty `next_page_token` means there are no more pages:

```python
# cookbook:partial
page_token = None
while True:
    page = client.annotation.pv_metadata.query_pv_metadata(
        [Q.attributes("AREA", ["GUNB"])], limit=100, page_token=page_token)
    if page.result_status.is_error:
        raise RuntimeError(page.result_status.message)

    for record in page.pv_metadata_list:
        print(record.pvName)

    page_token = page.next_page_token
    if not page_token:
        break
```

**`iter_*` does that for you.**  It follows the tokens transparently and yields individual
records.  Prefer it unless you specifically need page boundaries:

```python
# cookbook:partial
for record in client.annotation.pv_metadata.iter_pv_metadata([Q.attributes("AREA", ["GUNB"])]):
    print(record.pvName)
```

The `iter_*` methods **raise `RuntimeError` if any page fails**, rather than returning a result
object — a deliberate difference, since a generator has no way to hand back an error flag partway
through.  This also means they cannot silently truncate: a failure on page 3 of 10 is an
exception, not a short iteration.

Some things to keep in mind:

- **`limit` is the page size, not a total cap.**  `limit=100` on an `iter_*` call does not stop
  after 100 records; it fetches 100 at a time until the results are exhausted.  To cap the total,
  use `itertools.islice`.
- **Omitting `limit` does not mean "no limit".**  The server applies its own default page size
  (currently 100) when `limit` is absent or zero, so a `query_*` call without one still returns a
  page, not the whole result set.  Check `next_page_token`.
- **Omitting the criteria browses everything.**  On the annotation-service queries — PV metadata,
  configurations, activations, datasets, annotations — an omitted or empty criteria list matches
  *all* records rather than being rejected, so `iter_pv_metadata()` with no arguments walks the
  whole catalogue:

  ```python
  # cookbook:partial
  for record in client.annotation.pv_metadata.iter_pv_metadata():
      print(record.pvName)
  ```

  The default page size still applies unconditionally — it does not change just because you
  dropped the last criterion — so the bare `query_*` form returns one page and a token, not the
  collection.  `iter_*` is the right call here.  Mind how large the collection is before iterating
  it.
- **There is no total count.**  The API deliberately omits it — computing one requires a separate
  expensive query — so you cannot know the result size in advance.
- **Results come back in a stable order.**  The server sorts each collection by its natural key —
  PV metadata by `pvName`, configurations by `configurationName`, activations by `startTime` — all
  ascending.  This is observed server behavior rather than a documented API guarantee; it is
  useful for reading output and for finding the newest record, but not something to depend on
  across releases.

## Query criteria

Structured queries take a list of criterion objects, built by the static helpers on
`PvMetadataQuery` (`Q`), `ConfigurationQuery` (`C`), `ConfigurationActivationQuery` (`CA`),
`PvQuery` (`PV`), and `ConfigQuery` (`CFG`).

The combining rules are uniform across all of them:

- **Criteria in the list are ANDed**
- **Values within one criterion are ORed**

So to require two tags *simultaneously*, pass two separate criteria rather than one criterion
with two values:

```python
# cookbook:partial
# tags = production OR commissioning
either = [Q.tags(["production", "commissioning"])]

# tags = production AND commissioning
both = [Q.tags(["production"]), Q.tags(["commissioning"])]
```

Name and alias criteria accept `exact`, `prefix`, and `contains` lists, which may coexist:

```python
# cookbook:partial
criteria = [Q.pv_name(prefix=["BPMS:"], contains=["GUNB"])]
```

**The helpers reject empty input.**  They raise `ValueError` rather than building a criterion that
would silently match everything:

```python
# cookbook:partial
Q.tags([])                  # ValueError: tags() requires a non-empty values list
Q.pv_name()                 # ValueError: requires at least one non-empty of exact/prefix/contains
```

That is a deliberate guard — an empty criterion is nearly always a bug in the caller's filter
construction, and failing loudly beats returning the whole collection.

**The one exception is `attributes()` / `attr()`, where an empty `values` list is meaningful.**  It
is the protocol's key-only *existence* search: match every record possessing the attribute key,
whatever its value.  That narrows the result set rather than matching everything, so the reasoning
above does not apply and the helpers allow it:

```python
# cookbook:partial
Q.attributes("S")           # every PV that has an S attribute at all
Q.attributes("S", [])       # the same thing
Q.attributes("", ["0.49"])  # ValueError: the *key* is still required
```

This holds for all seven attribute helpers — `Q`, `C`, `CA`, `DS`, `AQ`, and the `PV.attr()` /
`CFG.attr()` query selectors.

## Save semantics: full replace

Methods named `save_*` are **full-replace upserts**, not partial updates.  Omitted fields are
cleared, not preserved.

When updating an existing record, read it first and carry forward every field you intend to keep:

```python
# cookbook:partial
read = client.annotation.pv_metadata.get_pv_metadata("BPMS:GUNB:314:X")
if read.result_status.is_error:
    raise RuntimeError(read.result_status.message)

existing = read.pv_metadata
assert existing is not None      # guaranteed once is_error is False

# WRONG -- erases aliases, tags, and description
client.annotation.pv_metadata.save_pv_metadata(SavePvMetadataRequestParams(
    pv_name="BPMS:GUNB:314:X",
    attributes={"AREA": "GUNB"},
))

# RIGHT -- complete desired state
client.annotation.pv_metadata.save_pv_metadata(SavePvMetadataRequestParams(
    pv_name=existing.pvName,
    aliases=list(existing.aliases),
    tags=list(existing.tags),
    attributes={a.name: a.value for a in existing.attributes},
    description=existing.description,
    modified_by="catalog-loader",
))
```

The corresponding `patch*` and `bulkSave*` methods are reserved in the protos but **not yet
implemented** — the server returns an error if you reach them through the generated stubs.  This
library does not wrap them.

Most save methods accept an optional `modified_by` identifying the actor making the change.

## Time

Time inputs accept any of three forms, converted by the shared `to_timestamp()` helper:

- a **timezone-aware** `datetime`
- **epoch seconds** as an `int` or `float` (the fractional part becomes nanoseconds)
- an already-built `common_pb2.Timestamp`

```python
# cookbook:partial
from datetime import datetime, timezone

ts1 = to_timestamp(datetime(2026, 2, 2, 18, 4, 1, tzinfo=timezone.utc))
ts2 = to_timestamp(1770055441)
ts3 = to_timestamp(1770055441.5)      # .5 -> 500_000_000 nanoseconds
```

**Naive datetimes raise `ValueError`.**  This is the most likely first-run error, and it is
intentional:

```python
# cookbook:partial
to_timestamp(datetime(2026, 2, 2, 18, 4, 1))     # ValueError -- no tzinfo
```

A naive datetime would otherwise be interpreted against the local timezone, silently shifting
every timestamp by the UTC offset of whatever machine happened to run the code.  Attach `tzinfo`
explicitly, or use `datetime.now(timezone.utc)`.

Two other rejections worth knowing: `bool` raises `TypeError` (it is a subclass of `int`, so it
would otherwise convert silently), and pre-1970 times raise `ValueError`, since
`Timestamp.epochSeconds` is unsigned.

### Half-open ranges

Time ranges are **half-open**: `[begin, end)`.  The begin time is included, the end time
excluded.  Adjacent intervals therefore compose cleanly — one interval's end can equal the next
one's start with no gap and no overlap, which is what makes configuration activation histories
continuous.

Note that *bucket selection* in data queries is an overlap test rather than containment: a bucket
is returned when it overlaps the requested window at all, so boundary buckets may extend past the
range you asked for.  Sample-oriented queries (`query_samples()`) trim to the exact range; a
[bucket query](query.md#buckets-come-back-whole) returns those boundary buckets whole, and trimming
them is opt-in.

## Deadlines, idle timeouts, and keepalive

Every call made through the library has a bound by default, so a server that never answers produces
an error instead of a call that waits forever
([#81](https://github.com/osprey-dcs/dp-python-lib/issues/81)).  The defaults:

| What | Default bound | Configured by (per service) |
|---|---|---|
| A unary call (`get_*`, `save_*`, `query_*`, one page of `iter_*`, ...) | 300 s total | `call_timeout_seconds` |
| `export_data()` | 3600 s total | `annotation.export_timeout_seconds` |
| A stream (`iter_*_stream()`, `ingest_data_stream()`) | 300 s *waiting on the server*; no total | `stream_idle_timeout_seconds` |
| The connection, on channels built from configuration | keepalive ping every 60 s, failing after 20 s unanswered | `keepalive_time_seconds`, `keepalive_timeout_seconds` |

These are hang guards, not performance budgets: no healthy call should come near them.

### `timeout=` on a call

Every method that calls the server takes a keyword-only `timeout`, with three meanings:

```python
# cookbook:partial
page = client.query.query_samples(params)                 # omitted: the configured deadline (300 s)
page = client.query.query_samples(params, timeout=30)     # 30 s, for this call only
page = client.query.query_samples(params, timeout=None)   # no deadline at all
```

The one exception is `await_request_statuses()`, whose `timeout` was already the total time to wait
for the statuses; it now also bounds each status query it makes inside that wait.

`timeout=0` and negative values raise `ValueError`: grpcio would read `0` as a deadline already
passed, while `0` in configuration means "disabled", so the library refuses to guess.  Pass `None` to
disable the deadline.

A call that runs out of time fails like any other gRPC error (`is_error`, or `RuntimeError` from an
`iter_*` method).  The message names the deadline and where it was set, so you know what to change:

```
gRPC error: Deadline Exceeded (client deadline 300 s, set by MLDP_QUERY_CALL_TIMEOUT_SECONDS /
query.call_timeout_seconds; pass timeout= to override one call, or timeout=None to disable)
```

**On paging methods, `timeout` applies to each page, not to the whole iteration.**  `iter_*` and
`get_datasets()` make one call per page or chunk, and each call gets the bound.  A total over an
iteration of unknown length would be the wrong tool; if you need one, check the clock in your loop.
The whole-query conveniences take the same keyword and pass it to the calls they make:
`query_samples_to_dataframe()` and `query_buckets_to_dataframes()` take `timeout=` (per page), and
`stream_query_samples_to_dataframes()` takes `idle_timeout=` and `timeout=` as its stream does.

### Streams use an idle timeout

A healthy stream can run for hours, so a total deadline would cut off exactly the large queries
streaming is for.  A stream is bounded instead by how long the client **waits on the server**: if
the server sends nothing for `idle_timeout` seconds while the client is waiting, the call is
cancelled.  Time your own code spends counts for nothing:

- **Server streams** (`iter_query_samples_stream()`, `iter_query_buckets_stream()`,
  `iter_sample_statuses_stream()`): the clock runs only while your loop is waiting for the next
  message.  Processing a page for ten minutes does not trip it.
- **`iter_ingest_data_bidi_stream()`**: the clock runs only while your loop is waiting for a result
  *and* a request is still unanswered.  The time your request generator spends producing the next
  request does not count.
- **`ingest_data_stream()`**: the clock runs while gRPC is sending a request, and after your
  requests are exhausted, until the summary arrives.  A server that stops reading stalls the upload,
  which the clock catches; the time spent inside your generator does not count.

So the first message, which includes any queueing on a busy server, and each message after it each
get the whole bound.  `idle_timeout=` sets it for one call, with the same three meanings as
`timeout`.  A stream method also accepts `timeout=` as a total deadline, but **omitting it on a
stream means no total deadline**, unlike a unary call:

```python
# cookbook:partial
for page in client.query.iter_query_samples_stream(params, idle_timeout=60):       # quicker to give up
    ...
for page in client.query.iter_query_samples_stream(params, timeout=3600):          # and a cap on the whole
    ...
```

An idle-timed-out stream reports
`gRPC error: stream idle timeout: no message from the server within 300 s`: as a `RuntimeError`
from the `iter_*` methods, and as an `is_error` result from `ingest_data_stream()`.

### Configuring the defaults

Each service has its own settings, through the usual
[configuration priority](connecting.md#configuration-priority) (constructor, then `MLDP_*`
variables, then the YAML file):

```yaml
query:
  call_timeout_seconds: 600            # MLDP_QUERY_CALL_TIMEOUT_SECONDS
  stream_idle_timeout_seconds: 120     # MLDP_QUERY_STREAM_IDLE_TIMEOUT_SECONDS
annotation:
  export_timeout_seconds: 7200         # MLDP_ANNOTATION_EXPORT_TIMEOUT_SECONDS
ingestion:
  keepalive_time_seconds: 0            # MLDP_INGESTION_KEEPALIVE_TIME_SECONDS: no keepalive
```

**`0` disables** a timeout or keepalive, everywhere except `keepalive_timeout_seconds`, which
must be positive (keepalive itself is turned off with `keepalive_time_seconds: 0`).  Negative,
infinite, and NaN values are rejected when the configuration loads.  To put back the behaviour from
before #81, set `call_timeout_seconds`, `stream_idle_timeout_seconds`, `keepalive_time_seconds`
and, for the annotation service, `export_timeout_seconds` to `0`.

A client given explicit channels gets the library defaults for deadlines, since deadlines belong
to the call, not the channel.  Keepalive is a property of the channel, so it does not reach a
channel you build yourself; see below.

### Keepalive detects a dead network path, not a hung server

On a channel built from configuration, the client sends an HTTP/2 ping every 60 s while a call is
in progress, and fails the call with `UNAVAILABLE` ("ping timeout") if the server does not answer
within 20 s.  This catches a network path that has silently gone dead (a dropped VPN or a firewall
that discards traffic without resetting the connection), at most **80 s** after the drop.

It does **not** catch a server that is connected but never answers: such a server goes on
acknowledging pings, so keepalive sees a healthy connection.  That is what the deadlines and idle
timeouts above are for.  The two cover different failures, and you want both.

**Do not set `keepalive_time_seconds` below 30.**  By default dp-service accepts a ping at most
every 30 s, and it answers faster pings by closing the connection (GOAWAY `too_many_pings`), which
fails every call on it.  The library logs a WARNING for a non-zero value below 30, but does not
refuse it, since a deployment can lower the server's limit.

### Channels you build yourself

A channel you construct (for custom TLS credentials, say) has no keepalive unless you ask for it.
`channel_options()` returns the grpcio options the library uses:

```python
import grpc
from dp_python_lib.client import MldpClient
from dp_python_lib.config import channel_options

credentials = grpc.ssl_channel_credentials()
options = channel_options()            # or channel_options(keepalive_time_seconds=120)
client = MldpClient(
    ingestion_channel=grpc.secure_channel("ingest.example.com:443", credentials, options=options),
    query_channel=grpc.secure_channel("query.example.com:443", credentials, options=options),
    annotation_channel=grpc.secure_channel("annotation.example.com:443", credentials, options=options),
)
```

Prefer it to writing the grpcio options out by hand.  Their names overstate what they control:
`grpc.keepalive_timeout_ms` alone did not bound the wait for a ping's answer in grpcio 1.84 (that
took `grpc.http2.ping_timeout_ms`), and without `grpc.http2.max_pings_without_data=0` a quiet stream
is throttled to one ping a minute.  The values were chosen by measuring them against a silently
dropped connection, and a unit test pins the result.

## Optional dependencies

The core install is deliberately lightweight.  Converting query results to pandas, NumPy, or
Excel requires the `analysis` extra:

```
pip install "dp-python-lib[analysis]"
```

Without it, `to_dataframe()` and `to_numpy()` raise `ImportError` at the point of use — the
imports are lazy, so the rest of the library works normally.  See
[Querying archived data](query.md).

## Also worth knowing

- **`createdTime` and `updatedTime` are server-set.**  They appear on records returned by `get_*`
  and `query_*`, but are not accepted as input on save.
- **`client.query` and `client.annotation` are `None` when no channel is configured.**  See
  [Creating and connecting a client](connecting.md).
- **Logging is the application's job, not the library's.**  `dp_python_lib` logs through the
  standard `logging` module under the `dp_python_lib.*` hierarchy but installs no handler; call
  `logging.basicConfig()` in your own code to see it.
