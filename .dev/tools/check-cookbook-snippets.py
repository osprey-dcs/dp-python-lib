#!/usr/bin/env python3
"""Verify the Python snippets in doc/cookbook/*.md against the installed dp_python_lib.

Two passes, because they have different blind spots:

  1. ast.parse()  -- syntax errors.
  2. mypy         -- wrong attribute names, wrong method names, wrong keyword arguments,
                     wrong arity.  This is the class of error that matters most here: a
                     recipe that writes `result.pv_metadata_list` when the attribute is
                     `result.pv_metadata` is valid Python and sails through pass 1.

Background: on the dp-grpc side, extracting and compiling the Java snippets found four real
defects that a careful multi-agent proto-verification pass had missed entirely.  Name-checking
against a schema and compiling are not the same tool.

Usage:
    .venv/bin/python .dev/tools/check-cookbook-snippets.py [--verbose] [FILE ...]

Exits non-zero if any snippet fails.

Snippet directives (in a comment on the block's first line):

    # cookbook:partial   -- fragment; gets the shared preamble prepended so that names like
                            `client` and `result` resolve.  Without this, a block is checked
                            standalone and must be self-contained.
    # cookbook:skip      -- do not check this block at all (illustrative pseudo-code, output
                            samples, deliberately-wrong "don't do this" examples).
    # cookbook:no-mypy   -- run pass 1 only.  Escape hatch for a block that is syntactically
                            fine but that mypy cannot resolve for an uninteresting reason.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COOKBOOK_DIR = REPO_ROOT / "doc" / "cookbook"
VENV_MYPY = REPO_ROOT / ".venv" / "bin" / "mypy"

# Fenced ```python blocks.  Captures the info string tail so ```python title=... still matches.
FENCE_RE = re.compile(
    r"^(?P<indent>[ \t]*)```[ \t]*python[^\n]*\n(?P<body>.*?)^(?P=indent)```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

DIRECTIVE_RE = re.compile(r"#\s*cookbook:(partial|skip|no-mypy)\b")

# Prepended to `# cookbook:partial` blocks so that bare names resolve.  Mirrors the preamble
# stub the dp-grpc Java harness needed for the same reason.
#
# Keep this minimal and honest: every name here should be one a recipe legitimately uses
# without re-establishing it.  Adding a name to paper over a broken snippet defeats the point.
PREAMBLE = """\
# --- checker preamble (not part of the recipe) ---
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from dp_python_lib.client import (
    MldpClient,
    IngestionClient,
    RegisterProviderRequestParams,
    AnnotationClient,
    PvMetadataClient,
    PvMetadataQuery,
    PvMetadataQuery as Q,
    SavePvMetadataRequestParams,
    MachineConfigClient,
    ConfigurationQuery,
    ConfigurationQuery as C,
    ConfigurationActivationQuery,
    ConfigurationActivationQuery as CA,
    to_timestamp,
    SaveConfigurationRequestParams,
    SaveConfigurationActivationRequestParams,
    QueryClient,
    QueryParams,
    PvQuery,
    PvQuery as PV,
    ConfigQuery,
    ConfigQuery as CFG,
)

from dp_python_lib.client import query_conversions as qc

client: MldpClient = MldpClient()
begin: datetime = datetime(2024, 1, 1, tzinfo=timezone.utc)
end: datetime = datetime(2024, 1, 2, tzinfo=timezone.utc)

# A already-built QueryParams, for snippets that are about consuming results rather than
# building the query.  Snippets that demonstrate query *construction* build their own.
params: QueryParams = QueryParams(
    begin_time=begin, end_time=end, pv_selector=PV.name_list(["BPMS:GUNB:314:X"]))
# --- end preamble ---
"""

PREAMBLE_LINES = PREAMBLE.count("\n")


def display_path(path: Path) -> str:
    """Repo-relative when possible, absolute otherwise (a file passed explicitly from elsewhere)."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


@dataclass
class Snippet:
    path: Path
    # 1-based line number of the snippet's first code line within the markdown file.
    start_line: int
    code: str
    partial: bool
    skip: bool
    no_mypy: bool

    @property
    def location(self) -> str:
        return f"{display_path(self.path)}:{self.start_line}"


def extract(path: Path) -> list[Snippet]:
    text = path.read_text(encoding="utf-8")
    snippets: list[Snippet] = []

    for match in FENCE_RE.finditer(text):
        body = match.group("body")
        indent = match.group("indent")

        # Strip the fence's indentation from each line so indented blocks (inside list items)
        # parse as top-level code.
        if indent:
            body = "\n".join(
                line[len(indent) :] if line.startswith(indent) else line
                for line in body.split("\n")
            )

        # The opening fence occupies one line, so code starts on the next.
        start_line = text[: match.start()].count("\n") + 2

        directives = set(DIRECTIVE_RE.findall(body))
        snippets.append(
            Snippet(
                path=path,
                start_line=start_line,
                code=body,
                partial="partial" in directives,
                skip="skip" in directives,
                no_mypy="no-mypy" in directives,
            )
        )

    return snippets


def check_syntax(snippet: Snippet) -> list[str]:
    """Pass 1: does it parse at all?"""
    import ast

    try:
        ast.parse(snippet.code)
    except SyntaxError as exc:
        # exc.lineno is relative to the snippet; map it back to the markdown file.
        line = snippet.start_line + (exc.lineno or 1) - 1
        return [f"{display_path(snippet.path)}:{line}: syntax error: {exc.msg}"]
    return []


def check_types(snippets: list[Snippet], verbose: bool) -> list[str]:
    """Pass 2: run mypy over every snippet at once, then map errors back to source lines.

    One mypy invocation for all snippets rather than one per snippet -- mypy's startup and
    dependency analysis dominate its runtime, so batching is dramatically faster.
    """
    checkable = [s for s in snippets if not s.skip and not s.no_mypy]
    if not checkable:
        return []

    if not VENV_MYPY.exists():
        return [
            f"mypy not found at {VENV_MYPY}. Install the dev extra:\n"
            f"    .venv/bin/python -m pip install mypy"
        ]

    errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="cookbook-snippets-") as tmpdir:
        tmp = Path(tmpdir)
        # Map temp filename -> (snippet, line offset applied by the preamble).
        written: dict[str, tuple[Snippet, int]] = {}

        for idx, snippet in enumerate(checkable):
            if snippet.partial:
                source = PREAMBLE + snippet.code
                offset = PREAMBLE_LINES
            else:
                source = snippet.code
                offset = 0

            name = f"snippet_{idx:03d}.py"
            (tmp / name).write_text(source, encoding="utf-8")
            written[name] = (snippet, offset)

        cmd = [
            str(VENV_MYPY),
            # The generated gRPC stubs are untyped; without this every `import ..._pb2` is an error.
            "--ignore-missing-imports",
            # Type-check the snippets but NOT the library itself.  dp_python_lib currently has
            # ~145 of its own mypy errors (mostly untyped protobuf stubs); those are not this
            # tool's business and would bury real snippet errors.
            "--follow-imports=silent",
            # Snippets are illustrative; unreachable/redundant warnings are noise here.
            "--no-warn-unused-ignores",
            "--no-error-summary",
            "--no-color-output",
            "--show-absolute-path",
            *[str(tmp / name) for name in written],
        ]

        if verbose:
            print(f"  running: {' '.join(cmd[:6])} ... ({len(written)} files)", file=sys.stderr)

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={"MYPYPATH": str(REPO_ROOT / "src"), "PATH": "/usr/bin:/bin"},
        )

        line_re = re.compile(r"^(?P<file>.+?):(?P<line>\d+):(?:\d+:)?\s*(?P<rest>error:.*)$")

        for raw in proc.stdout.splitlines():
            match = line_re.match(raw.strip())
            if not match:
                # mypy notes and anything unparseable -- surface it rather than swallow it.
                if raw.strip() and "error:" in raw:
                    errors.append(raw.strip())
                continue

            name = Path(match.group("file")).name
            if name not in written:
                errors.append(raw.strip())
                continue

            snippet, offset = written[name]
            snippet_line = int(match.group("line")) - offset

            if snippet_line < 1:
                # An error inside the preamble itself means the preamble is broken, not the recipe.
                errors.append(f"{snippet.location}: [checker preamble] {match.group('rest')}")
                continue

            md_line = snippet.start_line + snippet_line - 1
            errors.append(f"{display_path(snippet.path)}:{md_line}: {match.group('rest')}")

    return errors


# A snippet that MUST fail.  If mypy stops resolving dp_python_lib -- a moved src layout, a
# missing MYPYPATH, an uninstalled package -- it reports success on everything and the checker
# becomes a rubber stamp that looks exactly like clean docs.  This canary makes that loud.
CANARY = """\
# cookbook:partial
result = client.annotation.pv_metadata.get_pv_metadata("ABC:1")
print(result.definitely_not_a_real_attribute_canary)
"""


def self_test(verbose: bool) -> list[str]:
    """Confirm the mypy pass can still detect a known-bad attribute."""
    canary = Snippet(
        path=REPO_ROOT / "<canary>",
        start_line=1,
        code=CANARY,
        partial=True,
        skip=False,
        no_mypy=False,
    )
    found = check_types([canary], verbose=verbose)
    if not found:
        return [
            "SELF-TEST FAILED: mypy did not flag a known-bad attribute, so name checking is "
            "not working. Every snippet would pass regardless of correctness.\n"
            "    Likely causes: package not importable from src/, or mypy resolving "
            "dp_python_lib as untyped.\n"
            "    Verify with:  MYPYPATH=$PWD/src .venv/bin/mypy --ignore-missing-imports "
            "--follow-imports=silent <a file using the client>"
        ]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "files", nargs="*", type=Path, help="markdown files (default: doc/cookbook/*.md)"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--no-self-test",
        action="store_true",
        help="skip the canary that verifies name checking still works",
    )
    args = parser.parse_args()

    if args.files:
        paths = [p if p.is_absolute() else REPO_ROOT / p for p in args.files]
    elif COOKBOOK_DIR.is_dir():
        paths = sorted(COOKBOOK_DIR.glob("*.md"))
    else:
        print(
            f"No cookbook directory at {COOKBOOK_DIR.relative_to(REPO_ROOT)} -- nothing to check."
        )
        return 0

    missing = [p for p in paths if not p.is_file()]
    if missing:
        for p in missing:
            print(f"error: no such file: {p}", file=sys.stderr)
        return 2

    all_snippets: list[Snippet] = []
    for path in paths:
        all_snippets.extend(extract(path))

    if not all_snippets:
        print("No python snippets found -- nothing to check.")
        return 0

    checked = [s for s in all_snippets if not s.skip]
    skipped = len(all_snippets) - len(checked)

    if args.verbose:
        for s in all_snippets:
            flags = ",".join(
                f
                for f, on in (("partial", s.partial), ("skip", s.skip), ("no-mypy", s.no_mypy))
                if on
            )
            print(f"  {s.location}{f'  [{flags}]' if flags else ''}", file=sys.stderr)

    errors: list[str] = []

    # Verify the checker itself works before trusting a clean result from it.
    if not args.no_self_test:
        if args.verbose:
            print("  running self-test (canary)...", file=sys.stderr)
        canary_errors = self_test(args.verbose)
        if canary_errors:
            print(f"\nFAIL: checker self-test failed\n")
            for err in canary_errors:
                print(f"  {err}")
            print()
            return 2

    # Pass 1 first: a syntax error would make the mypy pass report noise for that file.
    syntax_failed = set()
    for snippet in checked:
        found = check_syntax(snippet)
        if found:
            errors.extend(found)
            syntax_failed.add(id(snippet))

    # Pass 2, excluding anything that already failed to parse.
    errors.extend(check_types([s for s in checked if id(s) not in syntax_failed], args.verbose))

    files_desc = f"{len(paths)} file{'s' if len(paths) != 1 else ''}"
    counts = f"{len(checked)} snippet{'s' if len(checked) != 1 else ''} in {files_desc}"
    if skipped:
        counts += f" ({skipped} skipped)"

    if errors:
        # Sort by file then line so output is stable and reads top-to-bottom through each recipe.
        # mypy emits per-tempfile, which does not match markdown order.
        def sort_key(err: str) -> tuple[str, int]:
            match = re.match(r"^(.+?):(\d+):", err)
            return (match.group(1), int(match.group(2))) if match else (err, 0)

        print(f"\nFAIL: {len(errors)} problem(s) in {counts}\n")
        for err in sorted(errors, key=sort_key):
            print(f"  {err}")
        print()
        return 1

    print(f"OK: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
