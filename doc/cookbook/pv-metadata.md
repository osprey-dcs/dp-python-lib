# Cataloguing PVs

Recording what a PV *is* — the device it belongs to, where it sits in the machine, what kind of
element it measures — and then finding PVs by those properties instead of by name.

> **Verified against:** dp-python-lib 1.15.0.
> The PV metadata API is unchanged since 1.14.0; these recipes apply to both.

See [API conventions](conventions.md) for result checking, paging, and criteria rules, and
[Creating and connecting a client](connecting.md) for getting a client in the first place.

All examples use `client.annotation.pv_metadata`, which is `None` unless an annotation channel is
configured.

### Imports used by the examples

```python
# cookbook:skip
from dp_python_lib.client import (
    MldpClient,
    SavePvMetadataRequestParams,
    PvMetadataQuery as Q,
)
```

## Contents

- [Model](#model) — the catalogue, and tags vs. attributes
- [Cataloguing a device](#cataloguing-a-device)
- [Cataloguing the sibling PVs of one device](#cataloguing-the-sibling-pvs-of-one-device)
- [Updating without losing data](#updating-without-losing-data) — the full-replace trap
- [Looking up a single PV](#looking-up-a-single-pv)
- [Finding PVs by what they are](#finding-pvs-by-what-they-are)
- [Deleting](#deleting)
- [Also worth knowing](#also-worth-knowing)

## Model

PV metadata is a **catalogue**, separate from the time-series data itself.  One record per PV,
keyed by `pvName`, holding:

| Field | Type | Purpose |
|---|---|---|
| `pvName` | `str` | Primary key |
| `aliases` | list of `str` | Alternate names; usable anywhere a PV name is accepted |
| `tags` | list of `str` | Flat labels — set membership |
| `attributes` | `dict[str, str]` | Key/value properties |
| `description` | `str` | Free text |
| `modifiedBy` | `str` | Who last changed it |

Cataloguing is optional — you can ingest and query PV data without it — but it is what makes
[metadata-driven queries](query.md#selecting-pvs-by-metadata) possible: *"every BPM in GUNB"*
rather than a hand-maintained list of PV names.

### Tags or attributes?

Both are searchable, and the difference is what question they answer:

- **Attributes** describe *what the device is*: `TYPE=MONI`, `AREA=GUNB`, `S=0.489650`.  Key/value,
  matched exactly.
- **Tags** describe *how it is being used*: `production`, `commissioning`.  Flat membership, no
  value.

If a property has a value you would want to read back, it is an attribute.  If it is a yes/no
label you would want to filter on, it is a tag.

**Values are always strings.**  A numeric property like `S=0.489650` is stored as the string
`"0.489650"`, and matched as a string — see
[a note on numeric attributes](#a-note-on-numeric-attributes).

## Cataloguing a device

A beam position monitor in the GUNB area, described the way the accelerator model sees it:

```python
# cookbook:partial
result = client.annotation.pv_metadata.save_pv_metadata(SavePvMetadataRequestParams(
    pv_name="BPMS:GUNB:314:X",
    attributes={
        "DEVICE": "BPMS:GUNB:314",   # the physical device this PV belongs to
        "ELEMENT": "BPM1B",          # accelerator model element name
        "TYPE": "MONI",              # MAD-style element type
        "AREA": "GUNB",              # machine area
        "Z": "-9.555017",            # global position
        "S": "0.489650",             # position along the beamline
    },
    tags=["production"],
    description="Beam position monitor, GUNB area, horizontal",
    modified_by="catalog-loader",
))

if result.result_status.is_error:
    raise RuntimeError(f"save failed: {result.result_status.message}")

print(result.pv_name)     # 'BPMS:GUNB:314:X'
```

`ELEMENT`, `TYPE`, `Z`, and `S` come from the accelerator model; `AREA` from the machine layout.
The `production` tag is **illustrative** — substitute whatever operational labels your facility
uses, or omit tags entirely.

`save_pv_metadata()` is an **upsert**: it creates the record if the PV is new and replaces it if
it already exists.  See [Updating without losing data](#updating-without-losing-data) before
using it on an existing record.

### Aliases

If a PV is known by more than one name, record the alternates as aliases:

```python
# cookbook:partial
client.annotation.pv_metadata.save_pv_metadata(SavePvMetadataRequestParams(
    pv_name="BPMS:GUNB:314:X",
    aliases=["BPM1B:X", "GUNB-BPM1-X"],
    attributes={"DEVICE": "BPMS:GUNB:314", "AREA": "GUNB", "TYPE": "MONI"},
    modified_by="catalog-loader",
))
```

Aliases work anywhere a PV name is accepted by `get_pv_metadata()` and `delete_pv_metadata()`, so
a lookup by legacy name resolves to the same record.

## Cataloguing the sibling PVs of one device

A single BPM reports several measurements, each its own PV.  They share a `DEVICE` and a
position, and differ in what they measure:

```python
# cookbook:partial
shared = {
    "DEVICE": "BPMS:GUNB:314",
    "ELEMENT": "BPM1B",
    "TYPE": "MONI",
    "AREA": "GUNB",
    "Z": "-9.555017",
    "S": "0.489650",
}

for suffix, description in [
    ("X", "horizontal position"),
    ("Y", "vertical position"),
    ("TMIT", "transmitted charge"),
]:
    result = client.annotation.pv_metadata.save_pv_metadata(SavePvMetadataRequestParams(
        pv_name=f"BPMS:GUNB:314:{suffix}",
        attributes=shared,
        tags=["production"],
        description=f"BPM1B {description}",
        modified_by="catalog-loader",
    ))
    if result.result_status.is_error:
        raise RuntimeError(f"save failed for {suffix}: {result.result_status.message}")
```

Sharing `DEVICE` across the three is what lets you later retrieve every signal from one physical
device with a single criterion.

## Updating without losing data

`save_pv_metadata()` is a **full replace**, not a partial update.  Whatever you send becomes the
entire record; anything you omit is erased.

This is the single easiest way to lose catalogue data:

```python
# cookbook:partial
# WRONG -- this PV now has ONE attribute, and no tags, aliases, or description
client.annotation.pv_metadata.save_pv_metadata(SavePvMetadataRequestParams(
    pv_name="BPMS:GUNB:314:X",
    attributes={"AREA": "GUNB"},
))
```

To change one field, read the record, modify it, and send the whole thing back:

```python
# cookbook:partial
read = client.annotation.pv_metadata.get_pv_metadata("BPMS:GUNB:314:X")
if read.result_status.is_error:
    raise RuntimeError(read.result_status.message)

existing = read.pv_metadata
assert existing is not None          # guaranteed once is_error is False

# carry everything forward, then change just what you meant to change
attributes = {a.name: a.value for a in existing.attributes}
attributes["S"] = "0.489700"         # re-surveyed

client.annotation.pv_metadata.save_pv_metadata(SavePvMetadataRequestParams(
    pv_name=existing.pvName,
    aliases=list(existing.aliases),
    tags=list(existing.tags),
    attributes=attributes,
    description=existing.description,
    modified_by="survey-update",
))
```

Note the conversions: `attributes` comes back as a repeated `Attribute` message with `.name` and
`.value`, while the save parameters take a plain `dict`.  `aliases` and `tags` come back as
protobuf repeated fields, which `list()` turns back into Python lists.

`createdTime` and `updatedTime` are server-managed — they are returned on reads but not accepted
on save, so there is nothing to carry forward.

## Looking up a single PV

`get_pv_metadata()` accepts a PV name **or** an alias:

```python
# cookbook:partial
result = client.annotation.pv_metadata.get_pv_metadata("BPMS:GUNB:314:X")
if result.result_status.is_error:
    raise RuntimeError(result.result_status.message)

metadata = result.pv_metadata
if metadata is not None:
    print(metadata.pvName)
    print(metadata.description)
    for attribute in metadata.attributes:
        print(f"  {attribute.name}={attribute.value}")
```

This is the direct answer to *"what do we know about `BPMS:GUNB:314:X`?"* — it returns the whole
name/value list, including `Z` and `S`.

## Finding PVs by what they are

`iter_pv_metadata()` pages transparently and yields individual records.  Criteria are built with
the `PvMetadataQuery` helpers, imported here as `Q`.

### Every PV of a given type in a machine area

Remember the combining rule: **separate criteria are ANDed**, values within one criterion are
ORed.

```python
# cookbook:partial
for record in client.annotation.pv_metadata.iter_pv_metadata([
    Q.attributes("AREA", ["GUNB"]),
    Q.attributes("TYPE", ["MONI"]),
]):
    print(record.pvName)
```

### Across several areas

One criterion with several values ORs them:

```python
# cookbook:partial
criteria = [
    Q.attributes("AREA", ["GUNB", "L0B", "HTR"]),   # any of these areas
    Q.attributes("TYPE", ["MONI"]),                  # AND is a monitor
]
for record in client.annotation.pv_metadata.iter_pv_metadata(criteria):
    print(record.pvName)
```

### Every signal from one physical device

```python
# cookbook:partial
for record in client.annotation.pv_metadata.iter_pv_metadata([
    Q.attributes("DEVICE", ["BPMS:GUNB:314"]),
]):
    print(record.pvName)     # BPMS:GUNB:314:X, :Y, :TMIT
```

### By name pattern

`pv_name()` takes `exact`, `prefix`, and `contains`, which may be combined:

```python
# cookbook:partial
for record in client.annotation.pv_metadata.iter_pv_metadata([
    Q.pv_name(prefix=["BPMS:"], contains=["GUNB"]),
]):
    print(record.pvName)
```

### By tag

```python
# cookbook:partial
for record in client.annotation.pv_metadata.iter_pv_metadata([Q.tags(["production"])]):
    print(record.pvName)
```

To require two tags at once, pass two criteria — `[Q.tags(["production"]), Q.tags(["critical"])]`
means *both*, whereas `[Q.tags(["production", "critical"])]` means *either*.

### One page at a time

When you need page boundaries — a UI, or bounded memory — use `query_pv_metadata()` directly:

```python
# cookbook:partial
page = client.annotation.pv_metadata.query_pv_metadata(
    [Q.attributes("AREA", ["GUNB"])], limit=100)

if page.result_status.is_error:
    raise RuntimeError(page.result_status.message)

for record in page.pv_metadata_list:
    print(record.pvName)

if page.next_page_token:
    print("more pages available")
```

Remember that `limit` is the **page size**, not a total cap, and that an empty result is success —
see [conventions](conventions.md#paging).

## Deleting

```python
# cookbook:partial
result = client.annotation.pv_metadata.delete_pv_metadata("BPMS:GUNB:314:X")
if result.result_status.is_error:
    raise RuntimeError(result.result_status.message)
```

Accepts a name or an alias, like `get_pv_metadata()`.  This removes the catalogue entry only — it
does not touch the PV's time-series data.

## Also worth knowing

### A note on numeric attributes

Attributes are string key/value pairs, and criteria match them **exactly**.  There is no range or
comparison operator, so `S` and `Z` are storable and retrievable but not directly searchable by
range — *"every BPM between S=0.4 and S=12.0"* is not expressible as a criterion.

Where that is needed, scope the query with categorical criteria and compare in Python:

```python
# cookbook:partial
in_range = [
    record
    for record in client.annotation.pv_metadata.iter_pv_metadata([
        Q.attributes("AREA", ["GUNB"]),
        Q.attributes("TYPE", ["MONI"]),
    ])
    if 0.4 <= float({a.name: a.value for a in record.attributes}["S"]) <= 12.0
]
```

This reads every matching record to filter client-side, so keep the server-side criteria as tight
as you can.  Note also that string equality is exact: `"0.489650"` and `"0.48965"` are different
attribute values even though the numbers are equal.

### Key-only (existence) search is not exposed

The protocol supports matching on an attribute *key* regardless of value, by sending an
`AttributesCriterion` with an empty `values` list.  The `Q.attributes()` helper does not allow
this — it raises `ValueError` on empty values, since an accidentally-empty list is far more often
a bug than a deliberate existence search.

If you genuinely need it, build the criterion directly:

```python
# cookbook:partial
# cookbook:no-mypy   (generated protobuf classes are built at import time; not statically visible)
from dp_python_lib.grpc import annotation_pb2

criterion = annotation_pb2.QueryPvMetadataRequest.QueryPvMetadataCriterion()
criterion.attributesCriterion.key = "S"      # no values -> match any PV having an S attribute

for record in client.annotation.pv_metadata.iter_pv_metadata([criterion]):
    print(record.pvName)
```

Dropping to the generated stubs like this is the general escape hatch whenever a helper is
stricter than the protocol.  The message classes are built dynamically at import time, so static
type checkers cannot see them — the code is correct, but your editor may flag it.

### Other details

- **An empty query result is not an error.**  Success with an empty list means nothing matched;
  check `is_error` to distinguish that from a failed call.
- **Saving with an empty `attributes={}` or `tags=[]` is the same as omitting them** — the request
  builder skips falsy values, so you cannot clear a field by passing an empty collection while
  leaving others intact.  A full-replace save that omits them clears them anyway.
- **`modified_by` is free text.**  Nothing validates it, but it is the only audit trail of who
  changed a record.
