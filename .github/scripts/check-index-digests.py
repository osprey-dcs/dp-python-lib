#!/usr/bin/env python3
"""Check that a package index serves exactly the files listed in a SHA256SUMS file, byte for byte.

release.yml runs this after each upload (PyPI on a tag, TestPyPI on an opted-in rehearsal), with the
SHA256SUMS the build job generated and signed.  It is what makes "the PyPI files are the signed GitHub
Release files" a checked statement rather than an assumption (#76; plan/tickets/76/plan.md, D5).

The version is read from the wheel's filename in SHA256SUMS, so tag builds and rehearsal (.devN) builds
are handled alike.  The index's JSON API (`<index>/pypi/<project>/<version>/json`) is polled until every
expected file is listed or the timeout expires, since a fresh upload can take a few minutes to appear
through the CDN.  Then it fails on any expected file missing from the index, any file on the index for
that version that SHA256SUMS does not list, and any digest mismatch, printing both sides.

Files skipped by the upload (`skip-existing: true`, plan D3a) are compared like any other: a re-run
that skips a file already uploaded passes, and a different build at a version already on the index
fails here, loudly, rather than at upload time.

A transient failure while polling -- a 429 or 5xx, a connection error or timeout, or a body that is not
JSON (a CDN error page) -- is logged and retried until the timeout, like a 404: the upload before this
check cannot be undone, so a blip must not turn a good release red.  Any other HTTP error fails at once.

Each run starts with a self-test of the comparison, so a rule that stops matching fails instead of
passing everything.  `--self-test` runs only that, which is how CI's quality job exercises this script
before a tag does.
"""

import argparse
import email.message
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def read_sums(path: Path) -> dict[str, str]:
    """Parse `sha256sum` output into {filename: hex digest}."""
    sums: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        sums[name.lstrip("*")] = digest.lower()
    if not sums:
        raise ValueError(f"{path} lists no files")
    return sums


def wheel_version(sums: dict[str, str]) -> str:
    """The version from the one wheel's filename (`<name>-<version>-<tags>.whl`)."""
    wheels = [name for name in sums if name.endswith(".whl")]
    if len(wheels) != 1:
        raise ValueError(f"expected exactly one wheel in SHA256SUMS, found {wheels}")
    return wheels[0].split("-")[1]


def compare(expected: dict[str, str], served: dict[str, str]) -> list[str]:
    """Every difference between the signed files and the index's files, as error messages."""
    errors = []
    for name in sorted(expected.keys() - served.keys()):
        errors.append(f"missing from the index: {name} (SHA256SUMS: {expected[name]})")
    for name in sorted(served.keys() - expected.keys()):
        errors.append(f"on the index but not in SHA256SUMS: {name} (index: {served[name]})")
    for name in sorted(expected.keys() & served.keys()):
        if expected[name] != served[name]:
            errors.append(f"digest mismatch: {name} (SHA256SUMS: {expected[name]}, index: {served[name]})")
    return errors


class TransientError(Exception):
    """A polling failure worth retrying: the index may answer properly on the next poll."""


def is_transient(error: Exception) -> bool:
    """Whether a failure fetching from the index is worth retrying rather than failing on."""
    if isinstance(error, urllib.error.HTTPError):  # before URLError and OSError, which it subclasses
        return error.code == 429 or error.code >= 500
    # URLError (DNS, refused connection), socket timeouts and resets are OSErrors; a non-JSON body
    # (a CDN error page served with 200) is a JSONDecodeError, a ValueError.
    return isinstance(error, (OSError, ValueError))


def fetch_served(url: str) -> dict[str, str] | None:
    """{filename: sha256} for the release at `url`, or None if the index does not have it yet.

    Raises TransientError for a failure worth retrying (see is_transient()); any other error propagates.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if is_transient(e):
            raise TransientError(f"HTTP {e.code} {e.reason}") from e
        raise
    except Exception as e:
        if is_transient(e):
            raise TransientError(f"{type(e).__name__}: {e}") from e
        raise
    return {f["filename"]: f["digests"]["sha256"].lower() for f in data["urls"]}


def self_test() -> list[str]:
    a, b = "a" * 64, "b" * 64
    failures = []
    cases = [
        ("identical", {"x.whl": a, "x.tar.gz": b}, {"x.whl": a, "x.tar.gz": b}, 0),
        ("missing", {"x.whl": a, "x.tar.gz": b}, {"x.whl": a}, 1),
        ("extra", {"x.whl": a}, {"x.whl": a, "x.tar.gz": b}, 1),
        ("mismatch", {"x.whl": a}, {"x.whl": b}, 1),
        ("all three", {"x.whl": a, "y.whl": a}, {"x.whl": b, "z.whl": a}, 3),
    ]
    for label, expected, served, count in cases:
        got = len(compare(expected, served))
        if got != count:
            failures.append(f"compare() self-test '{label}': expected {count} error(s), got {got}")
    sums = {"dp_python_lib-1.17.0.dev3-py3-none-any.whl": a, "dp_python_lib-1.17.0.dev3.tar.gz": b}
    if wheel_version(sums) != "1.17.0.dev3":
        failures.append("wheel_version() self-test: did not read 1.17.0.dev3")

    def http_error(code: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("https://index.invalid", code, "test", email.message.Message(), None)

    transient = [
        ("HTTP 429", http_error(429), True),
        ("HTTP 503", http_error(503), True),
        ("HTTP 403", http_error(403), False),
        ("URLError", urllib.error.URLError("name resolution failed"), True),
        ("timeout", TimeoutError("read timed out"), True),
        ("non-JSON body", json.JSONDecodeError("Expecting value", "<html>", 0), True),
        ("KeyError", KeyError("urls"), False),
    ]
    for label, error, retry in transient:
        if is_transient(error) != retry:
            failures.append(f"is_transient() self-test '{label}': expected {retry}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sums", type=Path, nargs="?", help="the SHA256SUMS file the build job signed")
    parser.add_argument("--index-url", help="e.g. https://pypi.org or https://test.pypi.org")
    parser.add_argument("--project", help="the project name on the index")
    parser.add_argument("--timeout", type=int, default=300, help="seconds to wait for the files to appear")
    parser.add_argument("--interval", type=int, default=15, help="seconds between polls")
    parser.add_argument("--self-test", action="store_true", help="run only the self-test, touching no index")
    args = parser.parse_args()
    if not args.self_test and (args.sums is None or args.index_url is None or args.project is None):
        parser.error("sums, --index-url and --project are required unless --self-test is given")

    failures = self_test()
    if failures:
        for failure in failures:
            print(f"::error::self-test: {failure}")
        return 1
    if args.self_test:
        print("Self-test passed.")
        return 0

    expected = read_sums(args.sums)
    version = wheel_version(expected)
    url = f"{args.index_url.rstrip('/')}/pypi/{args.project}/{version}/json"
    print(f"Checking {url} against {args.sums}:")
    for name, digest in sorted(expected.items()):
        print(f"  {digest}  {name}")

    deadline = time.monotonic() + args.timeout
    served: dict[str, str] | None = None
    last_failure: str | None = None
    while True:
        try:
            served = fetch_served(url)
            last_failure = None
        except TransientError as e:
            last_failure = str(e)
        if served is not None and last_failure is None and expected.keys() <= served.keys():
            break
        if time.monotonic() >= deadline:
            break
        if last_failure is not None:
            state = f"request failed ({last_failure})"
        else:
            state = "not found" if served is None else f"lists {sorted(served)}"
        print(f"Index {state}; retrying in {args.interval}s")
        time.sleep(args.interval)

    if served is None:
        reason = f"still fails ({last_failure})" if last_failure is not None else "still returns 404"
        print(f"::error::{url} {reason} after {args.timeout}s.")
        return 1
    if last_failure is not None:
        print(f"::warning::the last poll failed ({last_failure}); comparing the listing from the poll before it.")
    errors = compare(expected, served)
    for error in errors:
        print(f"::error::{error}")
    if errors:
        return 1
    print(f"The index serves exactly the {len(expected)} files in {args.sums}, byte for byte.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
