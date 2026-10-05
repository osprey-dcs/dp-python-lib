# Issue #75 — Cookbook checker: verify each recipe imports the names its snippets use

**Status:** triaged and implemented 2026-10-02; plan and implementation land in one PR (the ticket
is small enough that a separate plan-only PR was declined).

## Overview

`.dev/tools/check-cookbook-snippets.py` gains a third pass: every name a `# cookbook:partial` snippet
uses that the checker preamble *imports* must also be bound somewhere in the snippet's own recipe.
The six recipes' "Imports used by the examples" blocks stop being `# cookbook:skip`, so they are
type-checked themselves and supply the bindings the new pass looks for.  `conventions.md` and
`connecting.md`, the two pages that rely on preamble imports today, get imports of their own.  For
cookbook authors and reviewers: the class of miss that #74 hit twice becomes a CI failure.

## Background / triage findings

- **The draft's diagnosis holds.**  The preamble (`PREAMBLE`, checker L70–188 before this change,
  L76–194 after it) imports 66 names, and every partial snippet is checked with it prepended, so a
  recipe's own imports are never consulted.
  A prototype of the proposed rule (pure `ast`, below) run against the cookbook as it stood before each
  #74 fix reports exactly the two misses the ticket names: `dfc` in `query.md` (at `eedae6a~1`), and
  `QueryParams` / `PV` / `bc` in `ingestion.md` (reconstructed by deleting those three imports, since
  that fix landed inside 00d9487 rather than as its own commit).  Against today's six recipes it
  reports nothing.
- **The imports blocks themselves are never checked — a gap the ticket does not mention.**  All six
  "Imports used by the examples" blocks open with `# cookbook:skip`, as they have since the cookbook
  was written (495ed2f, #15); they are the only skipped blocks in the cookbook.  So a misspelled name
  *in the imports block* passes today: changing `PvQuery as PV` to `PvQuerry as PV` and
  `query_conversions` to `query_conversion` in `query.md` is reported by nothing.  With the skip
  removed, mypy flags both (`Module "dp_python_lib.client" has no attribute "PvQuerry"`), and the
  current cookbook still passes (128 snippets, 0 skipped).  This also settles the draft's step 1:
  the blocks have to be checked anyway, and once they are, their bindings need no special handling.
- **The `conventions.md` / `connecting.md` question in the draft resolves to "real hits".**  Both
  uses are in fenced, checked code blocks, not prose:
  - `conventions.md` uses `Q` (L69 onward), `SavePvMetadataRequestParams` (L235, L241), and
    `to_timestamp` (L269 onward) and has no imports block at all.  It is a cross-cutting page that
    recipes link to, but a reader copying from it meets the same `NameError`.
  - `connecting.md`'s "Sub-clients can be None" block (L175) uses `QueryParams` and `PV`.  Every
    other block on the page is standalone and imports what it uses.
  Decision (2026-10-02): give both pages imports rather than an opt-out directive (D5).
- **The draft's prose hits need no special handling.**  The checker only ever sees fenced
  ` ```python ` blocks, so `Q.attributes(...)` in a callout or `dfb.serialized_column()` in a bullet
  is invisible to it by construction.
- **`ruff --select F821` is the wrong tool here** (the draft offers it as one option).  mypy already
  reports any name the preamble does not define, so the only gap is names the preamble *does* define;
  F821 over a concatenated recipe would re-report mypy's errors and need a stub header for the
  fixtures.  It would also tie the checker to ruff, while the checker's one external tool (mypy) is
  deliberately resolved from the running interpreter.
- **No open ticket folds in.**  #61's step 5 waits on a stub sync carrying `.pyi` files (dp-grpc#158
  is closed, but no `grpc-sync-*` PR has brought them, and dp-grpc's latest release is still
  rel-1.16.0); its "checker gets stricter" item is about mypy, not imports.  #68 waits on
  osprey-dcs/dp-grpc#165 and osprey-dcs/dp-service#302, both open.  #76 (PyPI) touches the cookbook's
  install text only.

## Design decisions

**D1. Pure `ast`, no new dependency.**  The pass parses each snippet (already done in pass 1) and
walks it.  Rejected: `ruff --select F821` (above).

**D2. "Used" means a `Load` of a name the preamble imports.**  The set is computed by parsing
`PREAMBLE`'s own `import` / `from … import` statements (asname if present, else the top-level module
for `import a.b`, else the imported name), so it tracks the preamble with no second list to maintain.
The preamble's fixtures (`client`, `begin`/`end`, `params`, `t0`/`t1`, the carried-forward ids,
`since`, `request`, `acquire()`) are not imports and are therefore out of scope automatically, as
the ticket asks.

**D3. "Bound" means bound anywhere in the same file's checked blocks.**  Imports (`import`,
`from … import`, with `as`), assignment and other `Store` targets (`for`, `with … as`,
comprehensions, walrus), `def` / `class` names, parameters, and `except … as` names, collected from
every non-skip block in the file: the imports block, inline imports, standalone blocks, and
`no-mypy` blocks.  Order and scope are ignored: a name imported in a later block, or inside a
function, still counts.  That is looser than Python, but recipes are flat scripts and no real case
needs the strictness; the job is "does the recipe import it at all", which is exactly what failed in
#74.  Rejected: order-sensitive, module-level-only binding (more code, no case it would catch).
`# cookbook:skip` blocks bind nothing, because they may be pseudo-code or deliberately wrong.

**D4. Only partial blocks are checked for uses.**  A standalone block is checked without the
preamble, so mypy already reports a missing import there as `Name "x" is not defined`; checking it
again would double-report.  Partial `no-mypy` blocks *are* checked, since this pass does not depend
on mypy.

**D5. Unskip the imports blocks; add imports to the two shared pages.**  Each recipe's imports block
loses `# cookbook:skip` and is then a standalone block like any other: syntax-checked, type-checked,
and a source of bindings.  No new directive.  `conventions.md` gets its own "Imports used by the
examples" block (after the intro, before Contents, matching the recipes); `connecting.md`'s one
partial block gets an inline `from dp_python_lib.client import QueryParams, PvQuery as PV`, since the
page has no imports block and every other block there is self-contained.  Rejected: a documented
opt-out directive for shared pages — it would exempt exactly the pages most often copied from.

**D6. One error per name per file, at its first use.**  Format:
`doc/cookbook/query.md:434: 'dfc' is used but never imported by this recipe (the checker preamble
supplies it; add it to the recipe's imports) [+3 more uses]`.  Sorted with the other errors by file
and line.

**D7. Self-test, in the style of the existing canary.**  Before the real run, the pass is fed two
synthetic recipes: one partial snippet using `dfc` with no import anywhere (must report `dfc`), and
the same snippet plus a separate block importing `data_frame_conversions as dfc` (must report
nothing).  Also asserted: the preamble-import set is non-empty and contains `dfc`, so a preamble
refactor that the parser stops seeing (say, imports moved under a conditional the walk skips) fails
loudly.  The first case catches a rule that stops matching; the second catches one that ignores
bindings and would then fail on the real cookbook for a confusing reason.  Both run under the
existing `--no-self-test` switch.

**D8. No `NEXT.md` entry.**  The change is to repository tooling and to cookbook import lines; no user
of the library or its release artifacts behaves differently.  Same call as #70.

## Implementation tasks

**`.dev/tools/check-cookbook-snippets.py`**
- Module docstring: "Two passes" becomes three; describe pass 3 and why it exists (the preamble
  masks a recipe's own imports).
- `preamble_imports() -> set[str]` from `ast.parse(PREAMBLE)`.
- `bound_names(tree) -> set[str]` per D3.
- `check_recipe_imports(snippets) -> list[str]`: group by `snippet.path`; bindings from every
  non-skip snippet that parses; uses from partial non-skip snippets; errors per D6.
- `self_test()`: add the D7 cases next to the canary; report failures in the same
  `SELF-TEST FAILED: ...` form.
- `main()`: run pass 3 after pass 1, over snippets that parsed (it needs the AST; a syntax error is
  already reported).  As built it also runs before pass 2: it needs nothing from mypy, and the
  errors are sorted by file and line afterwards anyway, so the order is invisible in the output.

**Cookbook**
- Remove `# cookbook:skip` from the imports block in `datasets-and-annotations.md`, `ingestion.md`,
  `machine-configuration.md`, `pv-metadata.md`, `query.md`, `sample-status.md`.
- `conventions.md`: add "### Imports used by the examples" with `from dp_python_lib.client import
  MldpClient, SavePvMetadataRequestParams, PvMetadataQuery as Q, to_timestamp`.  As built, those
  are the three names the pass reported plus `MldpClient`, listed to match the other recipes'
  blocks; `datetime` / `timezone` were left out because the page's one block using them already
  imports them inline.
- `connecting.md` L175 block: inline import per D5.
- `README.md` "Verifying the examples": add that each recipe must import what its snippets use, and
  that this is checked; the directives line no longer needs to cover the imports block.

**`CLAUDE.md`**
- In Linting and Formatting, where the checker is first mentioned, one sentence: the checker also
  enforces that each recipe's own imports cover its partial snippets, because the shared preamble
  would otherwise hide a missing import.  It still cannot check values carried *between* snippets;
  running a recipe as a script with only its own imports remains the way to verify those.

**Verification**
- `python .dev/tools/check-cookbook-snippets.py` passes on the branch.
- Against `git show eedae6a~1:doc/cookbook/query.md` and an `ingestion.md` with the three imports
  removed, it reports `dfc` and `QueryParams` / `PV` / `bc` respectively.
- Misspelling a name in an imports block fails the run.
- `ruff check` / `ruff format --check` stay clean, the checker included: `.gitignore` re-includes
  `.dev/tools/`, so ruff lints and formats it like other source.  CI needs no change, since the
  quality job already runs the script.

## Out of scope

- Checking values carried between snippets (`dataset_id`, `provider_id`, …).  The preamble seeds
  those deliberately; a recipe binding the wrong name is still caught only by running it as one
  script, as the preamble's own comment says.
- Pruning preamble imports no recipe uses (e.g. the `typing` aliases).  Harmless once the new pass
  exists, and unrelated.
- Making the checker importable as a module (its `@dataclass` needs `sys.modules` registration when
  loaded by path); the self-test lives inside it, so nothing needs that.

## Dependencies and sequencing

None.  Independent of #61, #68, and #76.

## Open questions

Resolved 2026-10-02:

1. **Shared pages: imports or opt-out?**  Imports (D5).
2. **PR shape?**  One PR carrying the plan and the implementation, `Closes #75`.
