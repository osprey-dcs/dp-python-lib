# Issue #41 — Make `criteria` optional on the annotation-service query/iter methods

- **Ticket**: [osprey-dcs/dp-python-lib#41](https://github.com/osprey-dcs/dp-python-lib/issues/41)
- **Companion**: [#40](https://github.com/osprey-dcs/dp-python-lib/issues/40) (key-only `attributes()`), shipping
  in the same PR as the preceding commit.  Same three source files, overlapping cookbook pages.
- **Surfaced by**: `plan/tickets/6/plan.md` finding 5 / Q6 / D3, whose `query_datasets()` / `query_annotations()`
  and their `iter_*` forms take `criteria` optional from the start.
- **Status**: written 2026-09-10 against dp-python-lib `e7a77db` (the #40 commit), dp-grpc `6dfff3f`,
  dp-service `fddf692`.  Every premise verified against the protos and the server source; two attribution
  errors in the ticket body are corrected below.

## Overview

Six methods take `criteria` as a required positional argument, a shape that dates from when the server
**rejected** an empty criteria list.  The server now treats an empty list as match-all, so "browse everything,
paged" is a legitimate call — but from Python it currently reads `iter_configurations([])`, which looks like a
mistake at the call site and is documented nowhere.

Make `criteria` optional on all six, so `for cfg in mc.iter_configurations(): ...` is the browse-all form.

Non-breaking: `criteria` stays the first parameter, so every positional caller is unaffected.

## Background / triage findings

Verified against the sources, not taken from the ticket text.

- **T1 — The six methods are as listed**, plus three private request builders that must change with them:
  `pv_metadata_client.py:405/445/471` (`_build_query_pv_metadata_request`, `query_pv_metadata`,
  `iter_pv_metadata`), `machine_config_client.py:755/800/826` (configurations) and `:1113/1158/1184`
  (activations).

- **T2 — The server accepts an empty criteria list on all five annotation-service paged queries and treats it
  as match-all.**  Each validation site carries a deliberate comment rather than merely lacking a check —
  `QueryPvMetadataJob.java:38`, `QueryConfigurationsJob.java:38`, `QueryConfigurationActivationsJob.java:38`:

  ```java
  // An empty criteria list is match-all by contract (#245), not an error, so there is
  // deliberately no list-level emptiness check here.  Per-criterion validation below is
  // unaffected: a criterion that IS supplied must still be well-formed.
  ```

  Datasets and annotations carry the same comment in `AnnotationServiceImpl.java:228` and `:679`.  Per-criterion
  validation is retained everywhere: a criterion that *is* supplied must still be well-formed, and
  `CRITERION_NOT_SET` is still rejected.  So this change loosens the list, not its contents.

- **T3 — Paging still applies, and the default is 100.**  `MongoSyncAnnotationClient.java:81` declares
  `private static final int DEFAULT_QUERY_LIMIT = 100`, applied identically at all five call sites as
  `request.getLimit() > 0 ? request.getLimit() : DEFAULT_QUERY_LIMIT`.  The class comment at `:79` and the
  proto both state the default is **unconditional** — it does not depend on whether criteria were supplied, so
  removing the last criterion from a request does not change its page size.  `:986` adds "limit is always
  positive (DEFAULT_QUERY_LIMIT when unset), so there is no unbounded path."  This confirms the ticket's claim
  that `iter_*` remains the right call for browse-all: a bare `query_*()` returns 100 records and a page token,
  not the collection.

- **T4 — Correction to the ticket: the default page size is NOT configurable.**  The ticket says "the server's
  default page size still applies", which is true, but the proto wording for datasets/annotations
  ("a server-configured default page size") is loose and should not be carried into client docs.
  `DEFAULT_QUERY_LIMIT` is a hardcoded `private static final int` with no config key and no `configMgr()`
  lookup.  Do not promise users it is tunable.  (Distinct from the genuinely config-driven
  `DEFAULT_SAMPLE_STATUS_QUERY_DEFAULT_PAGE_SIZE = 10_000` in `MongoAnnotationHandler.java:34`, which belongs
  to the sample status API — do not conflate them.)

- **T5 — Correction to the ticket: the upstream attribution is too broad.**  The ticket credits dp-grpc #245 /
  PR #147 for all five RPCs.  `2e6f849` (inside PR #147) changed only the three metadata queries —
  `annotation.proto:1805`, `:2221`, `:2642` — replacing the prior text "An empty criteria list is rejected with
  an ExceptionalResult; at least one criterion is required."  Datasets and annotations got their match-all
  wording from the earlier `7b2ea35` (dp-grpc #132) at `annotation.proto:929` and `:1471`, with dp-service #248
  implementing it.  The net behavior is what the ticket says; only the provenance is wrong.  Worth correcting
  because this repo's plans cite upstream tickets as evidence.

- **T6 — Server tests cover the empty-criteria path for all five**, including the paging claim specifically.
  `PvMetadataClientIT.java:858` (`testQueryPvMetadataUnsetLimitReturnsDefaultPageSize`) saves 101 PVs, asserts
  an unset-limit match-all returns exactly 100 with a non-empty `nextPageToken`, then repeats *with* criteria
  present to pin that the default is unconditional.  Also `PvMetadataClientIT.java:789`/`:812`,
  `ConfigurationIT.java:237`/`:856`, `ConfigurationClientIT.java:1168`/`:1195`/`:1302`,
  `QueryDataSetsIT.java:300`, `QueryAnnotationsIT.java:466`.

- **T7 — A server-side inconsistency exists but is invisible from Python.**  Four of the five build a sentinel
  match-all filter (`Filters.exists()` on an always-present field: `MongoSyncAnnotationClient.java:369`, `:707`,
  `:982`, `:1241`); activations use a literal empty document (`:1500`).  Behaviorally equivalent, since the
  probed fields are required on every document.  Noted so a future reader does not mistake it for a
  client-visible difference.  Out of scope.

- **T8 — The existing `iter_*` methods will do the right thing unchanged.**  Each loops on the page token and
  raises `RuntimeError` on a page error; none inspects `criteria` beyond passing it through.  So the change is
  confined to signatures, the `criteria or []` normalization, and docstrings.

- **T9 — `check_at_most_one_text_criterion()` is not involved.**  The three older query methods do not call it
  (only #6's datasets/annotations do, since only those criterion types have a `textCriterion` arm).  Confirmed
  by `grep`: no call in `pv_metadata_client.py` or `machine_config_client.py`.  Nothing to guard against an
  empty list there.

- **T10 — Baseline is green at the #40 commit**: 721 unit tests, ruff clean, 104 cookbook snippets checked.

## Design decisions

- **D1 — Mirror #6's shape exactly**, as #40 did for the criterion helpers: `criteria: list[...] | None = None`
  on the public methods and the private builders; `criteria = criteria or []` at the top of each public method
  (so the existing `len(criteria)` log lines stay correct); `if criteria:` guarding `request.criteria.extend()`
  in the builders.  Reference: `dataset_client.py:546-570` and `:588-615`.  Rejected: a separate
  `iter_all_*()` method per entity, which would double the surface for a case the protocol models as an empty
  list.

- **D2 — Keep `criteria` as the first positional parameter.**  Making it keyword-only would be cleaner in
  isolation but would break every existing positional caller for no benefit, and would diverge from #6.

- **D3 — Document the page-size consequence at each call site, not just in `conventions.md`.**  A user reaching
  for `query_pv_metadata()` with no criteria expecting "everything" gets 100 records and a token.  Each
  docstring already says `limit` is per-page; the browse-all sentence should name `iter_*` as the way to get
  the whole collection.  Per T4, say "a server default" without claiming it is configurable.

## Implementation tasks

Second commit in the #40 PR.

1. **`src/dp_python_lib/client/pv_metadata_client.py`** — `_build_query_pv_metadata_request` (405),
   `query_pv_metadata` (445), `iter_pv_metadata` (471) per D1/D3.
2. **`src/dp_python_lib/client/machine_config_client.py`** — the same for configurations (755/800/826) and
   activations (1113/1158/1184).
3. **Tests** — one build-request test per method asserting that omitted criteria produces an empty `criteria`
   list rather than a rejection, in `test_pv_metadata_client.py`, `test_machine_config_client.py`, and
   `test_machine_config_activation_client.py`.  Add an `iter_*`-with-no-criteria test for at least one entity,
   since the iterator is the method the browse-all case actually uses.
4. **`doc/cookbook/conventions.md`** — in the paging section, state that an omitted or empty criteria list
   matches all records, and that the server's default page size still applies, so `iter_*` is the right call
   for browse-all.
5. **`doc/cookbook/pv-metadata.md` / `machine-configuration.md`** — a short browse-all recipe where each file
   introduces querying.
6. **`CLAUDE.md`** — update the sentence at line 617-620 that names #40/#41 as pending back-ports; both are now
   done, so the five older helpers and six methods no longer diverge from #6's.
7. **Verify** — `pytest tests/unit/`, `ruff check .`, `ruff format --check .`, and
   `.dev/tools/check-cookbook-snippets.py`.

## Out of scope

- **The two v2 query methods** (`query_samples` / `iter_query_samples`).  Their `QueryParams` requires at least
  one of `pv_selector` / `config_criteria`, which is a different contract — a time-series query with no PV
  selection is not a browse-all, it is unbounded.  No change.
- **`get_datasets()` / other non-criteria methods.**
- **The server's sentinel-vs-empty filter divergence** (T7) — a dp-service cosmetic issue, no client impact.
- **Making the server page size configurable** (T4) — a dp-service question if anyone wants it.

## Dependencies and sequencing

- **Depends on nothing**, but is sequenced **after #40** in the same PR so the `conventions.md` edits stack
  cleanly (#40 rewrites the empty-input rule; #41 adds to the paging section — different sections, but the
  smaller edit lands first).
- No stub regeneration, no server change, no live server needed to verify.
- No release gating: the behavior is in dp-service `fddf692` and predates it for datasets/annotations.

## Open questions

- **Q1 — Should the browse-all recipes warn that the collection may be large?**  *Recommendation*: yes, one
  clause, since `iter_*` will happily page through an unbounded catalogue.  Not a separate section.
  **Resolved 2026-09-10**: accepted.
