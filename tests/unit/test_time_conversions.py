import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client.time_conversions import to_epoch_nanos, to_timestamp
from dp_python_lib.grpc import common_pb2


class TestToEpochNanos(unittest.TestCase):
    """to_epoch_nanos() is to_timestamp()'s inverse, shared by the three conversions modules."""

    def test_combines_seconds_and_nanoseconds(self):
        ts = common_pb2.Timestamp()
        ts.epochSeconds = 1_700_000_000
        ts.nanoseconds = 123_456_789
        self.assertEqual(to_epoch_nanos(ts), 1_700_000_000_123_456_789)

    def test_round_trips_with_to_timestamp_exactly(self):
        # The exactness is the point: present-day epoch nanoseconds need ~61 bits and a float64 carries 53, so a
        # conversion routed through float seconds would move the instant.
        original = 1_770_055_200_123_456_789
        ts = common_pb2.Timestamp()
        ts.epochSeconds, ts.nanoseconds = divmod(original, 1_000_000_000)
        self.assertEqual(to_epoch_nanos(ts), original)

    def test_zero_timestamp_is_zero(self):
        self.assertEqual(to_epoch_nanos(common_pb2.Timestamp()), 0)


class TestToTimestamp(unittest.TestCase):
    """Unit tests for the to_timestamp() conversion helper."""

    def test_passthrough_timestamp(self):
        ts = common_pb2.Timestamp()
        ts.epochSeconds = 123
        ts.nanoseconds = 456
        self.assertIs(to_timestamp(ts), ts)

    def test_int_epoch_seconds(self):
        ts = to_timestamp(1_700_000_000)
        self.assertEqual(ts.epochSeconds, 1_700_000_000)
        self.assertEqual(ts.nanoseconds, 0)

    def test_float_epoch_seconds_with_fraction(self):
        ts = to_timestamp(1_700_000_000.25)
        self.assertEqual(ts.epochSeconds, 1_700_000_000)
        self.assertEqual(ts.nanoseconds, 250_000_000)

    def test_aware_datetime_utc(self):
        dt = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
        expected_epoch = int(dt.timestamp())
        ts = to_timestamp(dt)
        self.assertEqual(ts.epochSeconds, expected_epoch)
        self.assertEqual(ts.nanoseconds, 0)

    def test_aware_datetime_nonzero_offset(self):
        tz = timezone(timedelta(hours=-5))
        dt = datetime(2023, 11, 14, 17, 13, 20, tzinfo=tz)  # same instant as the UTC test above
        ts = to_timestamp(dt)
        self.assertEqual(ts.epochSeconds, int(dt.timestamp()))

    def test_negative_epoch_rejected_by_uint64_field(self):
        # common.Timestamp.epochSeconds is uint64, so pre-1970 (negative) epochs cannot be represented.
        # Flooring keeps nanoseconds normalized, and the negative seconds are cleanly rejected at assignment
        # (ValueError: out of range) rather than silently producing an incorrect (seconds, nanos) pair.
        with self.assertRaises(ValueError):
            to_timestamp(-1.25)
        with self.assertRaises(ValueError):
            to_timestamp(-5)

    def test_naive_datetime_raises(self):
        with self.assertRaises(ValueError):
            # DTZ001: the naive datetime is the point of this test -- it must be rejected.
            to_timestamp(datetime(2023, 11, 14, 22, 13, 20))  # noqa: DTZ001

    def test_bool_raises(self):
        with self.assertRaises(TypeError):
            to_timestamp(True)

    def test_unsupported_type_raises(self):
        with self.assertRaises(TypeError):
            to_timestamp("2023-11-14")


if __name__ == "__main__":
    unittest.main()
