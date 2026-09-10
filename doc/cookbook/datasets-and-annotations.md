# DataSets and Annotations

Naming a region of the archive so you can find it again, attaching derived values to it with a
record of what they were computed from, and exporting the result.

> **Target API version:** dp-grpc 1.16.0, which is **not yet released** — the newest tag is
> `rel-1.15.0`.  The modernized DataSet / Annotation / Export API is new in 1.16.0 and will not work
> against a `rel-1.15.0` server, which answers these calls with `UNIMPLEMENTED`.
>
> **Verified against:** a dp-service build from `main` at commit `fddf692`, carrying the 1.16.0 API.

See [API conventions](conventions.md) for result checking, paging, and time handling.

Examples use `client.annotation.datasets`, `client.annotation.annotations`, and
`client.annotation.export`.  Note that `client.annotation` itself is `None` unless an annotation
channel is configured, so guard on it before reaching through.

### Imports used by the examples

```python
# cookbook:skip
from datetime import datetime, timezone

from dp_python_lib.client import (
    MldpClient,
    SaveDataSetRequestParams,
    SaveAnnotationRequestParams,
    ExportDataRequestParams,
    ExportFormat,
    DataSetQuery as DS,
    AnnotationQuery as AQ,
    data_block,
    calculations,
    calculations_spec,
    sampling_clock,
)
from dp_python_lib.client import data_frame as dfb
from dp_python_lib.client import data_frame_conversions as dfc
```

## Contents

- [Model](#model) — three objects and how they relate
- [Naming a region of the archive](#naming-a-region-of-the-archive)
- [Finding datasets again](#finding-datasets-again)
- [Attaching an analysis result](#attaching-an-analysis-result)
- [Recording where the numbers came from](#recording-where-the-numbers-came-from)
- [Reading calculations back](#reading-calculations-back)
- [Going through pandas](#going-through-pandas)
- [Updating without losing the calculations](#updating-without-losing-the-calculations)
- [Exporting](#exporting)
- [Tearing down](#tearing-down)
- [Also worth knowing](#also-worth-knowing)

## Model

Three objects, in a chain:

A **DataSet** names a region of the archive: a list of **DataBlocks**, each one a time range plus
the PVs covered over it.  It holds no data — only the coordinates of data that already exists.

An **Annotation** describes one or more DataSets: a name, a description, tags, and optionally a
**Calculations** payload of derived values.  Annotations are how analysis results get attached to
the raw data they came from.

**Calculations** are named frames, each one a time axis plus typed columns of values, with
optional per-column **provenance** recording what each column was derived from.

```
DataSet  ──described by──>  Annotation  ──owns──>  Calculations
(where the                  (what you              (the derived
 data is)                    concluded)             numbers)
```

Two consequences worth internalizing before you start.

**A DataSet can only name PVs that already have archived data.**  `save_dataset()` checks that
every PV in every data block exists in the archive — not merely that it has PV metadata saved.  A
dataset over a PV that has never been ingested is rejected:

```
no PV metadata found for names: [BPMS:GUNB:314:X]
```

That message is misleading: the check is against ingested data, not the metadata catalogue.  If you
see it for a PV you know you catalogued, the PV has no samples.

**Calculations belong to their annotation.**  They have no independent lifecycle: saving an
annotation is the only way to write them, deleting the annotation deletes them, and re-saving an
annotation without them deletes them too.  See
[updating without losing the calculations](#updating-without-losing-the-calculations).

## Naming a region of the archive

A `DataBlock` is one time range and the PVs measured over it.  Build one per (range, PV list) pair;
a dataset is a list of them, so a study spanning two disjoint windows is one dataset with two
blocks.

```python
# cookbook:partial
t0 = datetime(2026, 2, 2, 18, 0, tzinfo=timezone.utc)
t1 = datetime(2026, 2, 2, 19, 0, tzinfo=timezone.utc)

saved = client.annotation.datasets.save_dataset(SaveDataSetRequestParams(
    name="CXI shift, hour 1",
    owner_id="cmcchesney",
    data_blocks=[data_block(t0, t1, ["BPMS:GUNB:314:X", "BPMS:GUNB:314:Y"])],
    description="First hour of the CXI_3443 shift, both transverse BPM planes.",
    tags=["cxi-3443", "shift-study"],
    attributes={"EXP": "CXI_3443", "DEST": "CXI"},
    modified_by="cmcchesney",
))
if saved.result_status.is_error:
    raise RuntimeError(saved.result_status.message)

dataset_id = saved.dataset_id
assert dataset_id is not None      # guaranteed once is_error is False; accessors are Optional
print(dataset_id)                  # server-assigned id, e.g. '6aa1bb271a768e97db44d426'
```

`data_block()` requires `begin < end` and a non-empty PV list.  That check exists here because the
server does not make it: it validates only that each bound is non-zero and never compares the two,
so a reversed block would otherwise be stored happily.  It also rejects a bare string for the PV
list, which would otherwise be iterated into one PV name per character.

A block's range is [half-open](conventions.md#half-open-ranges), `[begin, end)`, like every other
range in the library, so back-to-back blocks — one ending at `T`, the next starting at `T` — cover
the sample at `T` exactly once.  `saveDataSet` itself never compares the bounds; the interval only
acquires meaning at export, where it reaches the same per-sample trimming that `query_samples()`
does.

**HDF5 export is the exception.**  It is bucket-granular: every bucket that *overlaps* the block is
written whole and untrimmed, so an HDF5 file can contain samples outside the range you asked for,
and back-to-back blocks sharing a straddling bucket write it twice.  CSV and XLSX trim to the exact
range.  Nothing client-side can change this — it is a property of the export format.

Note the tags come back **normalized** — lowercased, deduplicated, and sorted — so a tag saved as
`CXI-3443` reads back as `cxi-3443`, and queries must match the lowercase form.

## Finding datasets again

Seven criteria, combined with the usual [AND across / OR within](conventions.md#query-criteria)
rules:

```python
# cookbook:partial
# Everything from this experiment, in this area of the machine.
for dataset in client.annotation.datasets.iter_datasets([
    DS.attributes("EXP", ["CXI_3443"]),
    DS.pv_names(["BPMS:GUNB:314:X"]),
]):
    print(dataset.name, len(dataset.dataBlocks))
```

Two criteria behave differently from the older helpers in this library:

**`attributes()` takes an optional value list.**  Omit it to ask *"does this key exist at all?"*,
regardless of value:

```python
# cookbook:partial
# Every dataset tagged with an experiment, whichever one.
tagged = list(client.annotation.datasets.iter_datasets([DS.attributes("EXP")]))
```

**`criteria` itself is optional.**  An empty or omitted list matches everything, so browsing the
whole collection is a legitimate call rather than something to work around:

```python
# cookbook:partial
for dataset in client.annotation.datasets.iter_datasets():
    print(dataset.id, dataset.name)
```

`text()` is a full-text search across the name and description together.  Only **one** text
criterion is allowed per query — two cannot be ANDed — and the client rejects a second one before
the RPC:

```python
# cookbook:partial
# One text criterion: fine.
found = list(client.annotation.datasets.iter_datasets([DS.text("shift")]))

# Two: ValueError, naming the rule.  Combine the terms into one search instead.
```

### Fetching many datasets at once

Annotations carry `dataSetIds`, not the datasets themselves.  Resolving them one call at a time is
an N+1, so `get_datasets()` does it in a single paged query:

```python
# cookbook:partial
ids = [i for a in client.annotation.annotations.iter_annotations([AQ.tags(["reviewed"])])
       for i in a.dataSetIds]
datasets = client.annotation.datasets.get_datasets(ids)      # dict: id -> DataSet

for dataset_id, dataset in datasets.items():
    print(dataset_id, dataset.name)
```

Ids are deduplicated, an empty list returns `{}` without an RPC, and ids that resolve to nothing
are simply **absent from the dict** rather than raising — a dangling `dataSetIds` entry is a normal
consequence of deletion, not an error.

## Attaching an analysis result

An annotation on its own records a conclusion:

```python
# cookbook:partial
result = client.annotation.annotations.save_annotation(SaveAnnotationRequestParams(
    name="Orbit drift during CXI_3443",
    owner_id="cmcchesney",
    dataset_ids=[dataset_id],
    description="Slow horizontal drift, ~0.3 mm over the hour. Suspect thermal.",
    tags=["reviewed", "orbit"],
    modified_by="cmcchesney",
))
if result.result_status.is_error:
    raise RuntimeError(result.result_status.message)

annotation_id = result.annotation_id
assert annotation_id is not None
```

To attach *numbers*, build a `Calculations` payload.  A frame is one time axis plus its columns:

```python
# cookbook:partial
# Your analysis output: one value per sample, however you computed it.
x_rms_values = [0.31, 0.29, 0.33]        # in reality, 3600 of them
y_rms_values = [0.12, 0.14, 0.11]

# A 1 Hz RMS series: one sample per second, count matching the values above.
axis = sampling_clock(start_time=t0, period_nanos=1_000_000_000, count=len(x_rms_values))

frame = dfb.data_frame(axis, [
    dfb.double_column("x_rms", x_rms_values),
    dfb.double_column("y_rms", y_rms_values),
])

result = client.annotation.annotations.save_annotation(SaveAnnotationRequestParams(
    name="1 Hz orbit RMS, CXI_3443 hour 1",
    owner_id="cmcchesney",
    dataset_ids=[dataset_id],
    tags=["reviewed"],
    calculations=calculations({"orbit-rms": frame}),
    modified_by="cmcchesney",
))
if result.result_status.is_error:
    raise RuntimeError(result.result_status.message)

# No annotation_id was passed, so this SAVED A SECOND ANNOTATION rather than adding calculations to
# the one above.  Rebind both handles: the rest of this recipe works with the annotation that owns
# the calculations.  To attach them to the first annotation instead, pass annotation_id= and the
# fields to carry forward -- see "Updating without losing the calculations" below.
annotation_id = result.annotation_id
calculations_id = result.calculations_id
assert annotation_id is not None and calculations_id is not None
```

`calculations()` takes a **dict** of frame name to frame, which makes frame-name uniqueness true by
construction — the server rejects duplicates, and a list would let you build one.

The column builders validate the shape before anything goes over the wire: every column needs a
non-blank name and a value count matching the axis, and names must be unique **across all column
types** in a frame. A mismatch names the offending column:

```
data_frame() column 'x_rms' has 3599 values but the time axis has 3600 timestamps;
every column must carry exactly one value per sample
```

Size limits — string length, array element counts, image and struct bytes — are deliberately *not*
checked here. Those are deployment policy and can change; the server enforces them.

### Columns with gaps

The typed columns are dense: a `DoubleColumn` has one value per sample, with no way to mark one
absent. When a column genuinely has holes, use `data_column()`, whose values are `DataValue`
messages that can be left unset:

```python
# cookbook:partial
# None becomes a genuinely absent value, not a fabricated zero.
sparse = dfb.data_column("beam_current", [12.7, None, 12.9])
```

The alternative — and often the better one — is to give the sparse column **its own frame** over
only the timestamps where it has values, built with `timestamp_list()`. A frame per sampling rate
is the natural shape when your columns genuinely differ.

## Recording where the numbers came from

A calculated column without provenance is a number nobody can check later. `ColumnMetadata` records
what a column was derived from, per column:

```python
# cookbook:partial
metadata = dfb.column_metadata(
    tags=["derived"],
    attributes={"unit": "mm"},
    provenance=dfb.provenance(
        source="orbit-analysis-rig",
        process="1 Hz RMS over 10 kHz BPM samples",
        derived_from=[dfb.pv_source("BPMS:GUNB:314:X", (t0, t1))],
    ),
)

axis = sampling_clock(start_time=t0, period_nanos=1_000_000_000, count=3)
x_rms_values = [0.31, 0.29, 0.33]

frame = dfb.data_frame(axis, [dfb.double_column("x_rms", x_rms_values, metadata=metadata)])
```

`pv_source()` names an archived PV; `calculations_source()` names a column of *other* calculations,
for values derived from earlier derived values:

```python
# cookbook:partial
chained = dfb.provenance(
    process="10-minute moving average of the 1 Hz RMS",
    derived_from=[dfb.calculations_source(calculations_id, "orbit-rms", "x_rms")],
)
```

Both accept an optional `(begin, end)` pair narrowing which part of the source was used.

Reading it back, `column_metadata_dict()` gives you the plain-Python view — no extras needed. Each
entry carries **only the origin arm actually set**, plus the time range when there is one, reported
as epoch nanoseconds like every other instant in this library:

```python
# cookbook:partial
provenance = dfb.provenance(derived_from=[dfb.pv_source("BPMS:GUNB:314:X", (t0, t1))])
column = dfb.double_column("x_rms", [0.31, 0.29, 0.33], metadata=dfb.column_metadata(provenance=provenance))

summary = dfc.column_metadata_dict(column)
print(summary["provenance"]["derived_from"])
# [{'pv_name': 'BPMS:GUNB:314:X', 'time_range': (1770055200000000000, 1770058800000000000)}]
```

A PV source has no `calculations_column` key and a calculations source has no `pv_name` key, rather
than the unset one showing up as an empty string — absence means "not this arm", the same
[absent-vs-empty](conventions.md) discipline the rest of the library follows. A source with no time
range simply has no `time_range` key, never a fabricated `(0, 0)`.

These are **soft references**. Deleting the annotation that owns the referenced calculations leaves
the link dangling; nothing resolves or cleans it up, and readers are expected to tolerate that.

## Reading calculations back

`get_annotation()` is the only method that returns calculations **inline**:

```python
# cookbook:partial
read = client.annotation.annotations.get_annotation(annotation_id)
if read.result_status.is_error:
    raise RuntimeError(read.result_status.message)

calcs = read.calculations          # None if the annotation has none
```

`query_annotations()` deliberately does not: results carry the `calculationsId` with the content
left empty, so listing a hundred annotations does not drag a hundred payloads with it. Follow the
id when you want the content:

```python
# cookbook:partial
for annotation in client.annotation.annotations.iter_annotations([AQ.tags(["reviewed"])]):
    if not annotation.calculationsId:
        continue
    fetched = client.annotation.annotations.get_calculations(annotation.calculationsId)
    if fetched.result_status.is_error:
        raise RuntimeError(fetched.result_status.message)
    payload = fetched.calculations
    assert payload is not None
    print(annotation.name, len(payload.calculationDataFrames))
```

Without pandas, `data_frame_conversions` returns plain Python:

```python
# cookbook:partial
calcs = client.annotation.annotations.get_annotation(annotation_id).calculations
assert calcs is not None

entry = calcs.calculationDataFrames[0]

epoch_nanos = dfc.data_frame_timestamps(entry.frame)   # list[int], exact
columns = dfc.data_frame_columns(entry.frame)          # {"x_rms": [...], "y_rms": [...]}

print(entry.name, len(epoch_nanos), list(columns))
```

Timestamps are computed in **integer nanoseconds** throughout. A `SamplingClock` expands as
`startTime + i * periodNanos` with no float step anywhere: present-day epoch nanoseconds need about
61 bits, and a float64 carries 53, so routing through float seconds would silently move every
timestamp by hundreds of nanoseconds.

## Going through pandas

With the [`analysis` extra](conventions.md#optional-dependencies) installed, whole frames convert
in one call:

```python
# cookbook:partial
calcs = client.annotation.annotations.get_annotation(annotation_id).calculations

frames = dfc.calculations_to_dataframes(calcs)    # {"orbit-rms": DataFrame}
df = frames["orbit-rms"]

print(df.index.tz)                                # UTC
print(df.attrs["column_metadata"]["x_rms"]["provenance"]["process"])
```

The index is a UTC `DatetimeIndex` built directly from int64 nanoseconds, and per-column metadata
lands in `df.attrs["column_metadata"]` — the same convention the
[query recipe](query.md) uses.

The reverse direction turns analysis output back into a payload:

```python
# cookbook:partial
calcs = client.annotation.annotations.get_annotation(annotation_id).calculations
df = dfc.calculations_to_dataframes(calcs)["orbit-rms"]

calcs_to_save = dfc.calculations_from_dataframes({"orbit-rms": df})
```

Two things to know about it.

**Missing values are rejected, loudly.** A dense typed column cannot represent a gap, so a `NaN`
anywhere raises rather than inventing a value or dropping a row:

```
column 'x_rms' contains missing values (first at row position(s) [17]), which a dense typed
column cannot represent.  Either give the sparse column its own frame over only the timestamps
where it has values, or build it with data_frame.data_column(), whose DataValues can be left unset.
```

**The index always becomes an explicit `TimestampList`**, never an inferred `SamplingClock`. A
clock is only correct if the spacing is exactly uniform at nanosecond precision, and inferring that
from an index that merely *looks* regular would quietly change your timestamps. When the data
really is a regular clock, say so with `sampling_clock()` and build the frame directly.

The dtype mapping is `float64`→Double, `float32`→Float, `int64`→Int64, `int32`→Int32, `bool`→Bool,
and object/string→String. Anything else — complex, datetime columns, categoricals — raises rather
than guessing. Duplicate column names are rejected up front: a frame stores one column per name, and
a `concat` or a `merge` with overlapping names produces duplicates easily.

**A round trip preserves values, but not column order.** A `DataFrame` keeps each column kind in its
own repeated field, so the wire format has no single ordering across kinds. Converting out and back
returns every value, dtype, and timestamp intact, with the columns grouped by type:

```python
# cookbook:partial
calcs = client.annotation.annotations.get_annotation(annotation_id).calculations
df = dfc.calculations_to_dataframes(calcs)["orbit-rms"]

restored = dfc.data_frame_to_pandas(dfc.data_frame_from_pandas(df))
restored = restored[list(df.columns)]     # reindex by name to get your order back
```

Select by name rather than by position, and reindex when the order matters.

## Updating without losing the calculations

`save_annotation()` is a [full replace](conventions.md#save-semantics-full-replace), and that
**includes the calculations**. Re-saving an annotation without them does not leave them alone: it
clears the reference and deletes the stored object.

```python
# cookbook:partial
# WRONG -- silently deletes the calculations this annotation owned
client.annotation.annotations.save_annotation(SaveAnnotationRequestParams(
    name="Orbit drift during CXI_3443 (revised)",
    owner_id="cmcchesney",
    dataset_ids=[dataset_id],
    annotation_id=annotation_id,
))
```

Read the current state and carry the calculations forward:

```python
# cookbook:partial
read = client.annotation.annotations.get_annotation(annotation_id)
if read.result_status.is_error:
    raise RuntimeError(read.result_status.message)

existing = read.annotation
assert existing is not None          # guaranteed once is_error is False

replaced = client.annotation.annotations.save_annotation(SaveAnnotationRequestParams(
    name="Orbit drift during CXI_3443 (revised)",
    owner_id=existing.ownerId,
    dataset_ids=list(existing.dataSetIds),
    annotation_ids=list(existing.annotationIds),
    description=existing.description,
    tags=list(existing.tags),
    attributes={a.name: a.value for a in existing.attributes},
    calculations=read.calculations,   # carried forward
    annotation_id=annotation_id,
    modified_by="cmcchesney",
))
if replaced.result_status.is_error:
    raise RuntimeError(replaced.result_status.message)

# Carrying calculations through a replace stores a NEW object and deletes the old one, so the id
# you were holding is now dangling.  Rebind it, or a later export/read will fail.
calculations_id = replaced.calculations_id
assert calculations_id is not None
```

A replace that *does* carry new calculations returns a **new** `calculations_id`; the previous
object is deleted, not orphaned. An annotation saved with no calculations at all reports
`calculations_id == ""` — empty string, not `None`. `None` means the call failed.

## Exporting

`export_data()` writes a file **on the server**, in HDF5, CSV, or XLSX. Sources merge: a saved
dataset by id, ad-hoc data blocks, calculations, or any combination — at least one is required.

```python
# cookbook:partial
export = client.annotation.export.export_data(ExportDataRequestParams(
    ExportFormat.HDF5,
    dataset_id=dataset_id,
    calculations_spec=calculations_spec(calculations_id),
))
if export.result_status.is_error:
    raise RuntimeError(export.result_status.message)

print(export.file_path)     # a path on the SERVER's filesystem
print(export.file_url)      # '' unless the deployment publishes exports over HTTP
```

`ExportFormat` accepts either the member or its string value, so `ExportFormat.CSV` and
`"csv"` both work; anything else raises a `ValueError` naming the valid formats, which is what
makes the server-rejected `EXPORT_FORMAT_UNSPECIFIED` unreachable.

For a one-off export of a region you do not want to keep, skip the dataset entirely:

```python
# cookbook:partial
client.annotation.export.export_data(ExportDataRequestParams(
    "csv",
    data_blocks=[data_block(t0, t1, ["BPMS:GUNB:314:X"])],
))
```

`calculations_spec()` narrows what gets included. Omit `frame_columns` for everything; supply it to
pick specific columns of specific frames, excluding any frame you do not mention:

```python
# cookbook:partial
spec = calculations_spec(calculations_id, {"orbit-rms": ["x_rms"]})
```

**There is no download.** `file_path` is a path on the server's filesystem, and no RPC retrieves
the file — so this library offers no download convenience rather than pretending otherwise. How you
collect the file is a deployment question: a shared mount, `file_url` if the deployment publishes
over HTTP, or an out-of-band copy.

Two rejections to expect: the tabular formats (CSV, XLSX) can only represent **scalar** columns, so
exporting array, image, or struct columns to them fails — use HDF5. And an unknown dataset or
calculations id is rejected rather than producing an empty file.

## Tearing down

Order matters. A dataset cannot be deleted while any annotation references it:

```python
# cookbook:partial
refused = client.annotation.datasets.delete_dataset(dataset_id)
print(refused.result_status.is_error)      # True, naming a referencing annotation
```

There is deliberately no `cascade=True`. Deleting a dataset and every annotation about it is two
destructive operations behind one flag; the honest shape is the two-step the server designed:

```python
# cookbook:partial
for annotation in client.annotation.annotations.iter_annotations([AQ.datasets([dataset_id])]):
    deleted = client.annotation.annotations.delete_annotation(annotation.id)
    if deleted.result_status.is_error:
        raise RuntimeError(deleted.result_status.message)

client.annotation.datasets.delete_dataset(dataset_id)
```

Deleting an annotation **cascades to its calculations**. It does not clean up incoming references:
other annotations' `annotationIds` entries, and `derivedFrom` provenance links naming the deleted
calculations, are left dangling.

## Also worth knowing

- **Deleting something that does not exist is an error**, not a silent success. Both
  `delete_dataset()` and `delete_annotation()` report `no ... record found for id: ...` on a second
  delete, so a teardown that runs twice reports failures the second time.
- **`updatedTime` is unset on create.** It appears once a record has been replaced at least once.
- **Ids are ObjectIds.** A malformed id is rejected as malformed rather than reported as "not
  found"; the distinction is useful when debugging.
- **Page tokens are opaque keyset tokens**, and a malformed or wrong-query one is **rejected**.
  This differs from the older metadata queries, which silently restart from the beginning
  ([dp-service #193](https://github.com/osprey-dcs/dp-service/issues/193)). Do not construct or
  reuse tokens across queries.
- **`patchDataSet` and `patchAnnotation` are not wrapped.** They are reserved placeholders that
  return "not implemented"; use the full-replace `save_*` methods.
- **Array, image, and struct column builders do not exist yet.** `data_frame.py` covers the typed
  scalar columns and the `DataColumn` escape hatch; the rest are
  [issue #17](https://github.com/osprey-dcs/dp-python-lib/issues/17)'s to design with ingestion data
  in hand. Meanwhile, build those column protos directly and pass them to `data_frame()`, which
  accepts pre-built columns alongside the ones its builders return.
- **Serialized columns are skipped on read.** `data_frame_columns()` ignores
  `serializedDataColumns`, whose payloads this library does not decode; read the field directly if
  you need it.

### How far these examples have been verified

The dataset and annotation lifecycle **has** been exercised against a live Annotation Service built
from dp-service `main` at `fddf692` (the 1.16.0 API, pre-release) by
`tests/integration/test_datasets_annotations_integration.py`: save/get round trips,
every criterion including the key-only attribute search, lowercase tag normalization, paging,
rejected page tokens, the `get_datasets()` batch fetch, calculations inline on `get_annotation()`
versus id-only on `query_annotations()`, the full-replace clearing behavior, the delete cascade, and
the referenced-dataset refusal.

That test ingests its own samples first, because of the archive-existence rule described in
[Model](#model) — there is no ingestion client yet
([issue #17](https://github.com/osprey-dcs/dp-python-lib/issues/17)), so it uses the generated stub
directly.

The `x_rms_values` in these examples stand in for real analysis output. The **numbers** are
illustrative; the calls around them are the verified part.
