# Issue #40 — Let the criterion helpers build the key-only attributes search the protos document

- **Ticket**: [osprey-dcs/dp-python-lib#40](https://github.com/osprey-dcs/dp-python-lib/issues/40)
- **Companion**: [#41](https://github.com/osprey-dcs/dp-python-lib/issues/41) (optional `criteria` on the six
  annotation-service query/iter methods) — same three source files, same three cookbook pages.  Sequencing in
  [Dependencies](#dependencies-and-sequencing).
- **Surfaced by**: `plan/tickets/6/plan.md` finding 5, whose new `DataSetQuery.attributes()` /
  `AnnotationQuery.attributes()` accept the key-only form from the start.
- **Status**: written 2026-09-10 against dp-python-lib `dc477be`, dp-grpc `6dfff3f`, dp-service `fddf692` (the
  same upstream commits `plan/tickets/6/plan.md` was verified against).  Triage verified every premise in the
  ticket body against the protos, the server source, and this repo's own history; three corrections to the
  ticket are recorded below and folded into [Implementation tasks](#implementation-tasks).

## Overview

Five criterion helpers reject an empty `values` list, making the protocol's key-only ("does this record have
attribute `X` at all?") search unreachable through the library.  This relaxes all five to accept an absent or
empty `values`, emitting a `key`-only criterion, while keeping the empty-`key` rejection.

The change is small — five two-line edits — but it is worth doing for three reasons, in descending order:

1. **The library is internally inconsistent right now.**  Issue #6 shipped `DataSetQuery.attributes()` and
   `AnnotationQuery.attributes()` accepting the key-only form; the five older helpers reject it.  Seven
   helpers spell the same protocol concept two different ways, and the two that differ are the *newest*, so
   the inconsistency grows rather than ages out.
2. **The stated rationale for the guard does not hold for this criterion.**  `doc/cookbook/conventions.md`
   justifies rejecting empty input as refusing "a criterion that would silently match everything."  That is
   true of `tags([])` and `pv_name()`, but false of a key-only attributes criterion, which matches records
   *possessing the key* — a narrowing filter, not a match-all.  The guard was generalized to a case it does
   not describe.
3. **The workaround is documented, which is evidence of demand, and it is a bad workaround.**
   `doc/cookbook/pv-metadata.md` already devotes a section ("Key-only (existence) search is not exposed") to
   dropping through to the generated protobuf classes — a snippet that must carry `# cookbook:no-mypy`
   because the escape hatch is not statically checkable.  The library forces users off its own type-checked
   surface to reach a documented protocol feature.

Non-breaking: every existing call passes a non-empty list and is unaffected.

## Background / triage findings

Verified against the sources, not taken from the ticket text.

- **T1 — The five helpers and their files are exactly as the ticket lists them.**  Confirmed at
  `pv_metadata_client.py:92`, `machine_config_client.py:90` and `:235`, `query_client.py:143` and `:234`.  All
  five have the identical two-guard body (`if not key: raise` / `if not values: raise`), and all five
  currently assign `criterion.attributesCriterion.values[:] = values` unconditionally.

- **T2 — The proto comments say what the ticket quotes them as saying.**  All five sites declare
  `repeated string values` and document the empty-values existence search.  The three annotation.proto sites
  (`dp-grpc/src/main/proto/annotation.proto:1850`, `:2276`, `:2731`) carry the long form the ticket quotes;
  the two query.proto sites (`query.proto:301`, `:384`) carry a compressed one-liner, `key required; empty
  values = key-only existence search`.  A documentation-density difference only, not a semantic one.

- **T3 — The server genuinely implements it, and the behavior is pinned by server tests.**  This is the
  premise the ticket asserts but does not evidence, and it is the one that decides whether the ticket is
  worth doing at all.  Four of the five criteria route to one shared helper,
  `dp-service/src/main/java/com/ospreydcs/dp/service/common/mongo/MongoQueryFilterBuilder.java:70-76`:

  ```java
  public static Bson attributeFilter(String key, List<String> values) {
      final String mapKey = BsonConstants.BSON_KEY_ATTRIBUTES + "." + key;
      if (values == null || values.isEmpty()) {
          return Filters.exists(mapKey);
      }
      return Filters.in(mapKey, values);
  }
  ```

  Empty values builds a real `exists` filter — not an `$in: []` that would match nothing.  Callers:
  `MongoSyncAnnotationClient.java:971` (PV metadata), `:1491` (activations), and
  `QueryV2Resolver.java:309`, `:412` (the two v2 selectors).  The fifth, Configurations, *inlines* the same
  branch at `MongoSyncAnnotationClient.java:1225-1232` — duplicated code, behaviorally identical, no
  divergence.  Attributes are stored as a nested BSON document keyed by attribute name
  (`DpBsonDocumentBase.java:31`, `Map<String, String> attributes`) and backed by a wildcard index
  (`MongoClientBase.java:187`), so the dotted-path `exists` is the correct and indexed form.
  `MongoQueryFilterBuilderTest.java:114-139` covers the empty path explicitly, for both `emptyList()` and
  `null`.  Nothing on the server rejects empty values, on any of the five.

- **T4 — Server-side validation checks a blank *key* and deliberately does not check values.**  The three
  annotation-service jobs reject a blank key (`QueryPvMetadataJob.java:68-74`, `QueryConfigurationsJob.java:74`,
  `QueryConfigurationActivationsJob.java:97`).  In each, the `TAGSCRITERION` arm immediately above *does*
  reject empty values — so the omission for attributes is a decision, not an oversight.  This is the
  strongest available evidence that key-only is intended rather than merely tolerated.

- **T5 — The two v2 selectors have no attributes validation at all**, so a blank key there falls through to
  `Filters.exists("attributes.")` and silently matches nothing.  Keeping the client-side non-blank-key
  rejection on all five papers over that gap uniformly.  This is an argument for the shape of the change
  (relax `values`, keep `key`), not against the change.

- **T6 — The guard was not an upstream-driven decision, and it postdates the semantics it overrides.**  It
  arrived in `c101964` ("Address Copilot review: criterion validation, dep pins, README RPC names",
  2026-07-14), which applied one rule — "raise on inputs that would build an empty criterion" — across
  `pv_name`/`aliases`/`tags`/`attributes` at once.  The proto comments documenting key-only search predate
  it: `7b6ce5d` (2026-04-24) for PV metadata, `315b4f6` (2026-05-01) for machine configuration, `37ac710`
  (2026-07-05) for query v2.  So the guard did not reflect a considered reading of the protocol; it
  generalized a sound rule onto the one criterion where it does not apply.  This matters for the ticket's
  framing: it is not overturning a deliberate design decision.

- **T7 — Existing tests assert the rejection, and the ticket does not mention them.**  Five assertions must
  be inverted, not merely supplemented: `test_pv_metadata_client.py:151`, `test_machine_config_client.py:75`,
  `test_machine_config_activation_client.py:89`, `test_query_client.py:128`, and — easy to miss — the
  `lambda: ConfigQuery.attr("k", [])` entry inside the `test_empties_raise` loop at `test_query_client.py:167`.
  The empty-key assertions beside them all stay.

- **T8 — The documentation surface is wider than the ticket's three files.**  Beyond
  `pv-metadata.md` / `machine-configuration.md` / `query.md`, two more need edits:
  - `doc/cookbook/conventions.md:178-189` states the empty-input guard as a uniform rule across all five
    helper classes ("**The helpers reject empty input.**  Every one of them raises `ValueError`…").  After
    this change that sentence is false, and it is the one place a reader looks for the general rule.
  - `doc/cookbook/pv-metadata.md:353-376`, the entire "Key-only (existence) search is not exposed" section,
    becomes wrong.  It is not a tweak: the section's *premise* is removed, so it should be replaced by a
    short recipe using the helper, and the `# cookbook:no-mypy` escape-hatch snippet deleted.  Deleting it
    is a documentation improvement in its own right — that snippet exists only to work around this bug.
  - `machine-configuration.md:377` and `query.md:216` are one-line helper inventories; they gain a clause.
    Neither file currently has an attributes recipe worth extending, so no new prose is needed there.

- **T9 — Baseline is green.**  719 unit tests pass at `dc477be` (plus 45 subtests).  No integration test
  touches these helpers, and no live server is required to verify this change.

- **T10 — No current consumer exercises key-only, so this is a capability gap, not a reported break.**  The
  desktop app builds attribute criteria through `setIfBothPresent(attributeKeyCriterion,
  attributeValueCriterion, …)` (`dp-desktop-app/.../DpApplication.java:1018`, `:1122`), requiring both key and
  value.  Recorded so the ticket is not oversold: nothing is broken in the field today.  The case rests on
  the internal inconsistency (Overview 1) and the documented-workaround cost (Overview 3).

## Design decisions

- **D1 — Match the #6 helpers exactly: `values: list[str] | None = None`, guarded by `if values:`.**  The
  five relaxed helpers should become copies of the shape already shipped at `dataset_client.py:193-214`, down
  to the `if values:` truthiness test (which folds `None` and `[]` together, both meaning key-only).  The
  point of the ticket is to remove a divergence, so introducing a second spelling would defeat it.
  Rejected: a distinct `attributes_exist(key)` helper.  It would read more explicitly at the call site, but
  it doubles the helper count on five classes, diverges from the two #6 helpers that just shipped, and has no
  counterpart in the protocol, which models this as one criterion with an optional field.

- **D2 — Keep the non-blank-key rejection on all five.**  Unchanged behavior, but now load-bearing: per T5,
  the v2 path has no server-side key check, so a blank key would silently match nothing.  The client-side
  guard is the only one there is for `PvQuery.attr` / `ConfigQuery.attr`.

- **D3 — Invert the five empty-values assertions rather than deleting them.**  Each becomes a positive
  key-only build-request test covering *both* spellings (`attributes("k")` and `attributes("k", [])`),
  mirroring `test_dataset_client.py:250-255`.  Deleting them would leave the new behavior unpinned in exactly
  the place a future reviewer would look for it.

- **D4 — Correct `conventions.md`'s general rule rather than leaving it as a near-truth.**  Reword to state
  that helpers reject empty input *except* `attributes()`/`attr()`, whose empty `values` is the protocol's
  key-only existence search — with the reason: an empty attributes criterion narrows, it does not match all.
  This is the one doc edit that is not optional; the others are recipes.

- **D5 — Replace, do not amend, the pv-metadata "not exposed" section.**  Per T8, retitle to a positive
  recipe ("Key-only (existence) search") showing `Q.attributes("S")`, and drop the raw-protobuf snippet and
  its `# cookbook:no-mypy` directive.  Keep the surrounding paragraph about dropping to the generated stubs
  as a *general* escape hatch only if it still reads naturally without this example; otherwise let it go —
  `conventions.md` is the right home for a general statement, and it is not this ticket's job to relocate it.

## Implementation tasks

Single PR; the work is one commit's worth.

1. **`src/dp_python_lib/client/pv_metadata_client.py`** — `PvMetadataQuery.attributes` (line 92): signature to
   `values: list[str] | None = None`; drop the `if not values: raise`; guard the assignment with `if values:`.
   Docstring: mark `values` optional, state the key-only meaning, drop `values is empty` from `:raises:`.
2. **`src/dp_python_lib/client/machine_config_client.py`** — same for `ConfigurationQuery.attributes` (line 90)
   and `ConfigurationActivationQuery.attributes` (line 235).
3. **`src/dp_python_lib/client/query_client.py`** — same for `PvQuery.attr` (line 143) and `ConfigQuery.attr`
   (line 234).
4. **Tests** — per D3, in `test_pv_metadata_client.py`, `test_machine_config_client.py`,
   `test_machine_config_activation_client.py`, and `test_query_client.py` (two helpers, including the
   `test_empties_raise` loop entry at line 167).  Each: a key-only build-request test asserting
   `HasField("attributesCriterion")`, the key, and `values == []`; empty-key assertions retained.
5. **`doc/cookbook/conventions.md`** — reword the empty-input rule per D4.
6. **`doc/cookbook/pv-metadata.md`** — replace the "not exposed" section per D5.
7. **`doc/cookbook/machine-configuration.md:377` and `doc/cookbook/query.md:216-217`** — note the key-only
   form in the helper inventories.
8. **Verify** — `.venv/bin/python -m pytest tests/unit/` (expect 719 + 5 new, all passing);
   `ruff check .` and `ruff format --check .`; and
   `.venv/bin/python .dev/tools/check-cookbook-snippets.py`, which must be run because task 6 changes a
   snippet's directives.

No `CLAUDE.md` change: it already describes the key-only form as #6's and names #40 as the back-port
([the DataSets/Annotations section](../../../CLAUDE.md)).  That sentence should be updated to past tense when
this merges, which is a one-line edit best made in the same PR — added here so it is not forgotten.

## Out of scope

- **Optional `criteria` on the query/iter methods** — [#41](https://github.com/osprey-dcs/dp-python-lib/issues/41).
- **Relaxing any other empty-input guard** (`tags([])`, `pv_name()`, `parent([])`, …).  For those the
  `conventions.md` rationale holds exactly: they would match everything.  No change, and D4's rewording must
  not weaken the rule for them.
- **The server-side blank-key gap on the two v2 selectors** (T5).  A dp-service issue if anyone wants it;
  D2 makes it unreachable through this client.
- **De-duplicating the inlined Configurations attributes branch** (T3) — a dp-service refactor, no client
  impact.

## Dependencies and sequencing

- **Nothing blocks this.**  It needs no stub regeneration (the `key`/`values` fields are present in the
  committed stubs, introspected: `['key', 'values']`), no server change, and no live server to verify.
- **Independent of #41**, which changes method signatures rather than criterion builders.  They touch the
  same three source files and overlapping cookbook pages, so shipping them **as two commits in one PR** is
  reasonable and was the ticket's own suggestion; shipping separately is equally fine, with the second to
  land rebasing over the first's `conventions.md` edit.  Recommendation: one PR, two commits, #40 first —
  its `conventions.md` edit is the smaller of the two.
- **No upstream release gating.**  The behavior has been in dp-service since before `fddf692` and needs no
  version banner: unlike the sample status API, this ships against servers already in use.

## Open questions

- **Q1 — Should `conventions.md` keep a single blanket sentence, or split the rule per helper?**
  *Recommendation*: keep it a single sentence with a stated exception (D4).  A per-helper table would be
  accurate but out of proportion for one exception in one criterion type.
  **Resolved 2026-09-10**: accepted.

- **Q2 — Should the general "drop to the generated stubs" paragraph in `pv-metadata.md` survive the section
  it illustrates?**  *Recommendation*: decide while editing (D5).  It is genuinely useful advice that
  happens to have lost its example; if a natural one-sentence form remains, keep it, otherwise drop it
  rather than inventing a contrived replacement example.
  **Resolved 2026-09-10**: accepted — defer to the edit, no separate decision needed.
