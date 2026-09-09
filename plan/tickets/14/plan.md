# Issue #14 — Extract a shared `_dispatch` helper for the unary `_send_*` methods

## Overview

Collapse the 18 near-identical unary `_send_*` bodies across the five service clients into short
delegations to one `_dispatch()` helper on `ServiceApiClientBase`.  The three-tier error handling
(gRPC exception / business `exceptionalResult` / unexpected exception) is written once instead of
eighteen times, and each new sender added by #6 costs ~6 lines rather than ~40.

This is a pure refactor: no public API changes, no behavior changes, no new tests of new behavior.
It is deliberately sequenced *before* #6 (issue comment 2026-09-09, and `plan/tickets/6/plan.md` Q3)
so #6's 10 new senders are written in the collapsed form from the start.

## Background / triage findings

Verified against the code on `main` at b6d0b37, not taken from the ticket text.

- **T1 — The count is 18 unary senders, and the ticket's corrected figure is right.**  `grep -c "def _send_"`
  reports 20 across `src/dp_python_lib/client/`; two of those are the server-streaming senders
  (`_send_query_sample_statuses_stream`, `_send_query_samples_stream`), leaving 18 unary.  By client:
  `MachineConfigClient` 9, `PvMetadataClient` 4, `SampleStatusClient` 3, `QueryClient` 1,
  `IngestionClient` 1.  The issue's *original* body undercounts (it names only the 13 annotation-service
  senders it knew about); its 2026-09-09 correction comment and the #6 plan's finding 5 both say 18 and
  are accurate.

- **T2 — All 18 result classes share one constructor shape.**  Every `*ApiResult` extends `ApiResultBase`
  with `__init__(self, is_error: bool, message: str, response: <Response> | None = None)`.  So a helper
  can construct any of them uniformly as `result_cls(is_error=..., message=..., response=...)`, and the
  `result_cls` parameter in the ticket's sketch is sound.  No result class takes extra required arguments.

- **T3 — Success-branch logging is *not* uniform, and the ticket's signature cannot carry it.**  This is
  the one real gap in the ticket.  Each sender logs a bespoke INFO line on success; four of them report a
  count read off the response body:
  - `"QueryPvMetadata returned %d records", len(response.pvMetadataResult.pvMetadata)`
  - `"QueryConfigurations returned %d records", ...`
  - `"QueryConfigurationActivations returned %d records", ...`
  - `"GetActiveConfigurations returned %d records", ...`
  - `"Successfully saved %d sample status(es)", response.saveSampleStatusesResult.savedCount`
  - `"Successfully deleted %d sample status(es)", response.deleteSampleStatusesResult.deletedCount`
  - `"Successfully queried %d sample status bucket(s)", ...`

  and the rest name the entity (`"Successfully saved PV metadata for: %s", request.pvName`).  A literal
  reading of `_dispatch(stub_call, request, result_cls, success_field, op_name)` would silently drop all
  18.  Resolved by D2 below.

- **T4 — The entry-log lines vary the same way.**  `"Calling savePvMetadata API for PV: %s", request.pvName`
  vs. bare `"Calling querySamples API"` vs. `"Calling saveSampleStatuses API with %d frame(s)"`.  Same
  problem, same resolution (D2).

- **T5 — `IngestionClient` alone guards `e.code()`.**  Its `except grpc.RpcError` block wraps `e.code()` in
  a nested `try/except (AttributeError, TypeError)` with the comment "may not be available in test mocks";
  the other 17 log only `e.details()`.  This is not incidental: `test_send_register_provider_grpc_error`
  raises a bare `grpc.RpcError()`, on which `code()` genuinely does not resolve.  Resolved by D3.

- **T6 — No unit test asserts on log output.**  `grep -rn "caplog\|assertLogs" tests/unit/` returns nothing,
  so log-message changes cannot break the suite — which means the suite would *not* have caught T3's
  silent log loss.  That is an argument for preserving the messages deliberately (D2), not for relying on
  the tests to police them.

- **T7 — The existing tests exercise `_dispatch` as-is.**  Every sender test mocks the stub method
  (`mock_stub.savePvMetadata.side_effect = ...`) and calls `_send_*` directly.  `_dispatch` still calls
  that same stub method, so all 406 tests on `main` cover the refactored path with no edits.  Baseline
  before the change: 406 passed.

## Design decisions

- **D1 — `_dispatch` lives on `ServiceApiClientBase`, covering all five clients.**  Not on an
  annotation-only mixin.  The shape is identical in `IngestionClient` and `QueryClient`, the base class
  already owns `self._stub` and `self.logger`, and #6's senders inherit it for free.  (Recorded in the #6
  plan finding 5 / Q3.)  Rejected: a helper module-level function, which would have to be handed the
  logger and the stub on every call.

- **D2 — Optional `request_log` / `success_log` callables preserve every existing log line verbatim.**
  `_dispatch` takes `request_log: Callable[[], None] | None` and `success_log: Callable[[Any], None] | None`
  (the latter receiving the response).  Callers that log a bare message pass nothing and get
  `_dispatch`'s default `"Calling <opName> API"` / `"<OpName> completed successfully"`; the ~13 senders
  with entity- or count-bearing messages pass a one-line lambda that keeps their exact text.  Each sender
  ends up 4–8 lines instead of ~40.

  Rejected: (a) dropping the bespoke messages for a uniform one — operators lose record counts and PV
  names from INFO-level logs, an observability regression a pure refactor has no business making, and T6
  shows nothing would flag it; (b) moving the detail into the public wrapper methods — no log content is
  lost, but it relocates messages to a different call site and grows 18 public methods, which is a larger
  diff than the one it avoids.

- **D3 — The `e.code()` guard becomes shared behavior.**  `_dispatch` logs the gRPC code when it resolves
  and omits it when it does not, exactly as `IngestionClient` does today.  This strictly improves the
  other 17 (a code in the log where one exists) and cannot regress any of them, since the message text
  returned in the result — `f"gRPC error: {e.details()}"` — is unchanged in every case.  Rejected:
  dropping the guard, which would break `test_send_register_provider_grpc_error`'s bare `grpc.RpcError()`.

- **D4 — The two server-streaming senders are out of scope and stay as they are.**  Their control flow is
  a `for` loop over a response stream that *yields* results (including error results) rather than
  returning one, and their documented contract — errors yielded, not raised, for the public `iter_*`
  wrapper to convert — is not the unary contract.  Forcing both through one helper would parameterize
  away the difference that matters.  (Consistent with the ticket and the #6 plan.)

- **D5 — Error-message text is byte-identical to today's.**  `f"gRPC error: {e.details()}"`,
  `f"Unexpected error: {e!s}"`, and
  `f"Unexpected response format: neither exceptionalResult nor {success_field} found"` are reproduced
  exactly, because ~40 existing assertions match on these strings with `assertIn`.  The refactor is
  verified by the unchanged suite; any diff in these strings would be a behavior change smuggled into a
  refactor.

## Implementation tasks

1. **`src/dp_python_lib/client/service_api_client_base.py`** — add `_dispatch()`, typed
   `(stub_call: Callable[[Any], Any], request: Any, result_cls: type[T], success_field: str,
   op_name: str, request_log=None, success_log=None) -> T`, with `T` bound to `ApiResultBase`.  Implements
   the three tiers per D3/D5 and the default log lines per D2.
2. **`pv_metadata_client.py`** — collapse 4 senders.
3. **`machine_config_client.py`** — collapse 9 senders.
4. **`sample_status_client.py`** — collapse 3 senders (leave the streaming one).
5. **`query_client.py`** — collapse 1 sender (leave the streaming one).
6. **`ingestion_client.py`** — collapse 1 sender.
7. **`tests/unit/test_service_api_client_base.py`** — new, direct coverage of `_dispatch` itself: success,
   business error, unrecognized response, `grpc.RpcError` with and without a resolvable `code()`, general
   exception, and that `request_log`/`success_log` fire (and that omitting them is fine).  The 18 senders
   are already covered by the existing per-client tests (T7); this file covers the helper's own branches,
   including the `code()` guard that no per-client test reaches.
8. **`CLAUDE.md`** — document `_dispatch` under the "Client Implementation Pattern" and "gRPC Error
   Handling" guidelines, so #6 and later work write senders in the collapsed form.

## Out of scope

- The two server-streaming senders (D4).
- Any change to public method signatures, result classes, or error-message text (D5).
- Relaxing the empty-`values` criterion helpers — [#40](https://github.com/osprey-dcs/dp-python-lib/issues/40).
- #6's own senders — they are written against this helper once it lands, in that ticket.

## Dependencies and sequencing

- Blocks nothing hard, but lands **before** #6 by decision (#6 plan Q3) so #6's 10 senders are written
  collapsed.  Own small PR off `main`.
- Does **not** depend on the stub sync (#39, merged as b6d0b37): `_dispatch` is generated-code-agnostic.
- Does **not** depend on #40 or #17.

## Open questions

- **Q1 — How to preserve the bespoke success/entry log messages.  RESOLVED 2026-09-09: optional
  `request_log`/`success_log` callables (D2).**  Confirmed with the repo owner; the alternatives
  considered and rejected are recorded in D2.
