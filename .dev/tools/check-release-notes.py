#!/usr/bin/env python3
"""Verify that every doc/release-notes/rel-X.Y.Z.md carries a correct verification section and changelog link.

The notes file is published verbatim as the GitHub release body (release.yml; plan/tickets/56/plan.md D1), so
the artifact verification instructions and the changelog link exist only if the file contains them.  This checks,
per file:

  - the file name is rel-X.Y.Z.md (release.yml derives the name from the tag, so nothing else is ever published);
  - there is a `## Verifying these artifacts` heading;
  - there is at least one `sigstore verify identity` command, each naming the wheel, the sdist, and SHA256SUMS,
    and every `--cert-identity` in the file is exactly this repository's release.yml at *this file's* tag, with
    the GitHub Actions OIDC issuer;
  - there is a `**Full Changelog**:` compare link ending at this file's tag and starting at an earlier one.

The identity check is the one that earns its keep.  The section is hand-written per release, usually by copying
the previous one, and a stale tag in the identity makes `sigstore verify` reject every genuine artifact of the
release ("Certificate's SANs do not match").  Readers would reasonably conclude the release is forged.  The
changelog link is copied the same way and goes stale the same way, though less dangerously.  Only the `<this>`
end can be checked: which release came before is not knowable from one file, so `<prev>` is checked only for
being earlier.

Runs in CI's quality job over every notes file, so a mistake fails the notes PR rather than the tag push; and in
release.yml on the tagged file, as a backstop.  Each run starts with a self-test that feeds the rules known-bad
notes and fails if any is accepted, so a rule that has quietly stopped matching cannot pass as clean notes.

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

STEM_RE = re.compile(r"^rel-(\d+)\.(\d+)\.(\d+)$")
HEADING_RE = re.compile(r"^## Verifying these artifacts\s*$", re.MULTILINE)
# The whole command, backslash continuations included, up to the first line that does not continue.
VERIFY_RE = re.compile(r"\bsigstore\s+verify\s+identity\b(?:[^\n]*\\\n)*[^\n]*")
IDENTITY_RE = re.compile(r"--cert-identity[ =]\"?([^\"\s]+)\"?")
ISSUER_RE = re.compile(r"--cert-oidc-issuer[ =]\"?([^\"\s]+)\"?")
CHANGELOG_RE = re.compile(r"^\*\*Full Changelog\*\*:\s*(\S+)\s*$", re.MULTILINE)
COMPARE_RE = re.compile(
    rf"^https://github\.com/{re.escape(REPOSITORY)}/compare/(rel-\d+\.\d+\.\d+)\.\.\.(rel-\d+\.\d+\.\d+)$"
)

# What each `sigstore verify identity` must name, as (description, predicate over its file operands).  Every
# release signs all three, and verifying only the wheel was exactly the shape rel-1.16.0's page first shipped with.
REQUIRED_OPERANDS = [
    ("the wheel", lambda arg: arg.endswith(".whl")),
    ("the sdist", lambda arg: arg.endswith(".tar.gz")),
    ("SHA256SUMS", lambda arg: arg == "SHA256SUMS"),
]


def expected_identity(tag: str) -> str:
    return f"https://github.com/{REPOSITORY}/.github/workflows/release.yml@refs/tags/{tag}"


def version_of(tag: str) -> tuple[int, ...]:
    match = STEM_RE.match(tag)
    assert match is not None
    return tuple(int(part) for part in match.groups())


def verify_operands(command: str) -> list[str]:
    """The file operands of one `sigstore verify identity` command: every token that is not an option or its value."""
    tokens = command.replace("\\\n", " ").split()[3:]
    operands: list[str] = []
    skip_value = False
    for token in tokens:
        if skip_value:
            skip_value = False
        elif token.startswith("--"):
            skip_value = "=" not in token
        else:
            operands.append(token)
    return operands


def check_text(name: str, tag: str, text: str) -> list[str]:
    """Returns one message per problem found in the notes `text` for `tag`; empty when it passes."""
    problems: list[str] = []

    if not HEADING_RE.search(text):
        problems.append(f"{name}: missing a '## Verifying these artifacts' section")

    commands = VERIFY_RE.findall(text)
    if not commands:
        problems.append(f"{name}: missing a 'sigstore verify identity' command")
    for command in commands:
        operands = verify_operands(command)
        for description, matches in REQUIRED_OPERANDS:
            if not any(matches(arg) for arg in operands):
                problems.append(f"{name}: 'sigstore verify identity' does not verify {description}")

    identities = IDENTITY_RE.findall(text)
    if not identities:
        problems.append(f"{name}: missing --cert-identity")
    want = expected_identity(tag)
    for identity in identities:
        if identity != want:
            problems.append(f"{name}: --cert-identity is\n      {identity}\n    expected\n      {want}")

    issuers = ISSUER_RE.findall(text)
    if not issuers:
        problems.append(f"{name}: missing --cert-oidc-issuer")
    for issuer in issuers:
        if issuer != OIDC_ISSUER:
            problems.append(f"{name}: --cert-oidc-issuer is {issuer}, expected {OIDC_ISSUER}")

    changelogs = CHANGELOG_RE.findall(text)
    if not changelogs:
        problems.append(f"{name}: missing a '**Full Changelog**: .../compare/rel-<prev>...{tag}' line")
    for url in changelogs:
        compare = COMPARE_RE.match(url)
        if compare is None:
            problems.append(
                f"{name}: Full Changelog link is\n      {url}\n    expected"
                f"\n      https://github.com/{REPOSITORY}/compare/rel-<prev>...{tag}"
            )
            continue
        prev, this = compare.groups()
        if this != tag:
            problems.append(f"{name}: Full Changelog link ends at {this}, expected {tag}")
        elif version_of(prev) >= version_of(tag):
            problems.append(f"{name}: Full Changelog link starts at {prev}, which is not earlier than {tag}")

    return problems


def check_file(path: Path) -> list[str]:
    """Returns one message per problem found in `path`; empty when it passes."""
    tag = path.stem
    if path.suffix != ".md" or not STEM_RE.match(tag):
        return [f"{path}: name must be rel-X.Y.Z.md (release.yml looks the notes up by tag)"]
    return check_text(str(path), tag, path.read_text(encoding="utf-8"))


def _sample(
    identity_tag: str = "rel-2.1.0",
    files: str = "dp_python_lib-*.whl dp_python_lib-*.tar.gz SHA256SUMS",
    changelog: str | None = "rel-2.0.0...rel-2.1.0",
) -> str:
    notes = f"""# dp-python-lib rel-2.1.0

## Verifying these artifacts

```bash
sigstore verify identity \\
  --cert-identity "{expected_identity(identity_tag)}" \\
  --cert-oidc-issuer "{OIDC_ISSUER}" \\
  {files}
```
"""
    if changelog is not None:
        notes += f"\n**Full Changelog**: https://github.com/{REPOSITORY}/compare/{changelog}\n"
    return notes


def self_test() -> list[str]:
    """Confirms a correct sample passes and each known-bad variant is rejected; returns a message per failure."""
    failures: list[str] = []
    good = check_text("<good>", "rel-2.1.0", _sample())
    if good:
        failures.append("a correct sample was rejected:\n      " + "\n      ".join(good))

    bad_cases = {
        "a stale --cert-identity tag": _sample(identity_tag="rel-2.0.0"),
        "a verify of the wheel only": _sample(files="dp_python_lib-*.whl"),
        "a verify missing SHA256SUMS": _sample(files="dp_python_lib-*.whl dp_python_lib-*.tar.gz"),
        "a verify missing the sdist": _sample(files="dp_python_lib-*.whl SHA256SUMS"),
        "no Full Changelog line": _sample(changelog=None),
        "a Full Changelog link ending at a stale tag": _sample(changelog="rel-1.9.0...rel-2.0.0"),
        "a Full Changelog link starting at a later tag": _sample(changelog="rel-2.2.0...rel-2.1.0"),
        "a Full Changelog link to another repository": _sample().replace(
            f"{REPOSITORY}/compare", "someone-else/dp-python-lib/compare"
        ),
        "no verification heading": _sample().replace("## Verifying these artifacts", "## Verification"),
    }
    for description, notes in bad_cases.items():
        if not check_text(f"<{description}>", "rel-2.1.0", notes):
            failures.append(f"{description} was accepted")
    return failures


def main(argv: list[str]) -> int:
    canary_failures = self_test()
    if canary_failures:
        print("FAIL: checker self-test failed; its rules no longer catch what they are meant to\n")
        for failure in canary_failures:
            print(f"  {failure}")
        return 1

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
