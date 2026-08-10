# Querying Time-Series Data

Retrieving archived PV samples over a time range — by name, by what the PVs *are*, or by what the
machine was *doing* — and getting the results into pandas or NumPy.

> **Verified against:** dp-python-lib 1.15.0.
> ⚠️ **This API requires a server newer than the latest dp-grpc release.**  `querySamples`,
> `PvSelector`, and `common.TimeRange` do not exist in `rel-1.14.0`, which is currently the newest
> published dp-grpc release — they come from unreleased work that dp-python-lib 1.15.0 was
> generated from.  Against a `rel-1.14.0` server these calls fail; the rest of this cookbook works
> on both.  Check what your deployment actually runs before relying on this recipe.

See [API conventions](conventions.md) for result checking and paging.  The metadata- and
configuration-driven queries below read the catalogue built in
[Cataloguing PVs](pv-metadata.md) and [Recording machine configuration](machine-configuration.md).

All examples use `client.query`, which is `None` unless a query channel is configured.

### Imports used by the examples

```python
# cookbook:skip
from datetime import datetime, timezone

from dp_python_lib.client import (
    MldpClient,
    QueryParams,
    PvQuery as PV,
    ConfigQuery as CFG,
)
from dp_python_lib.client import query_conversions as qc
```

## Contents

- [Model](#model) — one `QueryParams`, three ways to choose PVs
- [Querying a known list of PVs](#querying-a-known-list-of-pvs)
- [Selecting PVs by metadata](#selecting-pvs-by-metadata) — "every BPM in GUNB"
- [Scoping a query to a machine configuration](#scoping-a-query-to-a-machine-configuration)
- [Getting results into pandas and NumPy](#getting-results-into-pandas-and-numpy)
- [Large queries: paging and streaming](#large-queries-paging-and-streaming)
- [Also worth knowing](#also-worth-knowing)

## Model

A query is described by a single `QueryParams`: a half-open time range `[begin_time, end_time)`
plus a choice of which PVs to return.

PVs are chosen in one of **three mutually exclusive ways** — pick exactly one:

| Selector | Use when |
|---|---|
| `PV.name_list([...])` | You know the PV names |
| `PV.pattern("BPMS:GUNB:.*")` | Names share a shape |
| `PV.metadata([...])` | You want PVs by what they *are* — area, type, device |

Independently, `config_criteria` restricts results to the intervals when matching machine
configurations were **active**.  It can be combined with any PV selector, or used alone — a
config-only query returns everything recorded while that configuration was in effect.

At least one of `pv_selector` or `config_criteria` must be present.

Results arrive as a **`ColumnTable`**: a list of timestamps plus one `DataColumn` per PV.  You can
work with that directly, or convert it — see
[Getting results into pandas and NumPy](#getting-results-into-pandas-and-numpy).

### Validation happens at construction

`QueryParams` rejects bad input immediately, rather than letting the server refuse it:

```python
# cookbook:partial
begin = datetime(2026, 2, 2, 17, 0, tzinfo=timezone.utc)
end = datetime(2026, 2, 2, 23, 0, tzinfo=timezone.utc)

QueryParams(begin_time=end, end_time=begin,
            pv_selector=PV.name_list(["BPMS:GUNB:314:X"]))   # ValueError: begin must precede end
```

Also rejected: no selector at all, and a negative `limit`.  Note that **`limit=0` is meaningful** —
it means "let the server choose a page size".

## Querying a known list of PVs

The three signals of one BPM over a shift:

```python
# cookbook:partial
begin = datetime(2026, 2, 2, 17, 0, tzinfo=timezone.utc)
end = datetime(2026, 2, 2, 23, 0, tzinfo=timezone.utc)

params = QueryParams(
    begin_time=begin,
    end_time=end,
    pv_selector=PV.name_list([
        "BPMS:GUNB:314:X",
        "BPMS:GUNB:314:Y",
        "BPMS:GUNB:314:TMIT",
    ]),
    limit=10_000,        # rows PER PAGE, not a total cap
)

result = client.query.query_samples(params)
if result.result_status.is_error:
    raise RuntimeError(result.result_status.message)

table = result.column_table
if table is not None:
    print(f"{len(table.timestampList.timestamps)} rows, {len(table.dataColumns)} columns")
```

`query_samples()` returns **one page**.  If `result.next_page_token` is non-empty there is more
data — see [Large queries](#large-queries-paging-and-streaming).

### By name pattern

```python
# cookbook:partial
params = QueryParams(
    begin_time=begin,
    end_time=end,
    pv_selector=PV.pattern("BPMS:GUNB:.*"),
)
```

## Selecting PVs by metadata

This is what the [PV catalogue](pv-metadata.md) is for: ask for *every beam position monitor in
GUNB* without maintaining a list of names.

```python
# cookbook:partial
params = QueryParams(
    begin_time=begin,
    end_time=end,
    pv_selector=PV.metadata([
        PV.attr("AREA", ["GUNB"]),
        PV.attr("TYPE", ["MONI"]),
    ]),
)

for page in client.query.iter_query_samples(params):
    table = page.column_table
    if table is not None:
        print([column.name for column in table.dataColumns])
```

The criteria follow the usual rule — **separate criteria are ANDed, values within one are ORed** —
so this reads "in area GUNB *and* of type MONI".  Widening to several areas is one criterion with
several values:

```python
# cookbook:partial
selector = PV.metadata([
    PV.attr("AREA", ["GUNB", "L0B", "HTR"]),    # any of these areas
    PV.attr("TYPE", ["MONI"]),                   # AND a monitor
])
```

Every signal from one physical device:

```python
# cookbook:partial
selector = PV.metadata([PV.attr("DEVICE", ["BPMS:GUNB:314"])])
```

`PV` also offers `pv_name(exact=, prefix=, contains=)`, `aliases(...)`, and `tags(values)`:

```python
# cookbook:partial
selector = PV.metadata([
    PV.pv_name(prefix=["BPMS:"]),
    PV.tags(["production"]),
])
```

> **These are not the same helpers as `PvMetadataQuery`.**  `Q.attributes(key, values)` builds
> criteria for *searching the catalogue*; `PV.attr(key, values)` builds criteria for *selecting
> PVs in a query*.  They mirror each other but are different types and are not interchangeable —
> note the different method name (`attributes` vs. `attr`).

## Scoping a query to a machine configuration

`config_criteria` restricts results to the intervals when a matching configuration was active.
This is how you ask for data *"from the CXI production shift"* without knowing when it ran.

```python
# cookbook:partial
params = QueryParams(
    begin_time=datetime(2026, 2, 2, 0, 0, tzinfo=timezone.utc),
    end_time=datetime(2026, 2, 3, 0, 0, tzinfo=timezone.utc),
    pv_selector=PV.metadata([PV.attr("AREA", ["GUNB"]), PV.attr("TYPE", ["MONI"])]),
    config_criteria=[CFG.configuration_name(["cxi-production"])],
)
```

The time range still bounds the search; the configuration narrows it further to the sub-intervals
that were actually active.  Samples recorded in the same window under a different configuration
are excluded.

### By experiment

Attributes recorded on the **activation** are matchable, so an experiment identifier works
directly:

```python
# cookbook:partial
params = QueryParams(
    begin_time=datetime(2026, 2, 2, 0, 0, tzinfo=timezone.utc),
    end_time=datetime(2026, 2, 3, 0, 0, tzinfo=timezone.utc),
    pv_selector=PV.metadata([PV.attr("TYPE", ["MONI"])]),
    config_criteria=[CFG.attr("EXP", ["CXI_3443"])],
)
```

`CFG` offers `configuration_name`, `client_activation_id`, `category`, `tags`, and
`attr(key, values)`.

### The result covers several disjoint intervals

If a configuration was active more than once inside the time range — two shifts in a day, say —
the query resolves to **several disjoint windows**, not one span from the first start to the last
end.  Samples recorded in the gaps between activations are excluded, even where they fall inside
the outer `[begin_time, end_time)` range.

The returned rows are therefore not necessarily contiguous in time.  A DataFrame built from them
has a jump in its index at each gap, which matters if you resample or difference across it.

### Config-only queries

Omit the PV selector entirely to get **everything** recorded while a configuration was active:

```python
# cookbook:partial
params = QueryParams(
    begin_time=datetime(2026, 2, 2, 0, 0, tzinfo=timezone.utc),
    end_time=datetime(2026, 2, 3, 0, 0, tzinfo=timezone.utc),
    config_criteria=[CFG.attr("DEST", ["CXI"])],
)
```

This is legal and occasionally what you want, but it can return a great deal of data.  Bound it
with a tight time range and a `limit`.

## Getting results into pandas and NumPy

These conversions need the optional extra:

```
pip install dp-python-lib[analysis]
```

Without it, the calls below raise `ImportError` — the imports are lazy, so the rest of the
library works regardless.

### One page to a DataFrame

```python
# cookbook:partial
result = client.query.query_samples(params)
if result.result_status.is_error:
    raise RuntimeError(result.result_status.message)

df = result.to_dataframe()
print(df)
```

The frame has a **UTC datetime index** and one column per PV, named by `DataColumn.name`:

```
                           BPMS:GUNB:314:X  BPMS:GUNB:314:TMIT
2026-02-02 17:00:00+00:00              0.1                 100
2026-02-02 17:00:01+00:00              0.2                 200
2026-02-02 17:00:02+00:00              0.3                 300
```

Per-column metadata from the catalogue — tags and attributes — travels with the results and lands
in `df.attrs`:

```python
# cookbook:partial
df = client.query.query_samples(params).to_dataframe()
print(df.attrs["column_metadata"])
# {'BPMS:GUNB:314:X': {'tags': ['production'], 'attributes': {'AREA': 'GUNB'}}}
```

Pass `exclude_column_metadata=True` to `QueryParams` to skip fetching it, or
`to_dataframe(exclude_column_metadata=True)` to drop it from the frame.

### The whole query to one DataFrame

`query_samples_to_dataframe()` pages internally and concatenates by column name:

```python
# cookbook:partial
df = qc.query_samples_to_dataframe(client.query, params, max_rows=1_000_000)
```

`max_rows` is a guard against pulling an unbounded query into memory: it raises once the
accumulated frame would exceed the cap, rather than silently truncating.

### NumPy

```python
# cookbook:partial
arrays = client.query.query_samples(params).to_numpy()
print(arrays["timestamps"].dtype)          # datetime64[ns]
print(arrays["BPMS:GUNB:314:X"].dtype)     # float64
```

A dict of **1-D** arrays, keyed by column name plus `"timestamps"`.  Complex values (arrays,
structures, images) stay as 1-D object arrays rather than collapsing into a 2-D array, so an
array-valued column never changes shape just because its rows happen to be equal-length.

### Excel

```python
# cookbook:partial
df = qc.query_samples_to_dataframe(client.query, params)
qc.dataframe_to_excel(df, "cxi-shift.xlsx")
```

A thin wrapper over `to_excel()` that guards Excel's row ceiling, drops the timezone (Excel has no
tz-aware type), and stringifies complex cells.

### How values map

| Source | Result |
|---|---|
| Scalars (`double`, `long`, `string`, `bool`) | Native dtype |
| `timestampValue` | `datetime64[ns, UTC]` |
| Integer column **with gaps** | `float64`, gaps as `NaN` |
| `arrayValue` / `structureValue` | Python list / dict, in an object column |
| `byteArrayValue` | `bytes` |
| `imageValue` | `Image(data, file_type)` wrapper |

The integer upcast is worth remembering: a `long` column with a missing sample comes back as
`float64`, because NumPy integer arrays cannot hold `NaN`.

## Large queries: paging and streaming

Three ways to consume a query, in increasing order of scale:

**`query_samples()`** — one page.  Simple, and enough when you know the result is small.

**`iter_query_samples()`** — pages transparently, yielding one result per page:

```python
# cookbook:partial
for page in client.query.iter_query_samples(params):
    table = page.column_table
    if table is not None:
        print(f"page: {len(table.timestampList.timestamps)} rows")
```

**`iter_query_samples_stream()`** — server-streaming, lazy, no page tokens.  The right choice for
a long time range, since the server pushes results as it produces them:

```python
# cookbook:partial
for page in client.query.iter_query_samples_stream(params):
    table = page.column_table
    if table is not None:
        print(f"chunk: {len(table.timestampList.timestamps)} rows")
```

Both iterator forms **raise `RuntimeError`** on a mid-query error rather than returning a result
object — a generator has no way to hand back an error flag partway through, and this makes silent
truncation impossible.

To stream straight into DataFrames, one per chunk:

```python
# cookbook:partial
for frame in qc.stream_query_samples_to_dataframes(client.query, params):
    print(len(frame))
```

Remember that **`limit` is the page size**, not a total cap.  To stop early, break out of the loop
or use `itertools.islice`.

## Also worth knowing

- **Half-open range.**  `[begin_time, end_time)` — a sample exactly at `end_time` is excluded.
  Sample-oriented queries trim to the exact range, unlike bucket-oriented ones.
- **Serialized columns are deferred.**  The client always requests dense columns
  (`useSerializedColumns = False`).  A `ColumnTable` carrying `serializedDataColumns` raises
  `NotImplementedError` in the conversion layer.
- **Duplicate column names raise.**  Both conversions key columns by `DataColumn.name`, so a table
  with two identically-named columns raises `ValueError` rather than silently dropping one.
- **`valueStatus` is ignored.**  It is never populated in `querySamples()` results.
- **An empty result is not an error** — success with an empty table means nothing matched the
  range and selector.
- **Bucket-oriented queries (`queryBuckets`) are not yet wrapped** by this library; see
  [issue #16](https://github.com/osprey-dcs/dp-python-lib/issues/16).

### How far these examples have been verified

Worth knowing what stands behind the recipes on this page, since it differs from the rest of the
cookbook.

The request-building side was exercised against a live MLDP stack: the queries below are accepted
and well-formed.  What has **not** been observed end to end is the data path — rows coming back,
`ColumnTable` populating, a DataFrame with real samples in it.  There is currently no way to
ingest sample data from Python (`IngestionClient` exposes only `register_provider()`), so every
query here returned zero rows.  The DataFrame and NumPy output shown above is illustrative, and
the conversions themselves are covered by unit tests over hand-built `ColumnTable` objects.

[Issue #17](https://github.com/osprey-dcs/dp-python-lib/issues/17) adds the ingestion client, and
its Phase 3 is a closed-loop ingest→query round-trip.  **When that lands, re-verify this recipe
against real data and delete this note** — in particular, confirm the sample DataFrame output
above matches what a real query returns, and add the ingestion recipe this cookbook currently
lacks.
