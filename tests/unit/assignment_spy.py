"""
Test support for asserting that a request builder *assigns* a proto3 scalar field.

A proto3 scalar without presence tracking (e.g. the ``uint32 limit`` on every query request) that holds its
default value is indistinguishable from one that was never assigned: same reflection, same empty wire bytes, and
``request.limit == 0`` is true either way.  So a test asserting ``request.limit == 0`` cannot tell the
``if limit is not None`` guard from the ``if limit:`` truthiness bug of issue #13 -- it passes under both.  The only
observable difference is the assignment itself, which this module watches.
"""

import contextlib
import unittest.mock
from collections.abc import Iterator


class _AssignmentSpy:
    """Wraps a real message, recording assignments to one (possibly nested) attribute and forwarding everything else."""

    def __init__(self, target, path: tuple[str, ...], assigned: list) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_assigned", assigned)

    def __getattr__(self, name):
        value = getattr(object.__getattribute__(self, "_target"), name)
        path = object.__getattribute__(self, "_path")
        if len(path) > 1 and name == path[0]:
            # Descend into a sub-message (e.g. request.executionOptions), still watching the rest of the path.
            return _AssignmentSpy(value, path[1:], object.__getattribute__(self, "_assigned"))
        return value

    def __setattr__(self, name, value) -> None:
        path = object.__getattribute__(self, "_path")
        if len(path) == 1 and name == path[0]:
            object.__getattribute__(self, "_assigned").append(value)
        setattr(object.__getattribute__(self, "_target"), name, value)


@contextlib.contextmanager
def watch_assignments(module, class_name: str, attr_path: str) -> Iterator[list]:
    """
    Patches ``module.<class_name>`` so that its next instantiation returns a spy around a real instance, and yields
    the list collecting every value assigned to ``attr_path`` (dotted to reach a sub-message field, e.g.
    ``"executionOptions.limit"``).  Assert ``[0]`` for "limit=0 was assigned" and ``[]`` for "an omitted limit was
    never assigned".  The builder under test must construct the request through the module attribute
    (``pb2.SomeRequest()``), which every ``_build_*_request`` in this library does.
    """
    assigned: list = []
    spy = _AssignmentSpy(getattr(module, class_name)(), tuple(attr_path.split(".")), assigned)
    with unittest.mock.patch.object(module, class_name, return_value=spy):
        yield assigned
