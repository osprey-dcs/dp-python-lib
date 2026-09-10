"""
Time conversions shared across the client library.

Every API that takes an instant accepts the same three spellings -- a timezone-aware datetime, epoch seconds, or an
already-built common.Timestamp -- and every conversions module that reads one back wants integer nanoseconds.  Those
two directions are `to_timestamp()` and `to_epoch_nanos()`, and they live here rather than in a feature client.

They were originally defined in machine_config_client, the first module to need them, and four other modules grew
imports of `to_timestamp()` from there -- which read as though datasets, queries, and DataFrames depended on the
machine configuration API.  Meanwhile the reverse conversion had been written out privately three separate times.
This module is a leaf: it imports nothing from the client package, so any client module can use it freely.

Design decisions:
  - Naive datetimes are rejected rather than assumed to be UTC or local.  A silent local-timezone interpretation is
    the kind of bug that surfaces months later as data attributed to the wrong shift.
  - `to_epoch_nanos()` is integer arithmetic on a Python int end to end.  Present-day epoch nanoseconds need about
    61 bits and a float64 carries 53, so routing the conversion through float seconds would move the instant --
    and sample-status matching is by exact timestamp at nanosecond precision.
"""

import math
from datetime import datetime

from dp_python_lib.grpc import common_pb2

# Accepted input types for API parameters that map to a common.Timestamp:
# a timezone-aware datetime, epoch seconds (int or float), or an already-built Timestamp.
TimestampInput = datetime | int | float | common_pb2.Timestamp


NANOS_PER_SECOND = 1_000_000_000


def to_epoch_nanos(timestamp: common_pb2.Timestamp) -> int:
    """
    Converts a common.Timestamp into a single integer of epoch nanoseconds -- the inverse of to_timestamp().

    Integer arithmetic throughout, on a Python int, so there is no overflow and no precision loss.  Every
    conversions module in the library reads instants back through this; see the module docstring for why the
    integer path is a correctness requirement rather than an optimization.

    :param timestamp: The timestamp to convert.
    :return: Epoch nanoseconds as a Python int.
    """
    return timestamp.epochSeconds * NANOS_PER_SECOND + timestamp.nanoseconds


def to_timestamp(value: TimestampInput) -> common_pb2.Timestamp:
    """
    Converts a user-supplied time value into a common.Timestamp{epochSeconds, nanoseconds}.

    Accepts:
      - a timezone-aware datetime (naive datetimes are rejected to avoid silent local-timezone bugs),
      - epoch seconds as an int or float (float fractional part becomes nanoseconds),
      - an already-built common.Timestamp (returned as-is).

    :param value: The time value to convert.
    :return: An equivalent common.Timestamp.
    :raises ValueError: if a datetime is naive (has no tzinfo), or if the resulting epoch seconds are negative
        (pre-1970) -- common.Timestamp.epochSeconds is an unsigned (uint64) field and cannot represent them.
    :raises TypeError: if value is not one of the supported types.
    """
    if isinstance(value, common_pb2.Timestamp):
        return value

    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                "to_timestamp() requires a timezone-aware datetime; naive datetimes are rejected "
                "to avoid silent local-timezone bugs. Use datetime.now(timezone.utc) or attach tzinfo."
            )
        epoch = value.timestamp()
        return to_timestamp(epoch)

    if isinstance(value, bool):
        # bool is a subclass of int; reject it explicitly as it is virtually always a mistake.
        raise TypeError("to_timestamp() does not accept bool")

    if isinstance(value, (int, float)):
        # Floor the seconds (not truncate toward zero) so the fractional remainder, and therefore
        # nanoseconds, is always in [0, 1_000_000_000) even for negative epoch inputs.
        epoch_seconds = math.floor(value)
        nanoseconds = int(round((float(value) - epoch_seconds) * 1_000_000_000))
        # Guard against float rounding pushing nanoseconds up to a full second.
        if nanoseconds >= 1_000_000_000:
            epoch_seconds += 1
            nanoseconds -= 1_000_000_000
        timestamp = common_pb2.Timestamp()
        timestamp.epochSeconds = epoch_seconds
        timestamp.nanoseconds = nanoseconds
        return timestamp

    raise TypeError(
        f"to_timestamp() expects datetime, int/float epoch seconds, or common.Timestamp, got {type(value).__name__}"
    )
