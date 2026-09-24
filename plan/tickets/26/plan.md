# Issue #26 — Detecting open-ended configuration activations

- **Ticket**: [osprey-dcs/dp-python-lib#26](https://github.com/osprey-dcs/dp-python-lib/issues/26)
- **Surfaced by**: [#20](https://github.com/osprey-dcs/dp-python-lib/issues/20), whose fix
  ([#25](https://github.com/osprey-dcs/dp-python-lib/pull/25)) made `end_time` optional, so callers can
  *create* an open-ended activation.  #26 is about *reading* one.
- **Status**: written 2026-09-24 against dp-python-lib `e6acada`, dp-service `5e64d3a` (`origin/main`),
  and dp-grpc `e775244`.  The ticket is AI-drafted.  Its premise holds, but it proposes the wrong shape (a
  property on one result class) and misses the real hazard (T3) along with a latent cookbook bug (T4).
  Findings are under [Background](#background--triage-findings).  Open questions Q1–Q3 were resolved
  2026-09-24, as recommended.  The issue body has been updated to match.

## Overview

An activation with no `endTime` means "still in effect".  Today the only way to recognize one is a
protobuf presence check in user code, `not activation.HasField("endTime")`.  The more dangerous way to
get it wrong fails silently: reading `activation.endTime` on an open record does not fail, but returns a
zero `Timestamp`, which is 1970-01-01.

The ticket adds two module-level helpers in `machine_config_client`, both exported from
`dp_python_lib.client`:

```python
def activation_is_open(activation: common_pb2.ConfigurationActivation) -> bool: ...
def activation_end_time(activation: common_pb2.ConfigurationActivation) -> common_pb2.Timestamp | None: ...
```

They work on every path that yields a `ConfigurationActivation`: get, query, iterate, and
`get_active_configurations()`.  The cookbook adopts them, including one recipe that currently prints `0`
for an open activation.

**Worth doing, and small.**  On its own, `activation_is_open()` is sugar for a one-liner.  The value is
`activation_end_time()`, which replaces a silent wrong answer (1970) with an explicit `None`, and the
cookbook fixes that come with it.  It is additive: no change to request building, results, or the wire
format.

## Background / triage findings

- **T1 — The premise holds on every read path, not just `get`.**  In dp-service, all three read paths
  build their response with the same converter.  `GetConfigurationActivationDispatcher`,
  `QueryConfigurationActivationsDispatcher`, and `GetActiveConfigurationsDispatcher` all call
  `ConfigurationActivationDocument.toConfigurationActivation()`.  That converter sets `endTime` only
  `if (this.endTime != null)`, and the document field is commented `// null = open-ended`.  The save path
  likewise stores it only `if (request.hasEndTime())`.  So `HasField("endTime")` is the correct and
  complete test on every path.  `common.proto` agrees: `Timestamp endTime = 4; // optional; absent =
  open-ended interval`.  The live round trip is already covered by
  `tests/integration/test_machine_config_client_integration.py` (~line 359), which asserts `endTime` is
  absent on read-back.

- **T2 — The motivating case is mostly a query, which the ticket's proposal would not cover.**  The ticket
  puts `is_open` on `GetConfigurationActivationApiResult`.  But a live bridge closing "whatever is open
  for configuration X" often does not hold the open record's id.  There is also no server-side criterion
  for "open": `ConfigurationActivationQuery` offers `timestamp`, `time_range`, name, id, category, tags,
  and attributes.  So finding it means `iter_configuration_activations(...)` filtered on the client, and
  that path yields bare `common_pb2.ConfigurationActivation` objects.  The ticket names this as "the main
  design question".  The answer is a module-level helper (D1).

- **T3 — The real hazard is silent, and the ticket does not mention it.**  Verified with protobuf 7.35.1
  on an activation with `endTime` unset:

  | expression | result |
  |---|---|
  | `a.endTime` | a zero `Timestamp` (1970-01-01T00:00:00Z) |
  | `bool(a.endTime)` | `True` |
  | `a.endTime is None` | `False` |
  | `to_epoch_nanos(a.endTime)` | `0` |
  | `a.HasField("endTime")` after any of the above | still `False` (reading does not set presence) |

  Code that reads the end time without first checking presence gets a plausible-looking wrong answer,
  and none of the natural Python checks catch it.  This is why `activation_end_time()` is in scope (Q2).

- **T4 — The cookbook already contains this bug.**  In `doc/cookbook/machine-configuration.md`, "Every
  interval a configuration was in effect" does
  `print(activation.startTime.epochSeconds, activation.endTime.epochSeconds)`, which prints `0` for an
  open-ended activation.  The ticket lists only the "still open" recipe (~line 183–193) as needing
  change.  Two more spots need it:
  - "Closing one activation and opening the next" step 1 reads the current activation by a known
    `client_activation_id`.  It should also show how to find the open one when you don't have the id.
  - The `get_active_configurations()` paragraph states the match rule as `startTime <= t` and
    `endTime > t`, without saying that an absent `endTime` also matches.  The section above it says so,
    but the rule as written is incomplete.

- **T5 — There is no precedent to match in the Java client.**  dp-service's `dp.client` result classes
  have no open-ended accessor, so there is no cross-language name to follow.

- **T6 — No existing tests assert behavior this changes.**  The change is purely additive.  The only
  existing `HasField("endTime")` assertions are on *requests*, in `TestBuildSaveActivationRequest`, and
  they stay as they are.

## Design decisions

- **D1 — Module-level helpers, not a result property** (Q1).  A function taking a
  `ConfigurationActivation` covers all four read paths with one definition.  The ticket's
  `GetConfigurationActivationApiResult.is_open -> bool | None` is rejected.  It covers one path of four,
  and it adds a second way to ask the same question.  Its `None` on error adds nothing:
  `configuration_activation` is already `None` then, and a caller must narrow that before using the
  record anyway.  *Rejected alternative*: offering both, with the property delegating to the helper.

- **D2 — Names are `activation_is_open` / `activation_end_time`** (Q3).  They are imported into the
  `dp_python_lib.client` package namespace alongside dozens of other names, where a bare `is_open` could
  mean an open channel or file.  The `activation_` prefix names the subject, and the two helpers sort
  together.

- **D3 — `activation_end_time()` returns `common_pb2.Timestamp | None`, not epoch nanos or a datetime.**
  It returns the same type as the field, so it can be passed straight back as `end_time=` in the
  full-replace re-save the cookbook's closing recipe does.  `to_timestamp()` accepts a `Timestamp` as-is.
  A caller who wants nanos composes it with `to_epoch_nanos()`.  Converting here would have to pick one
  representation for everyone.  It returns the message's own sub-message, not a copy, the same as
  `configuration_activation` does.

- **D4 — The helpers take a `ConfigurationActivation`, not `... | None`.**  Accepting `None` would force a
  three-valued return.  Passing `None` at runtime fails with `AttributeError`, which is acceptable for a
  type error.  mypy cannot flag it yet, because `common_pb2` resolves to `Any` until typed stubs land
  (#61).  After that, passing the unnarrowed `configuration_activation` becomes a type error.

- **D5 — Presence is the only test; epoch zero is a real end time.**  An `endTime` that is present with
  value 0 means "closed at 1970-01-01", not open.  This mirrors the save-side rule pinned by
  `test_end_time_zero_is_not_treated_as_absent`.  The helpers must not add a `!= 0` fallback, and a test
  pins that.

## Implementation tasks

**`src/dp_python_lib/client/machine_config_client.py`**
- Add `activation_is_open()` and `activation_end_time()` as module-level functions after the
  `ConfigurationActivationQuery` class, before the params/result classes.  Their docstrings state:
  - absent means open-ended ("still in effect"), and this matches the server's representation on every
    read path;
  - the 1970 hazard (T3), so a reader knows why not to touch `.endTime` directly;
  - `activation_end_time()` returns the field itself, which is a `Timestamp` that `end_time=` accepts
    as-is.
- `activation_end_time()` is
  `return activation.endTime if activation.HasField("endTime") else None`.
  `activation_is_open()` is `not activation.HasField("endTime")`.  Neither reimplements the other's check
  in a way that could drift.
- Update the `SaveConfigurationActivationRequestParams.end_time` docstring to point at the two helpers
  for reading the value back.

**`src/dp_python_lib/client/__init__.py`**
- Import both helpers from `machine_config_client` and add them to `__all__`.

**`.dev/tools/check-cookbook-snippets.py`**
- Add both names to `PREAMBLE`'s `from dp_python_lib.client import (...)`, next to `to_timestamp`, so
  cookbook snippets using them type-check.

**`tests/unit/test_machine_config_activation_client.py`** — new `TestActivationOpenEndedHelpers`:
- open-ended record: `activation_is_open` is `True` and `activation_end_time` is `None`;
- bounded record: `False`, and the end time equals the stored `Timestamp`;
- **epoch-zero `endTime` present**: `False`, and the end time is a `Timestamp` with `epochSeconds == 0`
  (D5);
- reading `a.endTime` first (e.g. `a.endTime.epochSeconds`) does not make an open record look closed
  (T3's last row);
- the returned `Timestamp` round-trips as `end_time=` through `_build_save_configuration_activation_request`;
- works on records taken from a `QueryConfigurationActivationsApiResult` page that mixes open and closed
  records: filtering with the helper keeps exactly the open ones.  This pins the query-path use case
  (T2).

**`tests/integration/test_machine_config_client_integration.py`**
- In the open-ended round-trip test, assert `activation_is_open(activation)` and
  `activation_end_time(activation) is None` alongside the existing raw `HasField` assertion.  Keep the
  raw assertion: it is what verifies the wire.  After closing, assert `not activation_is_open(closed)`
  and that `activation_end_time(closed).epochSeconds == end_time`.

**`doc/cookbook/machine-configuration.md`**
- Add both helpers to the page's import block.
- "Open-ended activations": replace the `HasField` snippet with `activation_is_open()`.  Add one
  sentence on the T3 hazard, pointing at `activation_end_time()`.
- "Closing one activation and opening the next": add a short variant for step 1 that finds the open
  activation by configuration name with `iter_configuration_activations([CA.configuration_name([...])])`
  and filters with `activation_is_open()`.  Say what to do when there is not exactly one open record.
  Keep the known-id form as the main path.
- "Every interval a configuration was in effect": print via `activation_end_time()`, showing
  "still in effect" for `None` (T4).
- `get_active_configurations()` paragraph: state the match rule as `startTime <= t` and either
  `endTime > t` or no `endTime`.

**`CLAUDE.md`**
- Key Files `machine_config_client.py` entry: add the two helpers and the one-line reason
  (reading `endTime` on an open record silently yields 1970).
- Machine Configuration API usage snippet and notes: show `activation_is_open()`, and add a note naming
  the hazard.

**`doc/release-notes/NEXT.md`**
- Add a section "Detecting open-ended activations (#26)" and a Contents entry.  It is additive with no
  upgrade action.  Mention the corrected cookbook recipe, since anyone who copied it prints `0` for open
  intervals.

**`README.md`** — no change.  Its machine configuration bullet lists capabilities, not helpers.

Checks before the PR: `pytest tests/unit/`, `ruff check .`, `ruff format --check .`, `mypy src/`,
`python .dev/tools/check-cookbook-snippets.py`, `python .dev/tools/check-release-notes.py`.  Also run the
machine-config integration test against a live stack, if one is available.

## Out of scope

- **A result-class property** (`GetConfigurationActivationApiResult.is_open`): rejected in D1.
- **A server-side "open activations" query criterion.**  This would need a dp-grpc proto change and a
  dp-service filter (`endTime` does not exist).  The client-side filter covers the bridge case, and a
  configuration has few activations.  File upstream if volume ever makes it matter.  No ticket exists.
- **Presence accessors for other optional message fields.**  None has this problem in the same form.
  `DataBlock` bounds are required, and provenance `time_range` is already reported only when present
  (`column_metadata_dict()`).

## Dependencies and sequencing

- None.  #25 (optional `end_time`) has shipped.  Nothing here waits on the typed-stub sync (#61):
  `HasField("endTime")` is a valid literal under mypy-protobuf stubs, and the helpers' annotations use the
  real `common_pb2` types.
- The plan goes up as its own PR (`Refs #26`); implementation follows in a separate PR (`Closes #26`).

## Open questions

- **Q1 — Property, module helper, or both?**  *Resolved 2026-09-24: module helper only* (D1).
- **Q2 — Also add an end-time accessor returning `None` for open records?**  *Resolved 2026-09-24: yes,
  `activation_end_time()`* (T3, D3).
- **Q3 — Naming.**  *Resolved 2026-09-24: `activation_is_open` / `activation_end_time`* (D2).
