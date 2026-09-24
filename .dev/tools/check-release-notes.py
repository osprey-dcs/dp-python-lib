#!/usr/bin/env python3
"""Verify that every doc/release-notes/rel-X.Y.Z.md carries a correct verification section.

The notes file is published verbatim as the GitHub release body (release.yml; plan/tickets/56/plan.md D1), so
the artifact verification instructions exist only if the file contains them.  This checks, per file:

  - the file name is rel-X.Y.Z.md (release.yml derives the name from the tag, so nothing else is ever published);
  - there is a `## Verifying these artifacts` heading;
  - there is at least one `sigstore verify identity` command, and every `--cert-identity` in the file is exactly
    this repository's release.yml at *this file's* tag, with the GitHub Actions OIDC issuer.

The identity check is the one that earns its keep.  The section is hand-written per release, usually by copying
the previous one, and a stale tag in the identity makes `sigstore verify` reject every genuine artifact of the
release ("Certificate's SANs do not match").  Readers would reasonably conclude the release is forged.

Runs in CI's quality job over every notes file, so a mistake fails the notes PR rather than the tag push; and in
release.yml on the tagged file, as a backstop.

Usage:
    python .dev/tools/check-release-notes.py [FILE ...]

With no arguments, checks every doc/release-notes/rel-*.md.  Stdlib only.  Exits 0 if all pass, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_DIR = REPO_ROOT / "doc" / "release-notes"

REPOSITORY = "osprey-dcs/dp-python-lib"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"

STEM_RE = re.compile(r"^rel-\d+\.\d+\.\d+$")
HEADING_RE = re.compile(r"^## Verifying these artifacts\s*$", re.MULTILINE)
VERIFY_RE = re.compile(r"\bsigstore\s+verify\s+identity\b")
IDENTITY_RE = re.compile(r"--cert-identity[ =]\"?([^\"\s]+)\"?")
ISSUER_RE = re.compile(r"--cert-oidc-issuer[ =]\"?([^\"\s]+)\"?")


def expected_identity(tag: str) -> str:
    return f"https://github.com/{REPOSITORY}/.github/workflows/release.yml@refs/tags/{tag}"


def check_file(path: Path) -> list[str]:
    """Returns one message per problem found in `path`; empty when it passes."""
    tag = path.stem
    if path.suffix != ".md" or not STEM_RE.match(tag):
        return [f"{path}: name must be rel-X.Y.Z.md (release.yml looks the notes up by tag)"]

    text = path.read_text(encoding="utf-8")
    problems: list[str] = []

    if not HEADING_RE.search(text):
        problems.append(f"{path}: missing a '## Verifying these artifacts' section")

    if not VERIFY_RE.search(text):
        problems.append(f"{path}: missing a 'sigstore verify identity' command")

    identities = IDENTITY_RE.findall(text)
    if not identities:
        problems.append(f"{path}: missing --cert-identity")
    want = expected_identity(tag)
    for identity in identities:
        if identity != want:
            problems.append(f"{path}: --cert-identity is\n      {identity}\n    expected\n      {want}")

    issuers = ISSUER_RE.findall(text)
    if not issuers:
        problems.append(f"{path}: missing --cert-oidc-issuer")
    for issuer in issuers:
        if issuer != OIDC_ISSUER:
            problems.append(f"{path}: --cert-oidc-issuer is {issuer}, expected {OIDC_ISSUER}")

    return problems


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv] if argv else sorted(NOTES_DIR.glob("rel-*.md"))
    if not paths:
        print(f"FAIL: no release notes found under {NOTES_DIR}")
        return 1

    problems: list[str] = []
    for path in paths:
        if not path.is_file():
            problems.append(f"{path}: not found")
            continue
        problems.extend(check_file(path))

    if problems:
        print(f"FAIL: {len(problems)} problem(s) in {len(paths)} release notes file(s)\n")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print(f"OK: {len(paths)} release notes file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
