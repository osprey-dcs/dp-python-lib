# Plan Documents

Official, version-controlled plan documents for dp-python-lib work — design decisions and
implementation plans that benefit from PR review and a stable URL (for example, a plan a
GitHub issue links to, or one that reads a dp-grpc / dp-service plan cross-repo).

Layout: one directory per GitHub issue under `plan/tickets/<N>/`, typically containing
`plan.md` plus any companion documents (design notes, review records). Other kinds of plans
may get sibling directories later (e.g., `plan/releases/`).

Lifecycle:

- Draft and iterate in `~/dp/dev/tickets/dp-python-lib/<N>/`, outside the repo; promote a
  document here once its decisions are settled, so the plan gets review without churning the
  repo during drafting.
- A committed plan is a **point-in-time record**, not a living document. It describes intent
  at the time of writing; the code, `CLAUDE.md`, and the cookbook are authoritative for
  current behavior.
- Merged plans are not retro-edited to track later changes. If a plan turns out to be wrong
  about something material, add a dated correction note at the top rather than rewriting it.

This replaces the earlier practice of keeping plans in the gitignored `.dev/plan/issue-<N>/`
directory, where they were invisible to reviewers, to CI, and to anyone working from a fresh
clone. `.dev/` still holds scratch material and tooling; do not add new ticket plans there.
