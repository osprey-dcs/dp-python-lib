# Issue #58 — Accumulate release notes in a version-less NEXT.md

**Status:** implemented 2026-09-24 in the PR that closes #58.

## Overview

Release notes are written during a cycle instead of in one sitting at the cut.  A version-less draft,
`doc/release-notes/NEXT.md`, gains a ticket-organized section in the same PR as each user-visible
change, and is renamed to `rel-<version>.md` when the release is cut.  For whoever cuts releases and
reviews release-bearing PRs; readers of the release pages see no difference.

## Background / triage findings

- rel-1.16.0's notes were written and tagged the same day, so the reasoning behind each change had to
  be reconstructed after the fact.  The same happened in dp-grpc, which adopted `NEXT.md` in
  osprey-dcs/dp-grpc#156; the Java repos are taking it up for their next release.
- The next version is genuinely open (1.17.0 or 2.0.0).  Writing notes early under a guessed name
  does not work: `release.yml` resolves `doc/release-notes/${GITHUB_REF_NAME}.md` exactly, so a
  guessed file is stranded, and the tag that does ship fails its notes check.
- Since #56 every notes file hand-carries two parts that name the tag: the `## Verifying these
  artifacts` section (whose `--cert-identity` ends `@refs/tags/<tag>`) and the Full Changelog line.
  Neither can be written before the version is known, so they belong to the cut, not the draft.
- `release.yml` needs no change: it never looks at any file but the tag's.
- Merged since rel-1.16.0: #53 (docs link pin, not user-visible), #57 (#56 and #30).  The seed
  sections cover #56 and #30.

## Design decisions

**D1 — Follow dp-grpc's `NEXT.md` shape**: the same preamble (no upcoming version named; no claims
about what else the release contains) and a "Cutting the release" checklist in the file itself, so
the cut's steps are not knowledge someone has to still have.  *Rejected:* per-ticket fragment files
assembled at the cut — dp-grpc tried a fragment for #137 and retired it for `NEXT.md`.

**D2 — The checklist adds this repo's two tag-bearing steps** (verification section over the wheel,
sdist, and `SHA256SUMS`; Full Changelog line), plus running the checker.  It omits dp-grpc's
README release-notes table step, since this repo's README has no such table.

**D3 — Enforce "no version in `NEXT.md`" in `check-release-notes.py`.**  In `NEXT.md` the four
tag-bearing parts are errors: a verification heading, a `sigstore verify identity` command, a
`--cert-identity` with a value, a Full Changelog line.  Each is matched only as the real thing (line-
anchored heading, command, and changelog line; an identity followed by its value), because the
draft's own checklist names all four in prose.  The self-test holds that prose to passing and each
real form to failing, so loosening a pattern to an unanchored match fails loudly.  *Rejected:*
banning any `rel-X.Y.Z` string, which would also ban the legitimate "since 1.16.0" and links to
past releases.  The checker's default run now includes `NEXT.md` when present; it is not required to
exist.

## Implementation tasks

- `doc/release-notes/NEXT.md` — new: preamble, sections for #56 and #30, "Cutting the release".
- `.dev/tools/check-release-notes.py` — `check_next_text()`, `NEXT.md` in the default file set,
  self-test cases for a correct draft and each forbidden part.
- `CLAUDE.md` — "Cutting a release": the `NEXT.md` workflow and the inverted checker rule.

## Out of scope

- Choosing the next version; that happens at the cut, which is the point.
- Any `release.yml` change.

## Dependencies and sequencing

None.  Builds on #56's checker (merged in #57).

## Open questions

None.
