# Labeling Samples

Recording that *this particular sample, at this particular instant, was bad* — and then querying
data with the flagged samples left out.

> **Verified against:** dp-grpc `rel-1.16.0`.
> The sample status API is **new in 1.16.0** and will not work against a `rel-1.15.0` server.

See [API conventions](conventions.md) for result checking, paging, and time handling.

All examples use `client.annotation.sample_status`.  Note that `client.annotation` itself is `None`
unless an annotation channel is configured, so guard on `client.annotation` before reaching through it.

### Imports used by the examples

```python
# cookbook:skip
from datetime import datetime, timezone

from dp_python_lib.client import (
    MldpClient,
    SampleStatusColumn,
    SampleStatusFrame,
    SaveSampleStatusesRequestParams,
    QuerySampleStatusesRequestParams,
    QueryParams,
    PvQuery as PV,
    SampleStatusFilter,
    sampling_clock,
    timestamp_list,
)
from dp_python_lib.client import sample_status_conversions as ssc
```

## Contents

- [Model](#model) — what a sample status is, and two rules that shape everything else
- [Labeling a few bad samples](#labeling-a-few-bad-samples)
- [Labeling a whole regular interval](#labeling-a-whole-regular-interval)
- [Reading statuses back](#reading-statuses-back)
- [Querying data with flagged samples removed](#querying-data-with-flagged-samples-removed)
- [Correcting and retracting](#correcting-and-retracting)
- [Also worth knowing](#also-worth-knowing)

## Model

A **sample status** attaches an `int32` status code to **one PV sample at one instant**.  Its
identity key is the four-tuple:

```
(pvName, timestamp, domain, layer)
```

**`domain`** names the *meaning* of the status codes — the contract between whoever writes the
statuses and whoever reads them.  `"data_quality"` might define `1 = suspect, 2 = bad`;
`"ml_anomaly"` would define something else entirely.  The MLDP does not validate or interpret
codes; it stores them.  This follows the same convention as `EnumColumn`.

**`layer`** names the *producer* — `"ml_model_v1"`, `"operator_override"`, `"calibration_pass"`.
Two producers can label the same sample in the same domain without colliding, because the layer is
part of the key.  That is the point: an operator's override and a model's guess coexist, and a
reader chooses which layers to trust.

Two rules shape how you use all of this.

**Absence means "no assertion."**  There is no implicit default status.  Labeling three samples
bad says *nothing* about the other 8,000 — not that they are good, only that nobody has said
anything about them.  You never need to mark the rest of a run "OK", and doing so would be a
different claim.

**Matching is by exact timestamp, at nanosecond precision.**  A status attaches to a sample whose
timestamp matches *exactly*.  There is no nearest-sample search and no tolerance window.  So the
timestamps you label with must come from the data itself — from query results, or from exact
integer arithmetic on a `SamplingClock`.  A timestamp that has been rounded, or that made a
round-trip through a float, will silently match nothing.  This is the single most common way to
get a save that "succeeds" and a query that returns nothing.

> Sample status replaces the deprecated `DataValue.ValueStatus` mechanism.  `valueStatus` is never
> populated in `querySamples()` results; use this API instead.

## Labeling a few bad samples

The sparse case, and the common one: a handful of individual samples are wrong.  Name exactly
those timestamps with `timestamp_list()`.

```python
# cookbook:partial
# Timestamps taken from the data itself -- never recomputed or rounded.
bad_times = [
    datetime(2024, 2, 2, 18, 4, 12, 250000, tzinfo=timezone.utc),
    datetime(2024, 2, 2, 18, 4, 12, 500000, tzinfo=timezone.utc),
    datetime(2024, 2, 2, 18, 4, 13, 750000, tzinfo=timezone.utc),
]

frame = SampleStatusFrame(
    domain="data_quality",
    layer="operator_override",
    data_timestamps=timestamp_list(bad_times),
    columns=[
        SampleStatusColumn(
            pv_name="BPMS:GUNB:314:X",
            status_codes=[2, 2, 1],                       # 2 = bad, 1 = suspect
            reasons=["beam loss", "beam loss", "settling"],
        )
    ],
)

result = client.annotation.sample_status.save_sample_statuses(
    SaveSampleStatusesRequestParams(
        frames=[frame],
        source="control room, shift log entry 2024-02-02-B",
        modified_by="operator",
    )
)
if result.result_status.is_error:
    print(f"save failed: {result.result_status.message}")
else:
    print(f"saved {result.saved_count} statuses")
```

`confidence` and `reasons` are optional parallel arrays.  Supply one entry per timestamp or omit
them entirely — a partial array is rejected client-side, naming the offending PV, rather than
having the whole batch bounced by the server.

Several PVs sharing the same bad timestamps go in the same frame, one column each:

```python
# cookbook:partial
bad_times = [
    datetime(2024, 2, 2, 18, 4, 12, 250000, tzinfo=timezone.utc),
    datetime(2024, 2, 2, 18, 4, 12, 500000, tzinfo=timezone.utc),
    datetime(2024, 2, 2, 18, 4, 13, 750000, tzinfo=timezone.utc),
]

frame = SampleStatusFrame(
    domain="data_quality",
    layer="operator_override",
    data_timestamps=timestamp_list(bad_times),
    columns=[
        SampleStatusColumn(pv_name="BPMS:GUNB:314:X", status_codes=[2, 2, 1]),
        SampleStatusColumn(pv_name="BPMS:GUNB:314:Y", status_codes=[2, 2, 1]),
        SampleStatusColumn(pv_name="BPMS:GUNB:314:TMIT", status_codes=[0, 2, 0]),
    ],
)
```

A PV may appear at most once per frame — a second column for the same PV would be two competing
claims about the same key, so it raises.

## Labeling a whole regular interval

When a model scores every sample in a regularly-sampled run, naming each timestamp individually is
wasteful.  `sampling_clock()` describes the axis compactly: a start time, a period, and a count.

```python
# cookbook:partial
# 10 kHz for one second: 10,000 samples, 100,000 ns apart.
def run_model() -> list[int]:               # your model, returning 10,000 status codes
    return [0] * 10_000

def model_confidence() -> list[float]:      # and 10,000 confidence values
    return [1.0] * 10_000

axis = sampling_clock(
    start_time=datetime(2024, 2, 2, 18, 4, 12, tzinfo=timezone.utc),
    period_nanos=100_000,
    count=10_000,
)

# Your model's output: one entry per sample on the axis above.
scores: list[int] = run_model()
confidence: list[float] = model_confidence()

frame = SampleStatusFrame(
    domain="ml_anomaly",
    layer="ml_model_v1",
    data_timestamps=axis,
    columns=[
        SampleStatusColumn(
            pv_name="BPMS:GUNB:314:X",
            status_codes=scores,
            confidence=confidence,
        )
    ],
)
```

**The clock must match the archived data's clock exactly.**  Status-to-sample matching is by exact
timestamp, so a period that is off by a single nanosecond misses every sample after the first.
Take `period_nanos` from the data's own sampling clock rather than deriving it from a nominal rate.

## Reading statuses back

Query by time range, optionally filtered by PV, domain, and layer.  The three filters are ANDed;
values within one filter are ORed.  Omitting `pv_names` matches *every* PV with statuses in the
range — which is how you discover what a layer has been labeling.

```python
# cookbook:partial
status_params = QuerySampleStatusesRequestParams(
    begin_time=begin,
    end_time=end,
    pv_names=["BPMS:GUNB:314:X"],
    domains=["data_quality"],
)

for page in client.annotation.sample_status.iter_sample_statuses(status_params):
    for bucket in page.sample_status_buckets:
        print(bucket.statusColumn.pvName, bucket.layer)
```

Results come back as **buckets**: one PV's statuses over one time axis, in compact form. To work
sample by sample, expand them into rows:

```python
# cookbook:partial
status_params = QuerySampleStatusesRequestParams(begin_time=begin, end_time=end)

for row in ssc.iter_rows(client.annotation.sample_status.iter_sample_statuses(status_params)):
    print(row.pv_name, row.epoch_nanos, row.status_code, row.reason)
```

Each `SampleStatusRow` carries its own exact timestamp (as integer epoch nanoseconds, computed
from the axis without ever passing through a float), plus the status code, domain, layer, and the
optional confidence and reason.

**`confidence` and `reason` are `None` when the producer did not supply them** — not `0.0` and not
`""`.  That distinction is load-bearing: `0.0` is a perfectly good confidence value, so a
fabricated default would be indistinguishable from a real "I am certain this is fine."

`row.timestamp` gives the same instant back as a `common.Timestamp`, ready to round-trip into a
correction or a delete.

For a single page, or for buckets already in hand:

```python
# cookbook:partial
status_params = QuerySampleStatusesRequestParams(begin_time=begin, end_time=end)

page = client.annotation.sample_status.query_sample_statuses(status_params)
rows = ssc.buckets_to_rows(page.sample_status_buckets)
```

There is also a streaming form, `iter_sample_statuses_stream()`, which is lazy and
fire-and-consume — no page tokens, the server pushes.  Use it for large sweeps.

## Querying data with flagged samples removed

The payoff.  A time-series query can filter on sample status, so you can ask for a PV's data
*minus whatever a given layer flagged*:

```python
# cookbook:partial
params = QueryParams(
    begin_time=begin,
    end_time=end,
    pv_selector=PV.name_list(["BPMS:GUNB:314:X"]),
    sample_status_filter=SampleStatusFilter.exclude(
        domain="data_quality",
        layers=["operator_override"],
        status_codes=[2],                 # drop only what was called outright bad
    ),
)

df = client.query.query_samples(params).to_dataframe()
```

Or the inverse — retrieve *only* the flagged samples, to look at what the model caught:

```python
# cookbook:partial
params = QueryParams(
    begin_time=begin,
    end_time=end,
    pv_selector=PV.name_list(["BPMS:GUNB:314:X"]),
    sample_status_filter=SampleStatusFilter.include(domain="ml_anomaly", layers=["ml_model_v1"]),
)
```

Omitting `layers` matches every layer in the domain; omitting `status_codes` matches any code.

The "absence means no assertion" rule decides what happens to unlabeled samples, and it is worth
being deliberate about: an unlabeled sample **does not match** the filter. So `exclude()` keeps it
(you remove only what was explicitly flagged) and `include()` drops it (you narrow to explicitly
labeled samples only). `exclude()` is almost always what you want for analysis.

There is no `MODE_UNSPECIFIED` to fall into — `include()` and `exclude()` are the ways to build a
filter, and the server rejects an unspecified mode. If you hand-build a `SampleStatusSelector`
instead, `QueryParams` rejects an empty domain or an unspecified mode up front rather than letting
the request reach the server.

## Correcting and retracting

Saving is an **upsert keyed by `(pvName, timestamp, domain, layer)`**, and it replaces the matched
status **in full**.  Re-saving a key with `reasons` omitted clears the reason that was there.
Always supply the complete desired state:

```python
# cookbook:partial
# Downgrade a sample from "bad" to "suspect", keeping a reason.
frame = SampleStatusFrame(
    domain="data_quality",
    layer="operator_override",
    data_timestamps=timestamp_list(
        [datetime(2024, 2, 2, 18, 4, 12, 250000, tzinfo=timezone.utc)]),
    columns=[SampleStatusColumn(
        pv_name="BPMS:GUNB:314:X",
        status_codes=[1],
        reasons=["re-reviewed: within tolerance after recalibration"],
    )],
)
client.annotation.sample_status.save_sample_statuses(
    SaveSampleStatusesRequestParams(frames=[frame], modified_by="operator"))
```

To retract entirely — to go back to *"nobody has said anything about these samples"* — delete.
Deletion is scoped to one required `(domain, layer)` over a half-open range:

```python
# cookbook:partial
result = client.annotation.sample_status.delete_sample_statuses(
    begin_time=begin,
    end_time=end,
    domain="ml_anomaly",
    layer="ml_model_v1",
    pv_names=["BPMS:GUNB:314:X"],
)
print(f"deleted {result.deleted_count}")
```

Omitting `pv_names` would delete that layer's statuses for **every PV** in the range — the path for
retiring an obsolete model's output.  Because that is destructive and easy to reach by leaving an
argument off, it must be opted into explicitly:

```python
# cookbook:partial
client.annotation.sample_status.delete_sample_statuses(
    begin_time=begin, end_time=end,
    domain="ml_anomaly", layer="ml_model_v1",
    all_pvs=True,
)
```

Passing neither `pv_names` nor `all_pvs` raises, as does passing both.  To see what a wildcard
delete would remove, run the same range and `(domain, layer)` through
`query_sample_statuses()` first.

## Also worth knowing

- **A delete that matches nothing is a success**, with `deleted_count == 0` — not an error.
- **Deletion is exact at the sample axis.**  Only statuses whose timestamps fall inside
  `[begin_time, end_time)` are removed; a bucket straddling the boundary is not deleted wholesale.
- **`source` and `modified_by` apply to the whole request**, not per frame.  Batch frames from one
  producer per request; mixing producers records the same provenance for all of them.
- **An all-empty `reasons` list is dropped** rather than sent as empty strings.  A partially
  populated one is kept whole — the blank entries position the real reasons against the axis.
- **Domain registration is not yet implemented.**  `saveSampleStatusDomain` and
  `querySampleStatusDomains` are reserved placeholders in the API that return a "not implemented"
  error, so this client does not wrap them.  Domains are for now a convention between producer and
  consumer, not a registry.
- **Sample status filtering is sample-query only.**  `sampleStatusSelector` is rejected on
  bucket-oriented queries, which are not yet wrapped by this library
  ([issue #16](https://github.com/osprey-dcs/dp-python-lib/issues/16)).
- **A pandas view of statuses is not built yet.**  `sample_status_conversions` returns plain Python
  objects and needs no optional extras; a DataFrame conversion is a follow-up.

### How far these examples have been verified

The save/query/delete loop **has** been exercised against a live 1.16.0 Annotation Service, by
`tests/integration/test_sample_status_client_integration.py`.  That covers the parts most likely
to break silently:

- Timestamps round-trip exactly, through both `timestamp_list()` and `sampling_clock()` — the
  dense case labels 100 samples at 1 kHz and checks every expanded position against
  `startTime + i * periodNanos`.
- Unsupplied `confidence` / `reasons` come back as `None`, not `0.0` / `""`.
- Re-saving a key with `reasons` omitted clears the stored reason (full replace, not merge).
- The same PV and instant in two layers stay two distinct statuses.

What has **not** been observed is the last link: labeling *real archived samples* and watching a
status-filtered query drop exactly those rows.  The integration tests save statuses and read them
back, but there is no way to ingest sample data from Python yet
([issue #17](https://github.com/osprey-dcs/dp-python-lib/issues/17)), so the statuses they write
have no underlying samples to attach to.

**When the ingestion client lands, re-verify the filtering examples** — the
[querying with flagged samples removed](#querying-data-with-flagged-samples-removed) section is
the part still standing on unit tests alone.
