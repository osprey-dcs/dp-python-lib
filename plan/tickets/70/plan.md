# Issue #70 — Release notes checker: link rules, and the main-pinned link in rel-1.16.0

**Status:** implemented 2026-10-01 in the PR that closes #70.  Part of osprey-dcs/data-platform#98,
which holds the rules (R1–R5, N1–N3) and their rationale; they are not repeated here.

## Overview

`.dev/tools/check-release-notes.py` gains #98's link and placeholder rules, and becomes the copy the
other four repos take verbatim, changing only a configuration block.  rel-1.16.0's one `main`-pinned
link is fixed in the file and, after merge, on the published release page.  For whoever cuts
releases and reviews PRs that touch release notes or headings they link to.

## Background / triage findings

- **`blob/rel-1.16.0/README.env` is a 404.**  README.env was added in 700d2ed (#56), after the tag,
  together with the backported verification section that links it.  Repointing line 410 to the tag,
  as #70 and #98 say, would replace a drifting link with a broken one.  `NEXT.md`'s checklist already
  noted this ("`README.env` did not exist at 1.16.0").  See D3.
- **The release body is no longer generated.**  #98 (and its second comment) describe a body built
  by `cat`ing the notes and appending a verification section.  Since #56 the notes file is the body,
  verbatim (`release.yml` copies it to `dist/RELEASE_NOTES.md`), and the published rel-1.16.0 body is
  byte-identical to the file.  Regenerating it the way `release.yml` does is therefore the file
  itself: `gh release edit rel-1.16.0 --notes-file doc/release-notes/rel-1.16.0.md`.
- rel-1.16.0.md has one other duplicated heading (`### Methods`, twice), which no link points at.

## Design decisions

**D1 — Released notes are every `rel-*.md` except the highest version** (#98's suggestion).  Offline,
needs only the directory listing, and behaves the same in CI and in `release.yml`, which passes one
file; the comparison is still against that file's directory.  *Rejected:* "has a local git tag",
since CI checkouts and release checkouts differ in which tags they hold, and the release being cut
would count as released at tag time.  Consequence accepted: between cuts the latest release is
checked against the moving tree, so a rename breaking one of its links fails that PR.

**D2 — R4's duplicate-heading rule applies to the heading a link points at**, not to every heading
in the target.  A duplicate is only harmful when a link's anchor is one of the `x`/`x-1` pair;
flagging unlinked duplicates would fail rel-1.16.0 on its two `### Methods` for no benefit.

**D3 — R2 accepts a full 40-character commit SHA**, and rel-1.16.0's `[readme-env]` is pinned to
`blob/700d2edf44776ad86ee220836db1facf340d4629/README.env` (identical to `main`'s README.env today).
A SHA is immutable, so it has neither defect R2 exists for; short SHAs, `main`, and other tags still
fail, and NEXT.md (N2) still requires `main`.  SHA-pinned links get R2 only, like cross-repo links.
*Rejected:* the tag (404); leaving `main` behind a per-link allow marker (still drifts, and #70's
done-when is "no `main`-pinned links").  **Needs confirmation before merge** — it departs from #98's
wording of R2.

**D4 — One file, a marked configuration block.**  `REPOSITORY`, `NOTES_DIR_IN_REPO`, and four
identity switches (`SIGSTORE_PYTHON_RULES`, `COSIGN_IDENTITY_WORKFLOWS`, `COSIGN_IMAGE`,
`COSIGN_IDENTITY_REGEXP`); the block's comment gives each repo's values.  The cosign rules for
dp-service, dp-desktop-app, and dp-grpc are implemented and self-tested under their own
configurations in every copy, so a port is configuration only.

**D5 — Bare URLs and autolinks count for R2–R4**; GitHub links them.  R1 looks only at inline and
reference-definition targets.  Paths are matched case-sensitively per component, since macOS is not.

## Implementation tasks

- `.dev/tools/check-release-notes.py`: the rules, the configuration block, and self-test cases for
  each rule (good and bad, newest vs released, draft), all three cosign profiles, and the released
  rule's numeric ordering.  Verified by mutation: disabling any rule fails the self-test.
- `doc/release-notes/rel-1.16.0.md:410`: SHA pin (D3).
- `doc/release-notes/NEXT.md`: preamble paragraph on links; checklist steps 6 and 8 point at the
  checker.
- `CLAUDE.md`: the link rules and the released-notes rule, under "Release notes".
- No workflow change: CI and `release.yml` already run the script; the `push` guard stays.

## Out of scope

- Republishing the rel-1.16.0 page: done after merge, by hand, with the command above (#70's task).
- Porting to dp-grpc, dp-service, dp-desktop-app, data-platform: #98 steps 2–3.
- Recording the rule in data-platform's `CLAUDE.md`: #98.
