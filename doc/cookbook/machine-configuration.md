# Recording Machine Configuration

Describing how the machine was set up, when each setup was in effect, and answering *"what was
the machine doing at 18:04 last Tuesday?"*

> **Verified against:** dp-python-lib 1.15.0.
> The machine configuration API is unchanged since 1.14.0; these recipes apply to both.

See [API conventions](conventions.md) for result checking, paging, and criteria rules.

All examples use `client.annotation.machine_config`, which is `None` unless an annotation channel
is configured.

### Imports used by the examples

```python
# cookbook:skip
from datetime import datetime, timezone

from dp_python_lib.client import (
    MldpClient,
    SaveConfigurationRequestParams,
    SaveConfigurationActivationRequestParams,
    ConfigurationQuery as C,
    ConfigurationActivationQuery as CA,
    to_timestamp,
)
```

## Contents

- [Model](#model) — configurations vs. activations
- [Defining a configuration](#defining-a-configuration)
- [Recording when it was active](#recording-when-it-was-active)
- [Closing one activation and opening the next](#closing-one-activation-and-opening-the-next)
- [What was the machine doing at a given instant?](#what-was-the-machine-doing-at-a-given-instant)
- [Finding activations](#finding-activations)
- [Addressing a specific activation](#addressing-a-specific-activation)
- [Deleting](#deleting)
- [Also worth knowing](#also-worth-knowing)

## Model

Two distinct things, and the split matters:

A **Configuration** is a reusable, named description of a machine setup.  It has **no time
component** — it is a definition, not an event.  `configurationName` is its primary key.

A **ConfigurationActivation** records a time interval during which a Configuration was in effect.
It references a configuration by name and carries a `startTime` and an `endTime`.

So `cxi-production` is a configuration; *"`cxi-production` was active from 17:00 to 23:00 on 2
February"* is an activation.  One configuration typically has many activations over its lifetime.

Activation intervals are **half-open**, `[start, end)`.  Setting one activation's `endTime` equal
to the next one's `startTime` gives continuous coverage with no gap and no overlap.  The server
rejects overlapping activations for the same configuration, and for different configurations in
the same `category`:

```
overlapping activation exists for configurationName 'cxi-production' or category 'physics-shift'
```

Configurations can also form a hierarchy via `parent_configuration_name` — useful when a specific
setup is a variation of a general one.

## Defining a configuration

A physics-shift setup: which beampath, what energy, what rate, which formal machine mode.

```python
# cookbook:partial
result = client.annotation.machine_config.save_configuration(SaveConfigurationRequestParams(
    configuration_name="cxi-production",
    category="physics-shift",
    attributes={
        "PATH": "CU_HXR",     # beampath
        "E": "14.6",          # beam energy, GeV
        "RATE": "10000",      # repetition rate, Hz
        "MODE": "09",         # formal machine mode
    },
    tags=["production"],
    description="CXI production running, Cu HXR beampath at 14.6 GeV",
    modified_by="physics-ops",
))

if result.result_status.is_error:
    raise RuntimeError(f"save failed: {result.result_status.message}")

print(result.configuration_name)     # 'cxi-production'
```

This is done **once, at setup** — not per shift.  The activations below are what change.

Like PV metadata, `save_configuration()` is a **full-replace upsert**: on update you must supply
the complete desired state, since omitted fields are cleared.  See
[conventions](conventions.md#save-semantics-full-replace).

`category` matters beyond grouping: the server's overlap validation applies *within* a category,
so configurations that are mutually exclusive in reality should share one.  The category used for
that check is the one on the **configuration**, not anything on the activation — changing a
configuration's category therefore changes which other configurations its activations conflict
with.

**The configuration must exist before any activation can reference it.**  Saving an activation
naming an unknown configuration fails with `no Configuration found for configurationName: '...'`,
rather than creating one implicitly.

## Recording when it was active

An activation ties the configuration to a time interval.  Timestamps accept a **timezone-aware**
`datetime`, epoch seconds, or a `common.Timestamp`:

```python
# cookbook:partial
shift_start = datetime(2026, 2, 2, 17, 0, tzinfo=timezone.utc)
shift_end = datetime(2026, 2, 2, 23, 0, tzinfo=timezone.utc)

result = client.annotation.machine_config.save_configuration_activation(
    SaveConfigurationActivationRequestParams(
        configuration_name="cxi-production",
        start_time=shift_start,
        end_time=shift_end,
        client_activation_id="act-2026-02-02-cxi",
        attributes={
            "DEST": "CXI",             # where beam was being delivered
            "EXP": "CXI_3443",         # experiment taking data
        },
        tags=["production"],
        description="CXI production shift",
        modified_by="physics-ops",
    ))

if result.result_status.is_error:
    raise RuntimeError(result.result_status.message)

print(result.client_activation_id)
```

**Supply your own `client_activation_id`.**  It is optional — the server generates one if you
omit it — but having a known ID lets you address the record later without a lookup, which the
next section depends on.  If you do omit it, keep the value returned in
`result.client_activation_id`.

Note that `DEST` and `EXP` live on the **activation**, not the configuration.  The configuration
describes the machine setup; where beam was going and which experiment was running are properties
of that particular interval.

### Naive datetimes are rejected

```python
# cookbook:partial
# ValueError -- no tzinfo
to_timestamp(datetime(2026, 2, 2, 17, 0))
```

A naive datetime would be read against the local timezone, silently shifting every activation by
the running machine's UTC offset.  Attach `tzinfo` explicitly.  See
[conventions](conventions.md#time).

### Open-ended activations: omit `end_time`

An activation with no `end_time` means **still in effect**.  This is the natural shape for a live
bridge: when the machine reports a configuration change, you open an interval now and close it
later, when the next change arrives — you do not know the end time at the moment you record the
start.

```python
# cookbook:partial
machine_config = client.annotation.machine_config
shift_start = datetime(2026, 2, 2, 17, 0, tzinfo=timezone.utc)

# no end_time -> open-ended: in effect until explicitly closed
machine_config.save_configuration_activation(SaveConfigurationActivationRequestParams(
    configuration_name="cxi-production",
    start_time=shift_start,
    client_activation_id="act-open",
    modified_by="physics-ops",
))
```

`get_active_configurations()` reports an open-ended activation as active at any instant at or
after its `start_time`.  To close it, re-save the record with the same `client_activation_id` and
a real `end_time` — see the next section.

To test whether a record you have read back is still open, check the field directly:

```python
# cookbook:partial
# cookbook:no-mypy   (generated protobuf classes are built at import time; not statically visible)
still_open = not activation.HasField("endTime")
```

## Closing one activation and opening the next

When the machine changes configuration at time `t`, close the current interval at `t` and open
the next one at the same instant.  Because intervals are half-open, that yields continuous
coverage with no gap and no overlap.

```python
# cookbook:partial
machine_config = client.annotation.machine_config
changeover = datetime(2026, 2, 2, 23, 0, tzinfo=timezone.utc)

# 1. read the activation currently in effect
read = machine_config.get_configuration_activation(client_activation_id="act-2026-02-02-cxi")
if read.result_status.is_error:
    raise RuntimeError(read.result_status.message)

current = read.configuration_activation
assert current is not None

# 2. close it -- same client_activation_id means UPDATE, not a second record.
#    save_* is full-replace, so carry every field forward.
machine_config.save_configuration_activation(SaveConfigurationActivationRequestParams(
    configuration_name=current.configurationName,
    start_time=current.startTime,                 # a common.Timestamp: accepted as-is
    end_time=changeover,                          # <- the change
    client_activation_id=current.clientActivationId,
    description=current.description,
    tags=list(current.tags),
    attributes={a.name: a.value for a in current.attributes},
    modified_by="physics-ops",
))

# 3. open the next interval starting exactly where the previous one ended
machine_config.save_configuration_activation(SaveConfigurationActivationRequestParams(
    configuration_name="mfx-production",
    start_time=changeover,                        # == previous end_time: no gap, no overlap
    end_time=datetime(2026, 2, 3, 6, 0, tzinfo=timezone.utc),
    client_activation_id="act-2026-02-02-mfx",
    attributes={"DEST": "MFX", "EXP": "MFX_1102"},
    modified_by="physics-ops",
))
```

Two things to get right:

- **Reuse the same `client_activation_id`** in step 2.  That is what makes it an update rather
  than a second, overlapping activation record.
- **Copy every field forward.**  `save_*` is full-replace, so omitting `description`, `tags`, or
  `attributes` erases them.  Note that `start_time` accepts the `common.Timestamp` you read back
  directly — no conversion needed.

### Late reports

If a configuration change is reported after the fact, use the **actual** change time, not the
time the report arrived.  Nothing special is required: step 2 sets an explicit `end_time` and
step 3 reuses it as `start_time`, so backdating is just a matter of choosing the right instant.

The server's overlap validation still applies, so a change time earlier than the current
activation's `startTime` is rejected.

## What was the machine doing at a given instant?

This is the question the whole model exists to answer:

```python
# cookbook:partial
when = datetime(2026, 2, 2, 18, 4, 1, tzinfo=timezone.utc)

result = client.annotation.machine_config.get_active_configurations(when)
if result.result_status.is_error:
    raise RuntimeError(result.result_status.message)

for activation in result.configuration_activations:
    print(activation.configurationName)
    for attribute in activation.attributes:
        print(f"  {attribute.name}={attribute.value}")     # DEST=CXI, EXP=CXI_3443
```

`get_active_configurations()` returns every activation whose interval covers that instant —
`startTime <= t` and `endTime > t`.  Several may be active at once when they belong to different
categories.

Called with no argument, it answers **"what is active right now"**:

```python
# cookbook:partial
active_now = client.annotation.machine_config.get_active_configurations()
```

The client fills in the current UTC time when you omit the argument.  That matters: the server
**rejects** a request with an absent or zero timestamp, so the no-argument form is a convenience
of this library rather than a server default.

To get the machine setup as well as the interval properties, look the configuration up by name:

```python
# cookbook:partial
when = datetime(2026, 2, 2, 18, 4, 1, tzinfo=timezone.utc)

result = client.annotation.machine_config.get_active_configurations(when)
for activation in result.configuration_activations:
    config_read = client.annotation.machine_config.get_configuration(activation.configurationName)
    configuration = config_read.configuration
    if configuration is not None:
        settings = {a.name: a.value for a in configuration.attributes}
        print(f"{configuration.configurationName}: {settings}")   # PATH, E, RATE, MODE
```

## Finding activations

`iter_configuration_activations()` pages transparently.  Criteria come from
`ConfigurationActivationQuery`, imported here as `CA`.

### Everything that happened in a time window

`time_range()` matches activations that **overlap** the window, not only those contained in it:

```python
# cookbook:partial
day_start = datetime(2026, 2, 2, 0, 0, tzinfo=timezone.utc)
day_end = datetime(2026, 2, 3, 0, 0, tzinfo=timezone.utc)

for activation in client.annotation.machine_config.iter_configuration_activations([
    CA.time_range(day_start, day_end),
]):
    print(activation.configurationName, activation.clientActivationId)
```

### Every interval a configuration was in effect

```python
# cookbook:partial
for activation in client.annotation.machine_config.iter_configuration_activations([
    CA.configuration_name(["cxi-production"]),
]):
    print(activation.startTime.epochSeconds, activation.endTime.epochSeconds)
```

### All the beam time an experiment received

Attributes recorded on the activation are searchable, so the experiment identifier is enough:

```python
# cookbook:partial
for activation in client.annotation.machine_config.iter_configuration_activations([
    CA.attributes("EXP", ["CXI_3443"]),
]):
    print(activation.configurationName, activation.startTime.epochSeconds)
```

Combining criteria ANDs them — this finds one experiment's intervals within one day:

```python
# cookbook:partial
day_start = datetime(2026, 2, 2, 0, 0, tzinfo=timezone.utc)
day_end = datetime(2026, 2, 3, 0, 0, tzinfo=timezone.utc)

criteria = [
    CA.attributes("EXP", ["CXI_3443"]),
    CA.time_range(day_start, day_end),
]
for activation in client.annotation.machine_config.iter_configuration_activations(criteria):
    print(activation.clientActivationId)
```

### Finding configurations themselves

The `ConfigurationQuery` helpers (`C`) search the definitions rather than the intervals:

```python
# cookbook:partial
for configuration in client.annotation.machine_config.iter_configurations([
    C.category(["physics-shift"]),
    C.tags(["production"]),
]):
    print(configuration.configurationName)
```

`C` offers `name`, `category`, `tags`, `attributes`, and `parent`; `CA` offers `timestamp`,
`time_range`, `configuration_name`, `client_activation_id`, `category`, `tags`, and `attributes`.

## Addressing a specific activation

`get_configuration_activation()` and `delete_configuration_activation()` take **either** a client
activation ID **or** the composite key `configuration_name` + `start_time` — exactly one form:

```python
# cookbook:partial
machine_config = client.annotation.machine_config

# by ID
machine_config.get_configuration_activation(client_activation_id="act-2026-02-02-cxi")

# by composite key -- for when you did not retain a server-generated ID
machine_config.get_configuration_activation(
    configuration_name="cxi-production",
    start_time=datetime(2026, 2, 2, 17, 0, tzinfo=timezone.utc),
)
```

Anything else raises `ValueError`:

| Arguments | Result |
|---|---|
| `client_activation_id` only | ✅ |
| `configuration_name` + `start_time` | ✅ |
| all three | ❌ `ValueError` |
| `configuration_name` alone | ❌ `ValueError` |
| `start_time` alone | ❌ `ValueError` |
| none | ❌ `ValueError` |

The composite key needs an **exact** `start_time` match, so it works for a record whose start you
know precisely — from a previous query, say — rather than as a search.

## Deleting

```python
# cookbook:partial
machine_config = client.annotation.machine_config

machine_config.delete_configuration_activation(client_activation_id="act-2026-02-02-cxi")
machine_config.delete_configuration("cxi-production")
```

**Order matters.**  Deleting a configuration is rejected while activations still reference it:

```
cannot delete configurationName 'cxi-production': existing activations must be deleted first
```

Remove the activations first.  Note that deleting an activation that does not exist is also an
error (`no ConfigurationActivation record found for: clientActivationId: ...`), not a silent
no-op — so a cleanup routine that deletes optimistically should tolerate that message.

## Also worth knowing

- **`createdTime` and `updatedTime` are server-set.**  Returned on reads, not accepted on save.
- **There is no "latest activation" query**, but results are ordered.  The server returns
  activations sorted by `startTime` **ascending**, so the most recent one in a result set is the
  last record — including closed activations, which `get_active_configurations()` will not return.
  There is still no total count, so getting the newest means paging to the end of a bounded window
  rather than asking for it directly.  Bound the window with `CA.time_range()` rather than
  scanning everything.

  The ordering is observed server behavior rather than a documented API guarantee, so treat it as
  a convenience rather than something to rely on across releases.
- **`patch*` and `bulkSave*` are not implemented.**  The copy-forward pattern above is the only
  way to change one field of an activation.
- **An empty result is not an error** — success with an empty list means nothing matched.
