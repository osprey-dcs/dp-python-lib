# API Conventions

Patterns that recur throughout `dp_python_lib`.  Recipes in this cookbook link here rather than
repeating them.

> **Verified against:** dp-python-lib 1.15.0.
> This package's version tracks the dp-grpc version its stubs were generated from.  Note that
> **1.15.0 is ahead of the latest dp-grpc release** (`rel-1.14.0`): the 1.15.0 stubs come from
> unreleased dp-grpc work, so a server running the current release will not implement everything
> documented here.  See each recipe's own verified-against note.

For the wire-level view of these same conventions — the protobuf messages and the `oneof result`
pattern this library wraps — see the
[dp-grpc cookbook](https://github.com/osprey-dcs/dp-grpc/blob/main/doc/cookbook/conventions.md).

## Contents

- [Checking results](#checking-results) — the one pattern every call shares
- [Empty is not an error](#empty-is-not-an-error)
- [Paging](#paging) — and why `iter_*` is usually the right choice
- [Query criteria](#query-criteria) — AND/OR rules
- [Save semantics: full replace](#save-semantics-full-replace)
- [Time](#time) — accepted inputs, and why naive datetimes are rejected
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

**The helpers reject empty input.**  Every one of them raises `ValueError` rather than building a
criterion that would silently match everything:

```python
# cookbook:partial
Q.tags([])                  # ValueError: tags() requires a non-empty values list
Q.pv_name()                 # ValueError: requires at least one non-empty of exact/prefix/contains
```

That is a deliberate guard — an empty criterion is nearly always a bug in the caller's filter
construction, and failing loudly beats returning the whole collection.

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

t1 = to_timestamp(datetime(2026, 2, 2, 18, 4, 1, tzinfo=timezone.utc))
t2 = to_timestamp(1770055441)
t3 = to_timestamp(1770055441.5)      # .5 -> 500_000_000 nanoseconds
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
range you asked for.  Sample-oriented queries (`query_samples()`) trim to the exact range.

## Optional dependencies

The core install is deliberately lightweight.  Converting query results to pandas, NumPy, or
Excel requires the `analysis` extra:

```
pip install dp-python-lib[analysis]
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
