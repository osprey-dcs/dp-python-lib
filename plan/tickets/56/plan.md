# #56 + #30 — Release body as the notes file verbatim, and a mypy CI gate

**Status:** decisions settled 2026-09-24; open questions resolved in place.

## Overview

One change closing two tickets, both about CI and release tooling:

- **#56** — the rel-1.16.0 release page lost its artifact verification and install instructions,
  and nothing noticed.  This makes the reviewed notes file the *entire* release body, the same
  arrangement the three Java repos use (dp-grpc#137, plan D9), so no path to the release page can
  drop content the file contains, and checks that the published page matches the file.
- **#30** (near-term scope only) — makes `mypy src/` clean and enforces it in the CI `quality` job.
  The typed-stub half moved to osprey-dcs/dp-grpc#158.

For: release consumers (verification instructions that actually reach the page), and maintainers
(a type-check gate on hand-written client logic).

## Background / triage findings

### #56: the ticket's root cause is wrong

#56 attributes the missing content to the release **already existing** when the workflow ran,
so that `action-gh-release` kept the old body.  Its evidence is a timeline: release "created"
17:49:05Z, workflow started 18:02:17Z.  Both halves of that are contradicted:

- **GitHub's release `created_at` is the date of the tagged commit**, not when the release object
  was created (documented API behavior).  The rel-1.16.0 tag commit `17d2cdb` is dated
  `2026-09-16T11:49:05-06:00` — 17:49:05Z exactly.  rel-1.15.0 shows the same 35-minute "gap"
  and its body is correct.
- **The workflow created the release.**  The publish step's log (run 35131880732) reads
  `Creating new GitHub release for tag rel-1.16.0...`, and the release's author is
  `github-actions[bot]`.  It received the full 27,145-byte `RELEASE_BODY.md` and passed it as
  `body_path`.

So the body was right when published and was **replaced afterwards**.  The replacement is the
notes file alone: no verification section and no generated commit list, both of which only the
workflow adds.  That fits `gh release edit rel-1.16.0 --notes-file doc/release-notes/rel-1.16.0.md`,
run while fixing the dp-grpc link.  #56's own comment notes that the link was fixed on the page
first and only backported later (aa994d5, PR #53).  **Confirmed (Q1):** a notes-file edit made
in another session.

The page has since been corrected in place and now carries the verification and install
sections.  It does **not** carry the generated commit list (rel-1.15.0's does).

What this changes: the ticket's options A (`append_body`) and B (don't pre-create the release)
address something that didn't happen.  Its underlying concern still stands, and is sharper than
it said: **the page and the file are two copies of the same content, and the obvious way to
republish from the file loses whatever the workflow added.**  Option C removes the difference
between them.

The issue description gets a triage note saying this.

### #30: re-measured, and a config that reaches zero

Numbers and scope in #30 were updated 2026-09-24 (426 errors / 27 files).  Trialled locally:

| Configuration | Result |
|---|---|
| none | 426 errors, 27 files |
| + `types-PyYAML`, `types-protobuf`, `pandas-stubs`; `ignore_missing_imports` for `grpc` | import-untyped cleared |
| + `follow_imports = "skip"` for `dp_python_lib.grpc.*` **alone** | still 391 — `src/` passes the generated files on the command line, so they are checked regardless |
| + `exclude = ["^src/dp_python_lib/grpc/"]` | **7 errors, 3 files** |
| + `TimestampInput: TypeAlias = ...` | **3 errors** — the real ones |

The remaining 4 came from `TimestampInput`: once `common_pb2.Timestamp` resolves to `Any`, mypy
can no longer infer the union as an alias.  An explicit `TypeAlias` annotation fixes it and is
correct regardless.

**None of the 3 "real" errors is a runtime defect** (#30 calls them genuine; they are genuine
*type* errors):

- `mldp_client.py:120,130` — `self.annotation` / `self.query` are legitimately `None`; the
  annotations don't say so.
- `data_frame_conversions.py:209` — `data_frame_image_descriptors()` iterates
  `frame.imageColumns`, so `image_descriptor_dict()` can never return `None` there, but mypy can't
  prove it from a `column: Any` parameter.

Two constraints found in the trial:

- **Do not set `python_version = "3.10"`.**  numpy's bundled stubs use the 3.12 `type` statement,
  and mypy then refuses to check anything.  mypy checks against the running interpreter (3.12 in
  the `quality` job).  The 3.10 unit-test leg and ruff's `target-version = "py310"` already guard
  3.10 syntax.
- **The cookbook checker reads `[tool.mypy]` too** (it runs mypy with `cwd=REPO_ROOT`).  Verified
  with the config in `pyproject.toml`: `OK: 107 snippets in 8 files (5 skipped)`, unchanged.

## Design decisions

**D1 — The notes file is the release body, verbatim.**  `body_path` points at
`doc/release-notes/<tag>.md`, carried through the `dist/` upload as `RELEASE_NOTES.md`.  The
assembly step and its heredoc go.  Each release's notes carry a hand-written
"Verifying these artifacts" section, reviewed in the notes PR.  Then the page equals the file, and
`gh release edit --notes-file` is a lossless republish rather than a destructive one.
*Rejected:* `append_body: true` (#56 option A), which fixes a pre-existing-release case that
didn't occur and would duplicate the notes if it did; keeping assembly plus a post-publish check
(the check would catch a bad publish, but not a later hand edit, which is what happened).

**D2 — Add `README.env`, matching the Java repos.**  A version-controlled
"Release Artifacts and Verification" reference: asset list, `sha256sum -c` (with the
`--ignore-missing` caveat), and `sigstore verify identity`.  The per-release notes section stays
short and points to it.  The name is odd for a Python repo, but dp-grpc D9 treats it as the
convention across all three Java repos, and four repos agreeing is worth more than a better name.
No Python convention competes with it: PyPA defines no file for release-verification
instructions, and projects put them in their README or docs.  (Q3.)

**D3 — Drop `generate_release_notes`.**  It's the one part of the body not in the file, so it's
exactly what a republish from the file loses.  The notes are organized by ticket and link their
PRs, and the Java repos don't set it.  A hand-written `**Full Changelog**: …/compare/rel-A...rel-B`
line in the notes gives most of the value.  (Q2: agreed.)

**D4 — Check the notes content at PR time, not only at tag time.**  A new
`.dev/tools/check-release-notes.py` (sibling of the cookbook checker) runs in the `quality` job
over every `doc/release-notes/rel-*.md`.  It asserts a `## Verifying these artifacts` heading and
a `sigstore verify identity` command whose `--cert-identity` ends
`release.yml@refs/tags/<file stem>`.  The last check matters: the identity is now hand-written per
release, and a copy-pasted previous tag would verify nothing.  release.yml's existing
"notes file must exist" step calls the same script on the tagged file.  *Rejected:* tag-time only,
because by then the notes are merged and the tag is public, so a failure costs a re-tag.

**D5 — After publishing, compare the page to the file.**  A step after `action-gh-release`
fetches the body (`gh release view "$TAG" --json body`) and diffs it against `RELEASE_NOTES.md`,
ignoring trailing whitespace.  With D3 the comparison is exact.  This is the "nothing compares
assembled against published" gap #56 names, closed for the one path the workflow controls.
Later hand edits are covered by D6.

**D6 — Rule: release pages are never hand-edited.**  Fix the notes file by PR, then republish
with `gh release edit <tag> --notes-file doc/release-notes/<tag>.md`.  Recorded in CLAUDE.md.
Lossless under D1 + D3.

**D7 — Backport the verification section into `rel-1.16.0.md`.**  The live page has it, and the
file doesn't, so D4 would fail on it and D6's republish would strip it again.  After the backport,
file and page match (apart from the trailing newline).

**D8 — mypy config in `pyproject.toml`**, exactly as trialled:
```toml
[tool.mypy]
exclude = ["^src/dp_python_lib/grpc/"]

[[tool.mypy.overrides]]
module = ["dp_python_lib.grpc.*"]
follow_imports = "skip"

[[tool.mypy.overrides]]
module = ["grpc", "grpc.*"]
ignore_missing_imports = true
```
Each part carries a comment giving its reason, and a pointer to dp-grpc#158 for when the generated
package suppression can go.  No `python_version` (see Background).  *Rejected:* global
`ignore_missing_imports`, which would also hide a genuinely missing dependency.

**D9 — Fix `data_frame_conversions.py:209` by restructuring, not by casting.**  Split out a
private `_image_descriptor(column) -> dict[str, Any]` that assumes an `ImageColumn`.
`image_descriptor_dict()` keeps its `isinstance` guard and calls it, and
`data_frame_image_descriptors()` calls it directly.  *Rejected:* `cast` or `# type: ignore`, which
would hide the claim instead of making it true by construction.

## Implementation tasks

### #56

- `.github/workflows/release.yml`
  - Build job: replace "Assemble the release body" with copying the notes file to
    `dist/RELEASE_NOTES.md`.  Run `check-release-notes.py` on the tag's file in the existing
    notes-presence step.  Update the comment block that explains assembly and `body_path`
    precedence.
  - Publish job: `body_path: dist/RELEASE_NOTES.md`; remove `generate_release_notes` (D3); add
    the D5 compare step (`GH_TOKEN: ${{ github.token }}`, `set -euo pipefail`).
  - Sigstore signing: no change.
- `.dev/tools/check-release-notes.py` — new (D4).  Stdlib only; exits non-zero with a message
  naming the file and the missing or incorrect item.  Takes optional paths (default: all
  `doc/release-notes/rel-*.md`).
- `.github/workflows/ci.yml` — `quality` job: add a "Check release notes" step.
- `README.env` — new (D2).  Assets: wheel, sdist, `SHA256SUMS`, and a `.sigstore.json` bundle for
  each of the three.  Verify all three in one `sigstore verify identity` call, not only the wheel
  as today's body does.
- `doc/release-notes/rel-1.16.0.md` — append the verification and install sections exactly as on
  the live page (D7).
- `CLAUDE.md` — rewrite the "Release notes" paragraph (no assembly; `body_path` is the file; D6
  rule; `README.env`).
- `README.md` — check for any mention of the release body or verification and align it.

### #30

- `pyproject.toml` — D8 config; add `types-PyYAML`, `types-protobuf`, `pandas-stubs` to `[dev]`.
- `src/dp_python_lib/client/time_conversions.py` — `TimestampInput: TypeAlias = ...`.
- `src/dp_python_lib/client/mldp_client.py` — `AnnotationClient | None` / `QueryClient | None`
  on the attribute declarations.
- `src/dp_python_lib/client/data_frame_conversions.py` — D9.
- `.github/workflows/ci.yml` — `quality` job: `mypy src/` step, after ruff.
- `.dev/tools/check-cookbook-snippets.py` — update the stale "~145 of its own mypy errors"
  comment.  `--follow-imports=silent` stays, since the snippets should not re-report library
  errors either way.
- `CLAUDE.md` — CI description (quality job now runs mypy; mention the release-notes check);
  `[dev]` extra contents.

### Verification

- `mypy src/` → `Success`; cookbook checker unchanged; `pytest tests/unit` green; ruff clean.
- `check-release-notes.py` passes on `rel-1.16.0.md` after D7, and fails on a copy with the tag in
  the cert identity changed.
- `workflow_dispatch` rehearsal of release.yml on the branch: build and sign succeed.  Publish and
  the D5 compare are tag-gated, so they aren't exercised until rel-1.17.0.  The compare step can
  be dry-checked locally against rel-1.16.0 after D7 (`gh release view rel-1.16.0 --json body`).

## Out of scope

- Typed protobuf stubs and removing the D8 suppression — osprey-dcs/dp-grpc#158.
- `py.typed` marker — follow-up to dp-grpc#158 (typing the public surface is only useful once the
  protos are typed).
- Env-var vs YAML precedence bug — #19, independent.
- Publishing to PyPI (the disabled job) — unchanged.

## Dependencies and sequencing

- **Must merge before the `rel-1.17.0` tag**, or that release publishes through the old assembly
  path.
- Not blocked by dp-grpc#158, and doesn't block it.
- Not related to #19.
- D7 (the 1.16.0 backport) must land in the same PR as D4, or the new CI check fails on `main`.

## Open questions

All resolved 2026-09-24.

**Q1 — Was rel-1.16.0's body replaced with `gh release edit --notes-file` (or equivalent)?**
*Resolved:* yes, confirmed by the maintainer.  It was done in another session while fixing the
dp-grpc link.  The Background section and the #56 triage note state it as the cause.

**Q2 — Drop the generated commit list (D3)?**  *Resolved:* drop it, and add a hand-written Full
Changelog compare link to each release's notes.

**Q3 — `README.env` name (D2)?**  *Resolved:* `README.env`.  A Python-specific name would have
been fine, but none is conventional, so consistency with the Java repos wins.

## Implementation notes

Found while implementing, 2026-09-24:

- **The `mldp_client.py` fix moved errors into the cookbook, not out of existence.**  Typing
  `client.annotation` / `client.query` as `X | None` is correct (they are `None` when only an
  ingestion channel is passed; `doc/cookbook/connecting.md`), but it made 83 cookbook snippets
  fail `union-attr`.  The trial in Background measured the config, not this change.  Fixed with a
  single narrowing `assert` in the checker's preamble.  `--disable-error-code=union-attr` was tried
  first and rejected: on an `X | None` receiver that code also carries "no attribute on X", so it
  hid misspelled names too, and the checker's self-test canary failed.  End users are unaffected
  until a `py.typed` marker ships; at that point, revisit whether an unconfigured sub-client should
  raise on access instead of being `None`.
- **`timestamp_list()` now takes `Sequence[TimestampInput]`**, not `list[...]`.  With the explicit
  alias, a `list[datetime]` argument failed on list invariance (2 cookbook errors).
- **D5 diffs with `--strip-trailing-cr`**, not `--ignore-trailing-space`, which BSD `diff` lacks,
  so the step can be dry-run on macOS.  Trailing newlines are normalized by `"$(cat …)"`.
  Dry-run against rel-1.16.0: matches; a one-line change fails.
- **The D4 rationale, corrected:** a stale tag in `--cert-identity` doesn't "verify nothing".
  `sigstore verify` *rejects* every genuine artifact ("Certificate's SANs do not match"), which
  readers would take as a forged release.  Confirmed against the rel-1.16.0 assets, which verify
  with the correct identity (wheel, sdist, and `SHA256SUMS` in one call, as `README.env` shows).
