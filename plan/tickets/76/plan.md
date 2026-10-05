# Issue #76 — Publish releases to PyPI

**Status:** triaged 2026-10-05; open questions resolved the same day.  Implementation in progress on
`feat/76-pypi-publish`.

## Overview

Enable the `publish-pypi` job in `release.yml` so that a `rel-X.Y.Z` tag publishes the same wheel and
sdist to PyPI that it already attaches, signed, to the GitHub Release.  Users (a SLAC user asked for
this) can then `pip install "dp-python-lib[analysis]"` instead of downloading a wheel by hand.  The
GitHub Release, its Sigstore bundles, and `SHA256SUMS` are unchanged; PyPI becomes a second channel
for byte-identical files, and that identity is checked after every publish.

The work is part account setup (done by hand on pypi.org, test.pypi.org, and the repo's settings) and
part repo change: the workflow, a TestPyPI rehearsal path, the install docs, and the release checklist.

## Background / triage findings

- **The draft's premise holds: the job is nearly complete.**  `publish-pypi` already uses Trusted
  Publishing (`id-token: write`, environment `pypi`), strips `SHA256SUMS` / `RELEASE_NOTES.md` /
  `*.sigstore.json` before upload, sets `attestations: true`, and pins `pypa/gh-action-pypi-publish`
  by SHA.  It has been gated `if: false` since the workflows were added (114c12f, #29).  The repo is
  public, so environment protection rules are available.
- **The name is free.**  `https://pypi.org/pypi/dp-python-lib/json` and its test.pypi.org counterpart
  both return 404 (checked 2026-10-05).
- **Correction to the draft: a name cannot be claimed on PyPI ahead of an upload.**  The draft's first
  task, "check that `dp-python-lib` is available on PyPI and claim it", has no direct equivalent.  The
  mechanism is a *pending* trusted publisher, registered from the PyPI account page before the
  project exists.  It does **not** reserve the name; the project is created by the first upload made
  through it.  PyPI's similar-name check also only runs at upload time.  The risk of losing the name
  before the next release is low, and the TestPyPI rehearsal (D3) exercises the same path.
- **Correction to the draft: a TestPyPI rehearsal from `workflow_dispatch` would be rejected as
  things stand.**  A dispatch build is versioned by setuptools-scm with a local segment
  (`1.16.1.dev61+g496f0e0` at `496f0e0`), and PyPI and TestPyPI both refuse local versions.
  `SETUPTOOLS_SCM_OVERRIDES_FOR_DP_PYTHON_LIB='{local_scheme="no-local-version"}'` yields
  `1.16.1.dev61`, which they accept (verified locally with setuptools-scm 10.3.4).
- **Missing from the draft: the README is PyPI's project page and has relative links.**
  `readme = "README.md"` becomes the long description, and its relative links (`doc/cookbook/*.md`,
  `README.env`) 404 on pypi.org.  `twine check --strict` does not catch this, since it validates
  markup, not targets.
- **Missing from the draft: the cookbook already claims a PyPI install.**  `doc/cookbook/conventions.md`
  ("Optional dependencies") and `doc/cookbook/query.md` ("Getting results into pandas and NumPy") both
  say `pip install dp-python-lib[analysis]`, which fails today.  Unquoted, it also fails in zsh, the
  macOS default shell, because of the brackets.  `README.md` (Installation, and the roadmap bullet
  "Publishing to PyPI"), `README.env` (Notes: "Publishing to PyPI is not enabled yet"), the
  `## Installing` section of `doc/release-notes/NEXT.md`, and `CLAUDE.md` (Continuous Integration and
  Releases) all describe the current state and need updating.
- **Missing from the draft: the PyPI job runs in parallel with the GitHub Release.**  It has
  `needs: build` only, so the one upload that can never be replaced may land before the GitHub Release
  has been published and its body verified, or after it has failed.
- **Missing from the draft: a job naming an environment that does not exist creates it, unprotected.**
  So the `pypi` environment and its reviewer must exist **before** the workflow change merges; otherwise
  the first tag push creates `pypi` with no approval gate.
- **The sdist contains every tracked file** (setuptools-scm's file finder): `plan/`, `.github/`,
  `CLAUDE.md`, `specifications/`, `tests/`, and the one tracked `.dev/` file, about 500 KB.  That is the
  same sdist the GitHub Release already ships and signs, so it is not changed here (see Out of scope).
- **No open ticket folds in.**  #68 (paging for `queryRequestStatus()`) waits on osprey-dcs/dp-grpc#165
  and osprey-dcs/dp-service#302.  #61's `py.typed` marker becomes more useful once the package is on
  PyPI, but it still waits on a stub sync carrying `.pyi` files, so it stays there.  No other osprey-dcs
  repo has a PyPI ticket.

## Design decisions

**D1. Ownership: two personal PyPI owners now, an organization later if wanted.**  The project is
created under Craig's account through the pending publisher.  A second owner, with 2FA (which PyPI
requires), is added as soon as the project exists, which is right after the first upload.  Rejected:
waiting for a PyPI organization, because PyPI's admins review organization requests and the SLAC user
would wait on that.  A project can be moved into an organization later without republishing.
*(Decided 2026-10-05.)*

**D2. One required reviewer on the `pypi` environment: Craig.**  The environment also gets a deployment
rule allowing only `rel-*` tags, as a second gate besides the job's `if:`.  Since the same person pushes
the tag and approves the deployment, GitHub's "prevent self-review" option must stay **off**.  The
approval is a deliberate pause before an upload that can never be replaced, not a second pair of eyes.
That bus-factor-of-one is accepted: if the approver is unavailable, the GitHub Release still ships and
PyPI follows later by approving the pending deployment or re-running the job.  That fallback lasts only
as long as the `release-dist` artifact, which `publish-pypi` downloads and which is uploaded with
`retention-days: 7`, while GitHub holds a deployment for approval for up to 30 days.  Retention
therefore goes to 30 days to match the approval window.  A full workflow re-run is no substitute once
the artifact has expired: it rebuilds and re-signs, so the files would no longer match the
`SHA256SUMS` and bundles already on the GitHub Release, and it re-runs the release step against a
release that already exists.  *(Decided 2026-10-05; the retention window was added in review of PR #78.)*

**D3. A permanent TestPyPI rehearsal behind an opt-in dispatch input.**  `workflow_dispatch` gains a
boolean input `testpypi` (default `false`).  When it is set, a `publish-testpypi` job uploads the
rehearsal build to test.pypi.org through its own trusted publisher and a `testpypi` environment (no
reviewer).  The dispatch build drops the local version segment using the setuptools-scm override above,
which is set on the build step only for `workflow_dispatch`, so tag builds are unaffected.  This changes
the workflow's stated invariant from "a dispatch never publishes" to "a dispatch never publishes to PyPI
or a GitHub Release", and the header comment says so.  Rejected: a one-off twine upload with an API
token, which checks rendering but not the OIDC/trusted-publisher configuration that is the actual risk;
and skipping TestPyPI, which makes the first real release the first test.  TestPyPI is a sandbox, but
its versions cannot be re-uploaded either.  *(Decided 2026-10-05.)*

**D3a. Both upload steps set `skip-existing: true`, and D5 is what keeps a conflict loud.**  The upload
sends files one at a time, so a wheel can land and the sdist fail.  Without `skip-existing`, every
"Re-run failed jobs" after that fails on the wheel with "File already exists", and the release can never
be completed on PyPI.  A re-run downloads the same `release-dist` artifact rather than rebuilding, so a
skipped file is the same file; D5's digest check then compares every file on the index, skipped ones
included, against `SHA256SUMS`.  A genuine conflict (a rebuilt sdist at a version already on the index,
from a fresh dispatch at the same commit, say) still fails, at the digest check, with both digests
printed.  Rejected: leaving `skip-existing` off so the upload itself fails loudly, which makes a partial
upload unrecoverable without a new version.  *(Added in review of PR #78.)*

**D4. PyPI publishes after the GitHub Release, not beside it.**  `publish-pypi` gets
`needs: [build, publish-github-release]`.  The GitHub Release can be edited or deleted; a PyPI file
cannot.  Publishing PyPI last means a failure anywhere earlier, including the body check, stops before
the irreversible step.  If the PyPI job itself fails, "Re-run failed jobs" retries it with the same tag
and the same OIDC identity.  Rejected: keeping the jobs parallel, which saves about a minute.

**D5. After publishing, check that PyPI serves the same bytes as the GitHub Release.**  A final step
polls the index's JSON API (`/pypi/dp-python-lib/<version>/json`, retrying for a few minutes while the
CDN catches up) and compares each file's `digests.sha256` with `SHA256SUMS`.  The existing step that
deletes `SHA256SUMS` from `dist/` before upload therefore moves a copy aside first.  The same step runs
after the TestPyPI upload against test.pypi.org.  This closes the loop the same way the body check does
for the GitHub Release, and it is what makes "the PyPI files are the signed files" a checked statement,
so `README.env` can tell a user to verify a `pip download` against `SHA256SUMS` from the GitHub
Release.  That recipe must be spelled out, because the obvious one fails: `SHA256SUMS` lists both the
wheel and the sdist, so a bare `sha256sum -c` over a directory holding only the wheel reports the sdist
missing and exits non-zero, and `pip download` without `--no-deps` also fetches every dependency.  The
documented form is:
```bash
pip download --no-deps "dp-python-lib==X.Y.Z"
sha256sum --ignore-missing -c SHA256SUMS
```
Rejected: a post-publish `pip install` smoke test, which the build job already does
against the same wheel; it would add resolver and CDN flakiness without checking anything new.

*Implementation note:* the check is a script, `.github/scripts/check-index-digests.py`, rather than
inline shell in both jobs, so it is linted, self-tested on each run, and runnable locally against any
project on PyPI.  The publish jobs fetch it with a sparse checkout of `.github/scripts` at the run's
ref, which adds `contents: read` to their permissions.

**D6. Only the next release goes to PyPI.**  rel-1.16.0 and earlier are not backfilled: that would be a
hand upload with an API token and no PEP 740 attestations, of a release carrying the #19
config-precedence bug.  The first PyPI version is whatever the next tag is.  *(Decided 2026-10-05.)*

**D7. README links become absolute `blob/main` URLs.**  They then work on GitHub, on pypi.org, and in
the sdist alike.  This includes the fragment-only links (`[current state](#current-state)`,
`[TODO](#todo)`), which become `blob/main/README.md#...`: whether PyPI's renderer gives headings `id`s
was not confirmed, and an absolute link works either way.  The TestPyPI rehearsal's link check covers
them.  A version's PyPI page will link to the docs as they are on `main`, not as they were
at that release; that drift is accepted, because the alternative (rewriting links at build time) adds
moving parts to the release build for a landing page.  `[project.urls]` gains `Documentation`
(`doc/cookbook/README.md` on `main`) and `Changelog` (the GitHub Releases page) for the PyPI sidebar.

**D8. Docs lead with PyPI and keep the verified-download path second.**  The install commands are
quoted (`pip install "dp-python-lib[analysis]"`) so they work in zsh.  Editable installs stay
documented for development.

## Implementation tasks

### Manual setup (before the workflow PR merges; see Dependencies)

1. **PyPI:** under Craig's account, add a pending trusted publisher: project `dp-python-lib`, owner
   `osprey-dcs`, repository `dp-python-lib`, workflow `release.yml`, environment `pypi`.
2. **TestPyPI:** a separate account on test.pypi.org; the same pending publisher with environment
   `testpypi`.
3. **GitHub environments** (Settings → Environments):
   - `pypi`: required reviewer Craig, "prevent self-review" off, deployment branches and tags limited
     to the tag pattern `rel-*`.
   - `testpypi`: no reviewer and no ref restriction, so a rehearsal can run from a PR branch.
4. **After the first real upload:** add the second PyPI owner (D1) and confirm the project shows the
   attestations and the trusted publisher (no API tokens).

### `.github/workflows/release.yml`

- Header comment: state the revised dispatch invariant (D3).
- `workflow_dispatch.inputs.testpypi`: boolean, default `false`, description naming test.pypi.org.
- Build step: set `SETUPTOOLS_SCM_OVERRIDES_FOR_DP_PYTHON_LIB: '{local_scheme="no-local-version"}'`
  only when `github.event_name == 'workflow_dispatch'`, with a comment explaining why (PyPI rejects
  local versions).
- `publish-pypi`: `if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/rel-')`,
  `needs: [build, publish-github-release]`; replace the "WIRED UP BUT INTENTIONALLY DISABLED" block
  with a short comment that names the one-time setup (pending publisher, environment) and points to
  this plan.  Keep `attestations: true` explicit.
- New `publish-testpypi` job: `needs: build`,
  `if: github.event_name == 'workflow_dispatch' && inputs.testpypi`, environment `testpypi` (url
  `https://test.pypi.org/p/dp-python-lib`), `repository-url: https://test.pypi.org/legacy/`, otherwise
  the same steps as `publish-pypi`.  The shared steps may stay duplicated; two copies of three steps is
  clearer than a composite action.
- `build`: raise the `release-dist` upload's `retention-days` from 7 to 30, with a comment tying it to
  the approval window (D2).
- Both jobs: `skip-existing: true` on the upload step, with a comment pointing at D3a.
- Both jobs: move `SHA256SUMS` aside before cleaning `dist/`, then the D5 digest check after upload.
  The version comes from the wheel filename, which covers both tag and dispatch builds.  Retry the JSON
  fetch for about five minutes; fail on a missing file, an extra file, or any digest mismatch, and
  print both sides.
- Every new `uses:` is SHA-pinned with a `# vX.Y.Z` comment (the CLAUDE.md grep must stay empty).

### `pyproject.toml`

- `[project.urls]`: add `Documentation` and `Changelog` (D7).

### Docs

- `README.md`: Installation leads with `pip install dp-python-lib` / `pip install "dp-python-lib[analysis]"`,
  then the editable and `[dev]` installs for development, then the GitHub Release / `README.env` path for
  verified downloads.  Remove the roadmap bullet "Publishing to PyPI".  Make every relative link absolute,
  fragment-only ones included (D7).  Quote the editable installs' extras too (`pip install -e ".[analysis]"`,
  `pip install -e ".[dev]"`): unquoted, they fail in zsh just as the PyPI form does.
- `README.env`: replace the Notes line about PyPI not being enabled.  Add a short section explaining that
  the PyPI files are byte-identical to the release's (checked by the workflow), how to verify a
  `pip download` against the release's `SHA256SUMS` and Sigstore bundles using exactly D5's commands
  (`--no-deps`, `--ignore-missing`), and that PyPI's PEP 740 attestations are a second, independent
  provenance record.
- `doc/cookbook/conventions.md`, `doc/cookbook/query.md`: quote the extra.  `doc/cookbook/README.md`: quote
  `pip install -e ".[dev]"` in its setup section, and check that section for anything that assumes a source
  checkout for ordinary use.
- `doc/release-notes/NEXT.md`: a new section, "Install from PyPI (#76)", plus a Contents entry.  Update
  `## Installing` to lead with `pip install dp-python-lib`, keeping the wheel form.  No version may be
  named (the checker enforces this).  Add a step to "Cutting the release" after the tag push: approve
  the `pypi` deployment once the GitHub Release job is green, then confirm the digest check passed.
- `CLAUDE.md`, Continuous Integration and Releases: replace "A PyPI publish job is wired up but disabled"
  with the enabled flow (order, approval, digest check, TestPyPI input), and add the approval step to
  "Cutting a release", including the warning that the environment must exist before a job names it.

### Verification for the PR

- `grep` for the pinning check, `ruff`, `mypy src/`, the cookbook checker, and
  `.github/scripts/check-release-notes.py` all pass.
- `actionlint` (if available) over `release.yml`.
- Run the workflow by dispatch from the PR branch with `testpypi=true`: the build has no local version,
  the upload succeeds, the digest check passes, and the test.pypi.org page renders the README with
  working links.  Then, in a clean venv, install the rehearsal build and import `MldpClient`:
  ```bash
  pip install --index-url https://test.pypi.org/simple/ \
      --extra-index-url https://pypi.org/simple/ "dp-python-lib==<dev version>"
  ```
  If GitHub rejects the input because `main`'s `release.yml` lacks it, run the rehearsal
  immediately after merge instead.
- A dispatch with `testpypi=false` still stops after build/sign, and `publish-pypi` shows as skipped.

## Out of scope

- How GitHub Releases are built, signed, or verified (unchanged, as the draft says).
- Trimming the sdist (`plan/`, `.github/`, `CLAUDE.md`, and so on).  It would make the PyPI sdist differ
  from the one already shipped and signed on GitHub Releases, so it is its own ticket if wanted.
- Backfilling rel-1.16.0 or earlier (D6).
- A PyPI organization (D1); it can be adopted later without republishing.
- `py.typed`: #61.
- Publishing dp-grpc's Python stubs as their own package: not proposed anywhere, and dp-python-lib
  vendors them.

## Dependencies and sequencing

1. Manual setup steps 1–3, **before** the workflow PR merges.  Step 3 is the hard ordering constraint:
   without it, the first tag push auto-creates an unprotected `pypi` environment.
2. The workflow and docs PR (`Closes #76`), rehearsed on TestPyPI from its branch before merge.
3. The next release cut, following the updated checklist; manual step 4 right after.

The release train order (dp-grpc → dp-service → dp-desktop-app → dp-python-lib → data-platform) is
unchanged, and so is the rule that the release-time `grpc-sync-*` PR merges before dp-python-lib is
tagged.  Nothing here depends on #61, #68, or any upstream repo.

## Open questions

All resolved 2026-10-05:

1. **Who owns the project on PyPI?**  Two personal owners now (D1).  *Remaining action:* name the second
   owner before the first release.
2. **Who approves a publish?**  Craig alone (D2).
3. **Rehearse on TestPyPI?**  Yes, as a permanent opt-in dispatch input (D3).
4. **Which release goes up first?**  The next one; no backfill (D6).
