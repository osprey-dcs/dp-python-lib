"""Automatically mark everything in tests/integration as `integration`.

These tests need a live MLDP ecosystem (docker compose up -d) listening on
localhost:50051-50053.  They self-skip when it is absent, but CI deselects them
outright with `-m "not integration"` so a missing backend is never mistaken for
a passing run.

Marking here rather than decorating each class keeps the marker from drifting as
tests are added -- anything dropped into this directory is covered automatically.
"""


def pytest_collection_modifyitems(config, items):
    import pytest

    integration = pytest.mark.integration
    for item in items:
        # rootdir-relative check, so it holds however pytest was invoked.
        if "tests/integration/" in item.nodeid.replace("\\", "/"):
            item.add_marker(integration)
