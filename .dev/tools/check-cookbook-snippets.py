#!/usr/bin/env python3
"""Verify the Python snippets in doc/cookbook/*.md against the installed dp_python_lib.

Three passes, because they have different blind spots:

  1. ast.parse()  -- syntax errors.
  2. mypy         -- wrong attribute names, wrong method names, wrong keyword arguments,
                     wrong arity.  This is the class of error that matters most here: a
                     recipe that writes `result.pv_metadata_list` when the attribute is
                     `result.pv_metadata` is valid Python and sails through pass 1.
  3. imports      -- every name a `# cookbook:partial` snippet uses that the preamble
                     *imports* must also be bound somewhere in the snippet's own recipe
                     (#75).  The preamble supplies those names to pass 2, so without this a
                     recipe that never imports `dfc` type-checks cleanly and still raises
                     NameError for a reader who copies it; #74 shipped that twice.

Background: on the dp-grpc side, extracting and compiling the Java snippets found four real
defects that a careful multi-agent proto-verification pass had missed entirely.  Name-checking
against a schema and compiling are not the same tool.

Usage:
    .venv/bin/python .dev/tools/check-cookbook-snippets.py [--verbose] [FILE ...]

Run it with the same interpreter dp_python_lib is installed into: mypy is invoked as
`sys.executable -m mypy`, so the snippets are checked against that environment's package.

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
import ast
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COOKBOOK_DIR = REPO_ROOT / "doc" / "cookbook"

# mypy is invoked as `sys.executable -m mypy` rather than as a console script on PATH.
# That resolves it from whatever interpreter is running this checker -- a local .venv, CI's
# system Python, uv, pipx -- without assuming a directory layout.  It also guarantees mypy
# sees the same installed dp_python_lib the checker was run against, which is the whole
# point of the exercise.
MYPY_CMD = [sys.executable, "-m", "mypy"]

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
import contextlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional

from dp_python_lib.client import (
    MldpClient,
    IngestionClient,
    RegisterProviderRequestParams,
    IngestDataRequestParams,
    IngestionRequestStatus,
    RequestStatusQuery,
    RequestStatusQuery as RS,
    chunked_request_params,
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
    activation_is_open,
    activation_end_time,
    SaveConfigurationRequestParams,
    SaveConfigurationActivationRequestParams,
    QueryClient,
    QueryParams,
    PvQuery,
    PvQuery as PV,
    ConfigQuery,
    ConfigQuery as CFG,
    SampleStatusFilter,
    SampleStatusClient,
    SampleStatusColumn,
    SampleStatusFrame,
    SampleStatusRow,
    SaveSampleStatusesRequestParams,
    QuerySampleStatusesRequestParams,
    sampling_clock,
    timestamp_list,
    DataSetClient,
    DataSetQuery,
    DataSetQuery as DS,
    SaveDataSetRequestParams,
    data_block,
    AnnotationsClient,
    AnnotationQuery,
    AnnotationQuery as AQ,
    SaveAnnotationRequestParams,
    calculations,
    ExportClient,
    ExportFormat,
    ExportDataRequestParams,
    calculations_spec,
)

from dp_python_lib.client import query_conversions as qc
from dp_python_lib.client import sample_status_conversions as ssc
from dp_python_lib.client import data_frame as dfb
from dp_python_lib.client import data_frame_conversions as dfc
from dp_python_lib.client import bucket_conversions as bc

client: MldpClient = MldpClient()
# client.annotation and client.query are typed `X | None`, which is honest: they are None when MldpClient is given
# only an ingestion channel (connecting.md, "Sub-clients can be None").  A client built from configuration, as every
# recipe's is, always has both, so narrow once here rather than in every recipe.  Do NOT instead disable union-attr:
# on an `X | None` receiver that code also carries the "no such attribute on X" error, so the self-test canary stops
# firing and every misspelled name under client.annotation passes.
assert client.annotation is not None and client.query is not None
begin: datetime = datetime(2024, 1, 1, tzinfo=timezone.utc)
end: datetime = datetime(2024, 1, 2, tzinfo=timezone.utc)

# A already-built QueryParams, for snippets that are about consuming results rather than
# building the query.  Snippets that demonstrate query *construction* build their own.
params: QueryParams = QueryParams(
    begin_time=begin, end_time=end, pv_selector=PV.name_list(["BPMS:GUNB:314:X"]))

# The datasets-and-annotations recipe is one continuous worked example: it saves a dataset, then an
# annotation on it, then calculations, then exports and deletes them.  These are the handles the
# recipe establishes in its own earlier snippets and legitimately carries forward, the way `client`
# and `params` are carried above.  Server-assigned ids are strings.
t0: datetime = datetime(2026, 2, 2, 18, 0, tzinfo=timezone.utc)
t1: datetime = datetime(2026, 2, 2, 19, 0, tzinfo=timezone.utc)
# Declared without an annotation so mypy infers `str` for the standalone snippets that consume these,
# while the recipe's own snippets can still rebind them from an Optional accessor and narrow with an
# assert, as conventions.md teaches.  An explicit `str` would conflict with those real assignments; an
# explicit `str | None` would force every later snippet to re-narrow a handle the recipe already did.
#
# Seeding a handle here cannot prove the recipe actually binds that name -- a snippet binding `saved_id`
# while later ones read `dataset_id` type-checked cleanly and was still broken end to end.  Names carried
# across snippets are verified by reading the recipe as one continuous script, not by this preamble.
# `str | None` is what the accessors return; the recipe narrows with an assert before use, and the
# standalone snippets below do the same, so consumers see a plain `str`.
dataset_id: str | None = "6aa1bb271a768e97db44d426"
annotation_id: str | None = "6aa1bb271a768e97db44d427"
calculations_id: str | None = "6aa1bb271a768e97db44d428"
assert dataset_id is not None and annotation_id is not None and calculations_id is not None

# The ingestion recipe's handles, carried forward the same way: the registration snippet binds provider_id
# (narrowing it with an assert), and every later snippet sends as that provider.
provider_id: str | None = "6aa1bb271a768e97db44d429"
assert provider_id is not None
since: datetime = datetime.now(timezone.utc)
request: IngestDataRequestParams


def acquire(pv: str, count: int) -> list[float]:
    # The recipe's stand-in for an acquisition system.  Its own snippet is syntax-checked only (no-mypy), since
    # this definition and that one would otherwise collide.
    return [0.0] * count


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
            body = "\n".join(line[len(indent) :] if line.startswith(indent) else line for line in body.split("\n"))

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

    if importlib.util.find_spec("mypy") is None:
        return [
            f"mypy is not installed in the environment running this checker ({sys.executable}).\n"
            f"    Install the dev extra:  {sys.executable} -m pip install -e '.[analysis,dev]'"
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
            *MYPY_CMD,
            # The generated gRPC stubs are untyped; without this every `import ..._pb2` is an error.
            "--ignore-missing-imports",
            # Type-check the snippets but NOT the library itself.  The library is checked by
            # `mypy src/` in CI's quality job; re-reporting its errors here would bury real
            # snippet errors.  (The [tool.mypy] config in pyproject.toml applies here too.)
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

        # Inherit the ambient environment rather than replacing it.  A hardcoded minimal env
        # breaks anywhere the interpreter is not under /usr (CI tool caches, uv, conda), and
        # mypy needs the real PATH and venv variables to resolve the same site-packages this
        # checker is running from.  Only MYPYPATH is overridden, to point at src/.
        env = {**os.environ, "MYPYPATH": str(REPO_ROOT / "src")}

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
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


def preamble_imports() -> set[str]:
    """The names PREAMBLE binds by import -- the names pass 3 holds each recipe to importing itself.

    Parsed from the preamble rather than listed, so there is no second list to keep in step.  Its
    fixtures (`client`, `params`, the carried-forward ids, `acquire()`) are assignments and defs, not
    imports, so they are excluded by construction: recipes legitimately carry those between snippets.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(PREAMBLE)):
        if isinstance(node, ast.Import):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
    return names


def bound_names(tree: ast.AST) -> set[str]:
    """Every name a snippet binds, anywhere in it.

    Deliberately looser than Python: scope and order are ignored, so a name imported inside a
    function or in a later block still counts.  Recipes are flat scripts, and the question is
    whether the recipe imports the name at all -- which is what #74 got wrong.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def check_recipe_imports(snippets: list[Snippet]) -> list[str]:
    """Pass 3: does each recipe import the preamble-imported names its partial snippets use?

    Bindings come from every checked snippet in the file (the imports block, inline imports,
    standalone and no-mypy blocks); uses only from partial ones, since a standalone snippet is
    type-checked without the preamble and mypy already reports a missing import there.  Skipped
    snippets contribute neither.  One error per name per file, at its first use.  Expects
    snippets that parse; pass 1 has already reported any that do not.
    """
    imported = preamble_imports()
    by_file: dict[Path, list[Snippet]] = {}
    for snippet in snippets:
        if not snippet.skip:
            by_file.setdefault(snippet.path, []).append(snippet)

    errors: list[str] = []
    for path, file_snippets in by_file.items():
        bound: set[str] = set()
        uses: dict[str, list[int]] = {}
        for snippet in file_snippets:
            tree = ast.parse(snippet.code)
            bound |= bound_names(tree)
            if not snippet.partial:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in imported:
                    uses.setdefault(node.id, []).append(snippet.start_line + node.lineno - 1)

        for name, lines in uses.items():
            if name in bound:
                continue
            lines.sort()
            more = f" [+{len(lines) - 1} more use{'s' if len(lines) > 2 else ''}]" if len(lines) > 1 else ""
            errors.append(
                f"{display_path(path)}:{lines[0]}: '{name}' is used but never imported by this recipe "
                f"(the checker preamble supplies it; add it to the recipe's imports){more}"
            )
    return errors


# Pass 3's self-test recipe: a partial snippet using a preamble import the recipe never imports.
# Checked alone it must be reported; with IMPORTS_CANARY_FIX alongside it, it must not be.  The
# first catches a rule that stops matching; the second, one that ignores bindings and would then
# fail the real cookbook for a confusing reason.
IMPORTS_CANARY = """\
# cookbook:partial
columns = dfc.data_frame_columns(frame)
"""

IMPORTS_CANARY_FIX = """\
from dp_python_lib.client import data_frame_conversions as dfc
"""


# A snippet that MUST fail.  If mypy stops resolving dp_python_lib -- a moved src layout, a
# missing MYPYPATH, an uninstalled package -- it reports success on everything and the checker
# becomes a rubber stamp that looks exactly like clean docs.  This canary makes that loud.
CANARY = """\
# cookbook:partial
result = client.annotation.pv_metadata.get_pv_metadata("ABC:1")
print(result.definitely_not_a_real_attribute_canary)
"""


def self_test(verbose: bool) -> list[str]:
    """Confirm pass 2 still flags a known-bad attribute, and pass 3 a missing import (and only that)."""
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

    errors: list[str] = []
    imported = preamble_imports()
    if "dfc" not in imported:
        errors.append(
            "SELF-TEST FAILED: the preamble's imports were not found (expected 'dfc' among "
            f"{sorted(imported)}), so the recipe import check would check nothing."
        )

    def canary_snippet(code: str, start_line: int, partial: bool) -> Snippet:
        return Snippet(
            path=REPO_ROOT / "<imports-canary>",
            start_line=start_line,
            code=code,
            partial=partial,
            skip=False,
            no_mypy=False,
        )

    if not check_recipe_imports([canary_snippet(IMPORTS_CANARY, 1, True)]):
        errors.append(
            "SELF-TEST FAILED: the recipe import check did not flag 'dfc' used without an import, "
            "so a recipe missing its imports would pass."
        )
    fixed = [canary_snippet(IMPORTS_CANARY_FIX, 1, False), canary_snippet(IMPORTS_CANARY, 5, True)]
    found = check_recipe_imports(fixed)
    if found:
        errors.append(
            "SELF-TEST FAILED: the recipe import check flagged a name the recipe does import, "
            f"so it is ignoring bindings: {found}"
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("files", nargs="*", type=Path, help="markdown files (default: doc/cookbook/*.md)")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--no-self-test",
        action="store_true",
        help="skip the canaries that verify name and import checking still work",
    )
    args = parser.parse_args()

    if args.files:
        paths = [p if p.is_absolute() else REPO_ROOT / p for p in args.files]
    elif COOKBOOK_DIR.is_dir():
        paths = sorted(COOKBOOK_DIR.glob("*.md"))
    else:
        print(f"No cookbook directory at {COOKBOOK_DIR.relative_to(REPO_ROOT)} -- nothing to check.")
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
            flags = ",".join(f for f, on in (("partial", s.partial), ("skip", s.skip), ("no-mypy", s.no_mypy)) if on)
            print(f"  {s.location}{f'  [{flags}]' if flags else ''}", file=sys.stderr)

    errors: list[str] = []

    # Verify the checker itself works before trusting a clean result from it.
    if not args.no_self_test:
        if args.verbose:
            print("  running self-test (canaries)...", file=sys.stderr)
        canary_errors = self_test(args.verbose)
        if canary_errors:
            print("\nFAIL: checker self-test failed\n")
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

    parsed = [s for s in checked if id(s) not in syntax_failed]

    # Pass 3 needs only the AST, so run it before the slow mypy pass; its errors are sorted in below.
    errors.extend(check_recipe_imports(parsed))

    # Pass 2, excluding anything that already failed to parse.
    errors.extend(check_types(parsed, args.verbose))

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
