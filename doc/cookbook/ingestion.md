# Ingesting Data

Getting samples into the archive — registering as a data provider, sending frames of time-series
data, and confirming that they actually landed.

See [API conventions](conventions.md) for result checking and time handling.  The frames sent here
are the same `common.DataFrame` an annotation's calculations use, built with the same `data_frame`
builders; [DataSets and annotations](datasets-and-annotations.md#attaching-an-analysis-result)
introduces them.

All examples use `client.ingestion_client`.

> The ingestion API has not changed since 1.15.0, so this recipe works against a `rel-1.15.0`
> server, with one exception: a column's metadata and provenance survive ingestion only into a
> `rel-1.16.0` or later one.

### Imports used by the examples

```python
# cookbook:skip
import contextlib
from datetime import datetime, timedelta, timezone

from dp_python_lib.client import (
    MldpClient,
    RegisterProviderRequestParams,
    IngestDataRequestParams,
    IngestionRequestStatus,
    RequestStatusQuery as RS,
    chunked_request_params,
)
from dp_python_lib.client import data_frame as dfb
from dp_python_lib.client import data_frame_conversions as dfc
```

## Contents

- [Model](#model) — providers, requests, and why an ack is not a receipt
- [Registering a provider](#registering-a-provider)
- [Ingesting a frame](#ingesting-a-frame)
- [Confirming what landed](#confirming-what-landed)
- [Ingesting from pandas](#ingesting-from-pandas)
- [Frames too big for one message](#frames-too-big-for-one-message) — chunking and streaming
- [One result per request: the bidirectional stream](#one-result-per-request-the-bidirectional-stream)
- [Arrays, images, and structures](#arrays-images-and-structures)
- [Reading failures](#reading-failures)
- [Also worth knowing](#also-worth-knowing)

## Model

A **provider** is whatever produces the data — a DAQ process, a script, a detector.  It registers
once by name and gets back an id that every request carries.

A **request** is one frame of data: a time axis plus one column per PV, sent with a
`client_request_id` of your choosing (or a generated one).

The important part is what the service's answer means.  **An ack says only that the request passed
validation.**  The service checks the request, acks or rejects it, and *then* queues it for
writing.  Anything that goes wrong after that — including an unknown provider id — is invisible at
ack time.  What tells you the data landed is the request's **status document**, which the service
writes after the data is stored:

```
ingest_data()  ──>  ack or reject          "is it well-formed?"
                        │
                        ▼ (queued)
                    write buckets
                        │
                        ▼
                    status document       "did it land?"   SUCCESS / ERROR / REJECTED
```

So every ingest in this recipe ends with a status check, and a SUCCESS status means the data is
stored and queryable.

## Registering a provider

```python
# cookbook:partial
registration = client.ingestion_client.register_provider(RegisterProviderRequestParams(
    "bpm-daq",
    description="GUNB BPM acquisition",
    tag_list=["bpm", "gunb"],
    attribute_map={"AREA": "GUNB"},
))
if registration.result_status.is_error:
    raise RuntimeError(registration.result_status.message)

provider_id = registration.provider_id
assert provider_id is not None     # guaranteed once is_error is False; accessors are Optional
print(provider_id, registration.is_new_provider)
```

Registration is **idempotent by name**: registering `"bpm-daq"` again returns the same id, with
`is_new_provider` False.  So a process can simply register at startup instead of storing its id.

## Ingesting a frame

One second of the worked example's BPM, at 10 kHz: a `SamplingClock` axis (start, period, count)
and one column per PV.

```python
# cookbook:no-mypy
def acquire(pv: str, count: int) -> list[float]:
    """Stand-in for your acquisition system: `count` readings of one PV."""
    offset = {"X": 0.0, "Y": 0.5, "TMIT": 1000.0}[pv.rsplit(":", 1)[1]]
    return [offset + (i % 100) / 100 for i in range(count)]
```

```python
# cookbook:partial
start = datetime(2026, 2, 2, 18, 4, 12, tzinfo=timezone.utc)
count = 10_000
frame = dfb.data_frame(
    dfb.sampling_clock(start, period_nanos=100_000, count=count),
    [dfb.double_column(pv, acquire(pv, count))
     for pv in ("BPMS:GUNB:314:X", "BPMS:GUNB:314:Y", "BPMS:GUNB:314:TMIT")],
)

request = IngestDataRequestParams(provider_id, frame)  # client_request_id: a generated uuid4
since = datetime.now(timezone.utc)                     # captured BEFORE sending -- see below
ack = client.ingestion_client.ingest_data(request)
if ack.result_status.is_error:
    raise RuntimeError(ack.result_status.message)      # rejected: nothing was queued
print(ack.client_request_id, ack.num_rows, ack.num_columns)   # '<uuid>' 10000 3
```

`data_frame()` checks the frame's shape as it builds it — every column has one value per
timestamp, names are unique and non-blank — and `IngestDataRequestParams` checks it again, so a
malformed frame raises `ValueError` naming the column rather than being rejected by the server.
Size limits are left to the server; see [frames too big for one message](#frames-too-big-for-one-message).

## Confirming what landed

```python
# cookbook:partial
statuses = client.ingestion_client.await_request_statuses(
    provider_id, [request.client_request_id], since=since)

for document in statuses[request.client_request_id]:
    status = IngestionRequestStatus(document.ingestionRequestStatus)
    print(status.name, document.statusMessage)                  # SUCCESS
    if status != IngestionRequestStatus.SUCCESS:
        raise RuntimeError(f"{request.client_request_id}: {status.name}: {document.statusMessage}")
```

`await_request_statuses()` polls until every named request has a status document, then returns
them, keyed by request id.  It does not judge them — that is the loop above.  A few details:

- **`since` is required**, and should be captured *before* the first request is sent.  The status
  query has no paging, so the time floor keeps the response small, and it keeps out documents from
  earlier runs that happened to reuse a request id.  The floor is backed off a minute for clock
  skew between you and the server.
- **Each id maps to a list**, because the server does not enforce unique request ids.  With the
  default generated ids there is exactly one document per request.
- **A missing document means "unknown", not "success".**  A few server paths write none at all —
  a request rejected while the service shuts down, say — which is why the wait has a `timeout`
  (30 seconds by default) and raises `TimeoutError` naming the ids still missing.
- **Never read a status you did not get from a document.**  The status enum's zero value is
  SUCCESS, so an unset or defaulted field reads as success.

To look at statuses directly, `query_request_status()` takes criteria built with
`RequestStatusQuery`, ANDed:

```python
# cookbook:partial
result = client.ingestion_client.query_request_status([
    RS.provider_id(provider_id),
    RS.status([IngestionRequestStatus.ERROR, IngestionRequestStatus.REJECTED]),
    RS.time_range(datetime.now(timezone.utc) - timedelta(hours=1)),   # the last hour; end omitted: now
])
for document in result.request_statuses or []:
    print(document.requestId, document.statusMessage)
```

The time range matches when each request *finished processing*, not the time of its data.  One
query cannot ask about several request ids — each `RS.request_id()` criterion holds one, and
criteria AND — which is why `await_request_statuses()` queries by provider and time and matches
the ids itself.

## Ingesting from pandas

If the data is already in a pandas DataFrame with a tz-aware `DatetimeIndex`,
`data_frame_from_pandas()` converts it, mapping each column's dtype to a typed column (this needs
the `[analysis]` extra).  The next second of the same BPM:

```python
# cookbook:partial
import pandas as pd

index = pd.date_range(datetime(2026, 2, 2, 18, 4, 13, tzinfo=timezone.utc), periods=10_000, freq="100us")
table = pd.DataFrame(
    {pv: acquire(pv, 10_000) for pv in ("BPMS:GUNB:314:X", "BPMS:GUNB:314:Y", "BPMS:GUNB:314:TMIT")},
    index=index,
)

request = IngestDataRequestParams(provider_id, dfc.data_frame_from_pandas(table))
since = datetime.now(timezone.utc)
ack = client.ingestion_client.ingest_data(request)
```

Confirm it exactly as above.  Two things differ from building the frame yourself:

- **The time axis is always an explicit `TimestampList`**, one timestamp per row, never an inferred
  `SamplingClock` — even for a regular index like this one.  That is about 14 more bytes per row on
  the wire.  For regularly-sampled data you can describe with a clock, the builders are smaller.
- **A `NaN` anywhere is rejected.**  A typed column holds exactly one value per timestamp and cannot
  express a gap; drop or fill the missing samples first, or put the sparse PV in its own frame.

## Frames too big for one message

The service accepts messages up to **4,096,000 bytes** by default — around half a million doubles.
A minute of one 10 kHz PV is 600,000.  Over the limit, the call fails with a gRPC
`RESOURCE_EXHAUSTED` error rather than a reject, and on a stream it takes every later request down
with it.

`split_data_frame()` cuts a frame along its time axis into pieces that each fit, lazily;
`chunked_request_params()` wraps them as requests with correlated ids; and `ingest_data_stream()`
sends them all on one call:

```python
# cookbook:partial
minute = datetime(2026, 2, 2, 18, 5, tzinfo=timezone.utc)
count = 600_000                                        # one minute at 10 kHz
frame = dfb.data_frame(
    dfb.sampling_clock(minute, period_nanos=100_000, count=count),
    [dfb.double_column(pv, acquire(pv, count)) for pv in ("BPMS:GUNB:314:X", "BPMS:GUNB:314:Y")],
)

chunks = dfb.split_data_frame(frame, max_bytes=dfb.SERVER_DEFAULT_MAX_MESSAGE_BYTES)
since = datetime.now(timezone.utc)
summary = client.ingestion_client.ingest_data_stream(
    chunked_request_params(provider_id, chunks, base_request_id="gunb-314-1805"))

print(summary.num_requests, summary.client_request_ids)
# 3 ['gunb-314-1805-0', 'gunb-314-1805-1', 'gunb-314-1805-2']
if summary.result_status.is_error:
    print("rejected:", summary.rejected_request_ids)

request_ids = summary.client_request_ids or []
statuses = client.ingestion_client.await_request_statuses(provider_id, request_ids, since=since)
```

Some points about each piece:

- **`max_bytes` bounds the whole request**, frame and ids included, with room reserved for the
  longest ids the library allows.  `SERVER_DEFAULT_MAX_MESSAGE_BYTES` is the server's *default*,
  exported as a reference; a deployment can configure its own, so pass what yours uses.
- **`split_data_frame()` also takes `max_rows` and `max_span_nanos`.**  The service caps one
  request's time span (first to last timestamp) at a day by default, so a long capture needs
  `max_span_nanos` as well.
- **Chunk *n* is `<base>-<n>`**, so the pieces of one frame are recognizable in their acks and
  status documents.  Omit `base_request_id` for a generated one.
- **Everything is lazy.**  `split_data_frame()` and `chunked_request_params()` are generators, and
  `ingest_data_stream()` pulls from them as it sends, so a large frame is never held twice.
- **A reject does not end the stream.**  The service checks each request as it arrives and queues
  every valid one.  If any were rejected, the result is an error that *still carries its
  response*: `rejected_request_ids` names the failures, and every other request was accepted.  It
  does not raise, because raising would hide which ones got through.  `num_requests` is then
  `None`; the server omits it.
- **If your request generator raises**, that exception is raised from `ingest_data_stream()` as
  itself, with a note giving how many requests had already been handed over.  Some of those may
  already be stored, so check their status before re-sending anything — a re-send of the same data
  fails, as [reading failures](#reading-failures) explains.

## One result per request: the bidirectional stream

`iter_ingest_data_bidi_stream()` sends requests on one call like `ingest_data_stream()`, but
yields each request's ack or reject as it arrives, in order — useful for a long-running producer
that wants to react to a reject as it happens:

```python
# cookbook:partial
second = datetime(2026, 2, 2, 18, 6, tzinfo=timezone.utc)

def one_second_frames(provider_id: str, n: int):
    """A producer: n consecutive one-second frames of the BPM's X plane."""
    for i in range(n):
        yield IngestDataRequestParams(provider_id, dfb.data_frame(
            dfb.sampling_clock(second + timedelta(seconds=i), period_nanos=100_000, count=10_000),
            [dfb.double_column("BPMS:GUNB:314:X", acquire("BPMS:GUNB:314:X", 10_000))],
        ))

requests = one_second_frames(provider_id, 10)
with contextlib.closing(client.ingestion_client.iter_ingest_data_bidi_stream(requests)) as results:
    for result in results:
        if result.result_status.is_error:
            print(f"{result.client_request_id} rejected: {result.result_status.message}")
            break                      # leaving the with block cancels the call
```

**A reject is yielded, not raised**: it is a fact about one request, and the service carries on
with the rest.  Only a transport failure ends the loop, as a `RuntimeError`.

**To stop early, close the iterator** — `contextlib.closing` does that however the block exits.
Closing cancels the call, so no further requests are sent.  A bare `break` without it is not
enough: while anything still refers to the iterator, the library keeps pulling requests from your
producer and sending them, until the iterator is garbage-collected.  Reading to the end needs no
close.

## Arrays, images, and structures

Beyond scalar columns, the `data_frame` builders cover one fixed-shape array per sample, one
encoded image per sample, one serialized structure per sample, and a whole column as one opaque
payload:

```python
# cookbook:partial
axis = dfb.sampling_clock(datetime(2026, 2, 2, 18, 7, tzinfo=timezone.utc), period_nanos=1_000_000, count=2)
frame = dfb.data_frame(axis, [
    dfb.double_array_column("BPMS:GUNB:314:WAVEFORM", [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]),
    dfb.image_column("CAMR:GUNB:100:IMAGE", [b"...png bytes...", b"...png bytes..."],
                     width=640, height=480, channels=1, encoding="png"),
    dfb.struct_column("BPMS:GUNB:314:STATE", [b"\x08\x01", b"\x08\x02"], schema_id="bpm_state:v1"),
])
```

Array samples may be nested lists or NumPy arrays, one to three dimensions, all the same shape;
they are stored flat in row-major order.  The image descriptor and the struct's `schema_id` are
per column.  These ingest and confirm like any other frame, but **cannot be queried back yet**:
`query_samples()` returns scalar columns only, and the bucket query that returns the rest is
[issue #16](https://github.com/osprey-dcs/dp-python-lib/issues/16).

## Reading failures

Where a problem shows up depends on who can see it, and when:

| Where | What | Examples |
|---|---|---|
| `ValueError` when building the params | The library's own checks | a column shorter than the axis, a blank or duplicate name, an id over 256 characters |
| A reject (`is_error` on the ack; `rejected_request_ids` on a stream) | The service's validation | a frame spanning more than a day; a value over a size cap |
| An ERROR status document | Anything after the ack | **an unknown provider id**; a re-ingest of the same data |
| gRPC error, `RESOURCE_EXHAUSTED` | A message over the size limit | a frame that needed `split_data_frame()`; the error message says so |

Two of these catch people out:

- **An unknown provider id is acked.**  The provider is looked up only when the request is
  processed, so a typo in the id succeeds at ack time and fails in the status document.  Only the
  status check catches it.
- **Re-ingesting the same data fails, possibly halfway.**  Stored data is keyed by PV and first
  timestamp, so sending a frame again — a retry after a timeout, say — is acked and then ends in
  ERROR.  The columns are written in order, so a multi-column frame can leave some PVs stored under
  an ERROR status.  Before retrying, check whether the first attempt landed.

## Also worth knowing

- **The client is stricter than the service in two places**: whitespace-only column names and
  duplicate timestamps are rejected, although the service accepts both, because the library must
  be able to read back what it writes.  If you truly need either, build the request by hand and
  call the generated stub.
- **Request ids** default to a random uuid4.  Pass your own when it should mean something, but keep
  it unique: nothing enforces that, and a reused id makes its status ambiguous.
- **There is no delete.**  The archive has no RPC for removing ingested samples, which is another
  reason to confirm before re-sending.
- **`subscribeData()`**, the service's live subscription to newly ingested data, is not wrapped.

### How far these examples have been verified

This recipe was run as one continuous script against a live MLDP stack, with the PV names made
unique per run.  The behaviors it describes — the ack-then-ERROR for an unknown provider and for a
re-ingest, the partial reject on a stream, the bidirectional stream's per-request results,
chunked ingest reading back whole, and arrays, images, structs, and serialized columns reaching
SUCCESS — are covered by `tests/integration/test_ingestion_client_integration.py`.
