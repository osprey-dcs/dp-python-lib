# Querying Time-Series Data

Retrieving archived PV samples over a time range — by name, by what the PVs *are*, or by what the
machine was *doing* — and getting the results into pandas or NumPy, either as one aligned table of
scalars or as the archive's [whole stored buckets](#whole-buckets-arrays-images-and-stored-metadata).

See [API conventions](conventions.md) for result checking and paging.  The samples queried here
are the ones [Ingesting data](ingestion.md) stores, and the metadata- and configuration-driven
queries read the catalogue built in [Cataloguing PVs](pv-metadata.md) and
[Recording machine configuration](machine-configuration.md).

All examples use `client.query`, which is `None` unless a query channel is configured.

> The v2 query API (`querySamples`, `PvSelector`, `common.TimeRange`) was added in 1.15.0 and
> is not available in earlier releases.

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
from dp_python_lib.client import bucket_conversions as bc
from dp_python_lib.client import data_frame_conversions as dfc
```

## Contents

- [Model](#model) — one `QueryParams`, three ways to choose PVs
- [Querying a known list of PVs](#querying-a-known-list-of-pvs)
- [Selecting PVs by metadata](#selecting-pvs-by-metadata) — "every BPM in GUNB"
- [Scoping a query to a machine configuration](#scoping-a-query-to-a-machine-configuration)
- [Getting results into pandas and NumPy](#getting-results-into-pandas-and-numpy)
- [Large queries: paging and streaming](#large-queries-paging-and-streaming)
- [Whole buckets: arrays, images, and stored metadata](#whole-buckets-arrays-images-and-stored-metadata)
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
configurations were **active**.  It narrows a PV selector; it does not replace one.

**A PV selector is required**, even with `config_criteria`: the server rejects a query without one,
so `QueryParams` refuses one up front.  `pv_selector` has no default, so leaving it out raises
`TypeError` (and a type checker flags it); passing `None` or an empty `PvSelector()` raises
`ValueError`.  To query every PV, say so with `PV.pattern(".*")` — see
[everything under one configuration](#everything-under-one-configuration).

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

Also rejected: no PV selector (even alongside `config_criteria`), and a negative `limit`.  Note
that **`limit=0` is meaningful** —
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

On both `PV.attr()` and `CFG.attr()` the values list is optional: omit it for a **key-only
existence search** matching everything that has the key, whatever its value — `PV.attr("AREA")`
selects every PV with an area recorded.

### The result covers several disjoint intervals

If a configuration was active more than once inside the time range — two shifts in a day, say —
the query resolves to **several disjoint windows**, not one span from the first start to the last
end.  Samples recorded in the gaps between activations are excluded, even where they fall inside
the outer `[begin_time, end_time)` range.

The returned rows are therefore not necessarily contiguous in time.  A DataFrame built from them
has a jump in its index at each gap, which matters if you resample or difference across it.

### Everything under one configuration

To get **every PV** recorded while a configuration was active, select all PVs explicitly:

```python
# cookbook:partial
params = QueryParams(
    begin_time=datetime(2026, 2, 2, 0, 0, tzinfo=timezone.utc),
    end_time=datetime(2026, 2, 3, 0, 0, tzinfo=timezone.utc),
    pv_selector=PV.pattern(".*"),
    config_criteria=[CFG.attr("DEST", ["CXI"])],
)
```

Occasionally what you want, but it can return a great deal of data: bound it with a tight time
range and a `limit`, and prefer the streaming form below.

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

The frame has a **UTC datetime index** and one column per PV, named by `DataColumn.name`.  For the
three BPM signals [the ingestion recipe](ingestion.md#ingesting-a-frame) stores at 10 kHz:

```
                                  BPMS:GUNB:314:TMIT  BPMS:GUNB:314:X  BPMS:GUNB:314:Y
2026-02-02 18:04:12+00:00                    1000.00             0.00             0.50
2026-02-02 18:04:12.000100+00:00             1000.01             0.01             0.51
2026-02-02 18:04:12.000200+00:00             1000.02             0.02             0.52
```

Columns need not come back in the order the selector listed them; here they arrived sorted by name.

**Sample query results carry no column metadata today.**  The conversions put any `ColumnMetadata`
a result carries into `df.attrs["column_metadata"]`, and `QueryParams` has an
`exclude_column_metadata` flag to suppress it, but the server's sample-query path populates none —
not the catalogue's tags and attributes, and not metadata ingested with the columns — so
`df.attrs` is empty.  Read the catalogue with
[`get_pv_metadata()`](pv-metadata.md#looking-up-a-single-pv) when you need it alongside the data.

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

**`iter_query_samples()`** — pages transparently, yielding one result per page.  The last page can
be empty: a full page comes with a token, and the server discovers there is nothing more only when
asked for the next one.

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

## Whole buckets: arrays, images, and stored metadata

Everything above is the **sample-oriented** query: one aligned table of scalar columns, trimmed to
the range.  The **bucket-oriented** query returns the archive's stored units instead.  Each
`DataBucket` holds one PV's column, in the type it was ingested as, over that bucket's own time
axis.  Reach for it when you need:

- **Array, image, struct, or serialized columns.**  `query_samples()` returns scalars only, so
  this is the way to read back what the [ingestion recipe](ingestion.md#arrays-images-and-structures)
  stores.
- **The column metadata stored with the data**, provenance included.  The sample path returns
  none; a bucket carries its column's `ColumnMetadata`.
- **The stored time axis** — a `SamplingClock` comes back as a clock, not expanded.

The methods take the same `QueryParams` and mirror the sample methods:
`query_buckets()` (one page), `iter_query_buckets()` (every page), and
`iter_query_buckets_stream()`.  The `bucket_conversions` module reads the results; its plain-Python
half needs no extras.

```python
# cookbook:partial
params = QueryParams(
    begin_time=datetime(2026, 2, 2, 18, 7, tzinfo=timezone.utc),
    end_time=datetime(2026, 2, 2, 18, 8, tzinfo=timezone.utc),
    pv_selector=PV.name_list(["BPMS:GUNB:314:WAVEFORM", "CAMR:GUNB:100:IMAGE"]),
)
for page in client.query.iter_query_buckets(params):
    for bucket in page.data_buckets:
        column = bc.bucket_column(bucket)           # the stored column message, whatever its type
        print(bucket.pvName, type(column).__name__, bucket.providerName)
        print("  at", bc.bucket_timestamps(bucket))  # epoch nanoseconds, exact
        print("  values", bc.bucket_values(bucket))  # one entry per sample
```

`bucket_values()` gives one entry per sample: a native scalar, an enum's integer code, a flat list
per array sample, or one `bytes` payload per image or struct sample.  The fields those samples
cannot be read without stay on the column, through the `data_frame_conversions` accessors:

```python
# cookbook:partial
for page in client.query.iter_query_buckets(params):
    for bucket in page.data_buckets:
        column = bc.bucket_column(bucket)
        print(dfc.column_dimensions(column))      # [3] for the waveform; None for an image
        print(dfc.image_descriptor_dict(column))  # width, height, channels, encoding; None otherwise
        print(dfc.column_schema_id(column))       # a struct's schemaId; None otherwise
        print(dfc.column_metadata_dict(column))   # tags, attributes, provenance
```

`bc.bucket_to_data_frame(bucket)` views a bucket as a one-column `common.DataFrame`, so any of the
[calculations readers](datasets-and-annotations.md) work on it too.

### Buckets come back whole

The server returns **every bucket that overlaps `[begin_time, end_time)`, untrimmed**.  A query
for a quarter of a second inside a one-second bucket returns the whole second.  Trimming is
opt-in, exact, and half-open like `query_samples()`:

```python
# cookbook:partial
begin = datetime(2026, 2, 2, 18, 4, 12, 250_000, tzinfo=timezone.utc)
end = datetime(2026, 2, 2, 18, 4, 12, 500_000, tzinfo=timezone.utc)
params = QueryParams(begin_time=begin, end_time=end, pv_selector=PV.name_list(["BPMS:GUNB:314:X"]))

for page in client.query.iter_query_buckets(params):
    for bucket in page.data_buckets:
        trimmed = bc.trim_bucket(bucket, begin, end)   # None if no sample falls in the range
        if trimmed is not None:
            print(len(bc.bucket_timestamps(bucket)), "->", len(bc.bucket_timestamps(trimmed)))
            # 10000 -> 2500
```

A trimmed `SamplingClock` bucket stays a clock, with its start moved to the first sample kept.

**Trimming cannot remove configuration gaps.**  With `config_criteria`, a bucket spanning a gap
between [two activations](#the-result-covers-several-disjoint-intervals) is returned once, whole,
with the samples in the gap still in it, and trimming to `[begin_time, end_time)` leaves them
there.  Use `query_samples()` when that matters.

### One DataFrame per PV

With the `[analysis]` extra, `query_buckets_to_dataframes()` runs the whole query and returns a
`dict` of PV name to DataFrame.  A bucket result is not one table — each PV has its own axis and
possibly its own column type — so there is no single-frame form.

```python
# cookbook:partial
frames = bc.query_buckets_to_dataframes(client.query, params, trim=True, max_buckets=10_000)
x = frames["BPMS:GUNB:314:X"]
print(len(x), x.attrs["buckets"][0]["provider_name"])
```

Each frame has a UTC index and one column, named for the PV, with the dtype its stored type
implies.  `df.attrs["buckets"]` describes every bucket in the frame (time span, sample count,
provider, column metadata); `df.attrs["column_metadata"]` is present only when every bucket agrees.
Array dims, image descriptors, struct schema ids, and enum ids are in `df.attrs` too.

A PV's buckets are kept as stored: two ingests that overlap in time both appear, so the index can
repeat instants.  A PV whose buckets differ in column type or structure — ingested as doubles one
day and int32 the next, say — raises `ValueError` naming both, rather than being coerced.

### Paging, streaming, and what `limit` counts

On this path **`limit` counts buckets, not rows**.  A page can also end early, with a page token,
once it reaches the server's message-size budget, and the server silently caps `limit` at its
configured maximum (100,000 by default).  `iter_query_buckets()` follows the tokens for you.

The stream has no tokens.  A PV's buckets can arrive across several messages, so collect them all
and convert once:

```python
# cookbook:partial
buckets = []
for message in client.query.iter_query_buckets_stream(params):
    buckets.extend(message.data_buckets)
frames = bc.buckets_to_dataframes(buckets, time_range=(params.begin_timestamp, params.end_timestamp))
```

### Two refusals

- **A `sample_status_filter` is refused** with a `ValueError` before any call is made.  The server
  rejects status filtering on bucket queries, and dropping the filter would hand back data you
  believed was filtered.  Filter with `query_samples()`, or drop the filter yourself.
- **A serialized column is passed through, never decoded.**  It comes back only if it was ingested
  as one (`dfb.serialized_column()`).  `bc.bucket_column(bucket)` returns it with its `encoding`
  and `payload`; `bucket_values()` and the DataFrame conversions raise `ValueError` rather than
  drop it.

## Also worth knowing

- **Half-open range.**  `[begin_time, end_time)` — a sample exactly at `end_time` is excluded.
  Sample-oriented queries trim to the exact range; bucket-oriented ones return
  [whole buckets](#buckets-come-back-whole).
- **Serialized sample results are deferred.**  The client always requests dense columns
  (`useSerializedColumns = False`).  A `ColumnTable` carrying `serializedDataColumns` raises
  `NotImplementedError` in the conversion layer.  (A column *ingested* serialized is a different
  thing; read it back with a [bucket query](#two-refusals).)
- **Duplicate column names raise.**  Both conversions key columns by `DataColumn.name`, so a table
  with two identically-named columns raises `ValueError` rather than silently dropping one.
- **`valueStatus` is gone.**  `DataValue.valueStatus` was removed in dp-grpc 1.16.0 (field 15 is
  reserved) in favor of the [sample status API](sample-status.md), which also lets a query exclude
  flagged samples outright.  It was never populated in `querySamples()` results before that.
- **An empty result is not an error** — success with an empty table means nothing matched the
  range and selector.

### How far these examples have been verified

The examples on this page were re-run against a live MLDP stack holding the data
[Ingesting data](ingestion.md) stores: one second of `BPMS:GUNB:314:X`, `:Y`, and `:TMIT` at
10 kHz, catalogued as in [Cataloguing PVs](pv-metadata.md), under a `cxi-production` activation
from 17:00 to 23:00.  The name-list, pattern, metadata (`DEVICE`, and `AREA` with `TYPE`), and
configuration (by name and by `EXP`) queries each returned those samples; the DataFrame above is
real output; and paging, streaming, `query_samples_to_dataframe()`, and
`stream_query_samples_to_dataframes()` agreed on the row count.

`tests/integration/test_query_client_integration.py` pins the data path itself: values and
timestamps round-trip exactly, the range is half-open at both bounds, columns on different clocks
align with gaps rather than zeros, and pages at a small `limit` concatenate in order.

The [whole buckets](#whole-buckets-arrays-images-and-stored-metadata) examples were run the same
way, straight after the ingestion recipe's frames: the array, image, and struct read-back printed
what the ingestion recipe shows, and the quarter-second trim cut a 10,000-sample bucket to 2,500.
`tests/integration/test_query_buckets_integration.py` pins the rest: a clock axis returned
verbatim, whole boundary buckets, `limit` counting buckets across pages, the stream matching the
unary pages, and column metadata read back and excluded.
