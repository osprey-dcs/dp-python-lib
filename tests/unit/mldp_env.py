"""
Test support for isolating configuration tests from ambient ``MLDP_*`` environment variables.

A developer shell may export ``MLDP_*`` (to point integration tests at a remote ecosystem, say).  Since issue #19
those variables override the YAML file, so any test asserting a value from a file or a default must run with them
removed, or it passes in CI and fails on that developer's machine.
"""

import contextlib
import os
import unittest
from collections.abc import Iterator
from unittest.mock import patch


def _without_mldp() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("MLDP_")}


@contextlib.contextmanager
def mldp_env(**overrides: str) -> Iterator[None]:
    """Run with every ambient ``MLDP_*`` variable removed, plus ``overrides``."""
    env = _without_mldp()
    env.update(overrides)
    with patch.dict(os.environ, env, clear=True):
        yield


def isolate_mldp_env(test: unittest.TestCase) -> None:
    """From a ``setUp``: remove every ambient ``MLDP_*`` variable for the duration of the test.

    A test's own ``@patch.dict(os.environ, {...})`` still applies on top, since it is entered after ``setUp``.
    """
    patcher = patch.dict(os.environ, _without_mldp(), clear=True)
    patcher.start()
    test.addCleanup(patcher.stop)
