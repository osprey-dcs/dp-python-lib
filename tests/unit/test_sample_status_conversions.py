"""
Unit tests for sample_status_conversions: time-axis expansion and bucket-to-row conversion.

The exactness tests matter more than they look.  Status-to-sample matching is by exact timestamp at nanosecond
precision, so an expansion that drifts by a nanosecond produces rows that silently fail to match the samples they
label.  Several tests below use timestamps large enough that a float64 round-trip would visibly lose precision,
which is the failure mode they exist to catch.
"""

import os
import sys
import unittest
from itertools import pairwise

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from dp_python_lib.client.sample_status_conversions import (
    SampleStatusRow,
    bucket_to_rows,
    buckets_to_rows,
    expand_data_timestamps,
    iter_rows,
)
from dp_python_lib.grpc import common_pb2

# A present-day epoch-second value; large enough that seconds*1e9 exceeds float64's exact-integer range (2**53).
_EPOCH_SECONDS = 1_700_000_000


def _timestamp(epoch_seconds: int, nanoseconds: int = 0) -> common_pb2.Timestamp:
    """Builds a common.Timestamp."""
    timestamp = common_pb2.Timestamp()
    timestamp.epochSeconds = epoch_seconds
    timestamp.nanoseconds = nanoseconds
    return timestamp


def _clock_axis(start_seconds: int, start_nanos: int, period_nanos: int, count: int) -> common_pb2.DataTimestamps:
    """Builds a DataTimestamps carrying a SamplingClock."""
    axis = common_pb2.DataTimestamps()
    axis.samplingClock.startTime.CopyFrom(_timestamp(start_seconds, start_nanos))
    axis.samplingClock.periodNanos = period_nanos
    axis.samplingClock.count = count
    return axis


def _list_axis(pairs: list[tuple[int, int]]) -> common_pb2.DataTimestamps:
    """Builds a DataTimestamps carrying a TimestampList from (epoch_seconds, nanoseconds) pairs."""
    axis = common_pb2.DataTimestamps()
    axis.timestampList.timestamps.extend(_timestamp(s, n) for s, n in pairs)
    return axis


def _bucket(
    axis: common_pb2.DataTimestamps,
    pv_name: str = "ABC:1",
    status_codes: list[int] | None = None,
    confidence: list[float] | None = None,
    reasons: list[str] | None = None,
    domain: str = "data_quality",
    layer: str = "ml_model_v1",
) -> common_pb2.SampleStatusBucket:
    """Builds a SampleStatusBucket, defaulting the status codes to one zero per timestamp on the axis."""
    bucket = common_pb2.SampleStatusBucket()
    bucket.domain = domain
    bucket.layer = layer
    bucket.dataTimestamps.CopyFrom(axis)
    bucket.statusColumn.pvName = pv_name

    if status_codes is None:
        status_codes = [0] * len(expand_data_timestamps(axis))
    bucket.statusColumn.statusCodes[:] = status_codes

    if confidence is not None:
        bucket.statusColumn.confidence[:] = confidence
    if reasons is not None:
        bucket.statusColumn.reasons[:] = reasons

    return bucket


class TestExpandDataTimestamps(unittest.TestCase):
    """Tests for expand_data_timestamps()."""

    def test_sampling_clock_expands_arithmetically(self):
        axis = _clock_axis(_EPOCH_SECONDS, 0, 1_000_000, 5)
        base = _EPOCH_SECONDS * 1_000_000_000
        self.assertEqual(expand_data_timestamps(axis), [base + i * 1_000_000 for i in range(5)])

    def test_sampling_clock_count_one(self):
        axis = _clock_axis(_EPOCH_SECONDS, 42, 1_000_000, 1)
        self.assertEqual(expand_data_timestamps(axis), [_EPOCH_SECONDS * 1_000_000_000 + 42])

    def test_sampling_clock_expansion_is_exact_at_nanosecond_precision(self):
        """
        The exact-match contract: every expanded position must be exactly startTime + i * periodNanos.

        This is the test that would fail if the implementation computed in float seconds.  With a present-day epoch
        and a 1 ns period, the values exceed 2**53, so a float64 round-trip cannot represent consecutive
        nanoseconds distinctly -- adjacent positions would collide instead of differing by exactly 1.
        """
        start_nanos = _EPOCH_SECONDS * 1_000_000_000 + 123_456_789
        axis = _clock_axis(_EPOCH_SECONDS, 123_456_789, 1, 100)
        expanded = expand_data_timestamps(axis)

        self.assertEqual(expanded, [start_nanos + i for i in range(100)])
        # Explicitly assert adjacent positions differ by exactly one nanosecond (what float would smear away).
        self.assertTrue(all(b - a == 1 for a, b in pairwise(expanded)))
        self.assertGreater(expanded[0], 2**53, "test timestamps must exceed float64's exact-integer range")

    def test_sampling_clock_carries_nanoseconds_across_second_boundary(self):
        """A period that pushes past 1e9 nanoseconds must roll into the next second, not wrap within one."""
        axis = _clock_axis(_EPOCH_SECONDS, 900_000_000, 200_000_000, 3)
        base = _EPOCH_SECONDS * 1_000_000_000
        self.assertEqual(
            expand_data_timestamps(axis),
            [base + 900_000_000, base + 1_100_000_000, base + 1_300_000_000],
        )

    def test_sampling_clock_rejects_non_positive_period(self):
        axis = _clock_axis(_EPOCH_SECONDS, 0, 1_000, 3)
        axis.samplingClock.periodNanos = 0
        with self.assertRaises(ValueError) as ctx:
            expand_data_timestamps(axis)
        self.assertIn("periodNanos", str(ctx.exception))

    def test_sampling_clock_rejects_zero_count(self):
        axis = _clock_axis(_EPOCH_SECONDS, 0, 1_000, 3)
        axis.samplingClock.count = 0
        with self.assertRaises(ValueError) as ctx:
            expand_data_timestamps(axis)
        self.assertIn("count", str(ctx.exception))

    def test_timestamp_list_expands_in_order(self):
        axis = _list_axis([(_EPOCH_SECONDS, 5), (_EPOCH_SECONDS, 250_000_000), (_EPOCH_SECONDS + 1, 0)])
        base = _EPOCH_SECONDS * 1_000_000_000
        self.assertEqual(expand_data_timestamps(axis), [base + 5, base + 250_000_000, base + 1_000_000_000])

    def test_timestamp_list_preserves_nanosecond_distinctions(self):
        """Adjacent list entries one nanosecond apart must stay distinct after conversion."""
        axis = _list_axis([(_EPOCH_SECONDS, 1), (_EPOCH_SECONDS, 2), (_EPOCH_SECONDS, 3)])
        expanded = expand_data_timestamps(axis)
        self.assertEqual(len(set(expanded)), 3)
        self.assertTrue(all(b - a == 1 for a, b in pairwise(expanded)))

    def test_unset_axis_raises(self):
        with self.assertRaises(ValueError) as ctx:
            expand_data_timestamps(common_pb2.DataTimestamps())
        self.assertIn("samplingClock", str(ctx.exception))


class TestSampleStatusRow(unittest.TestCase):
    """Tests for the SampleStatusRow value object."""

    def test_timestamp_property_round_trips(self):
        row = SampleStatusRow("ABC:1", _EPOCH_SECONDS * 1_000_000_000 + 123_456_789, 1, "d", "l")
        timestamp = row.timestamp
        self.assertEqual(timestamp.epochSeconds, _EPOCH_SECONDS)
        self.assertEqual(timestamp.nanoseconds, 123_456_789)

    def test_equality_and_repr(self):
        a = SampleStatusRow("ABC:1", 100, 2, "d", "l", confidence=0.5, reason="spike")
        b = SampleStatusRow("ABC:1", 100, 2, "d", "l", confidence=0.5, reason="spike")
        c = SampleStatusRow("ABC:1", 100, 3, "d", "l", confidence=0.5, reason="spike")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, "not a row")
        self.assertIn("ABC:1", repr(a))


class TestBucketToRows(unittest.TestCase):
    """Tests for bucket_to_rows()."""

    def test_expands_one_row_per_sample(self):
        axis = _clock_axis(_EPOCH_SECONDS, 0, 1_000_000, 3)
        bucket = _bucket(axis, status_codes=[1, 2, 3])
        rows = bucket_to_rows(bucket)

        self.assertEqual(len(rows), 3)
        self.assertEqual([r.status_code for r in rows], [1, 2, 3])
        self.assertEqual([r.pv_name for r in rows], ["ABC:1"] * 3)
        self.assertEqual([r.domain for r in rows], ["data_quality"] * 3)
        self.assertEqual([r.layer for r in rows], ["ml_model_v1"] * 3)

    def test_row_timestamps_match_the_axis_exactly(self):
        axis = _clock_axis(_EPOCH_SECONDS, 7, 1, 4)
        rows = bucket_to_rows(_bucket(axis, status_codes=[0, 0, 0, 0]))
        self.assertEqual([r.epoch_nanos for r in rows], expand_data_timestamps(axis))

    def test_omitted_confidence_and_reasons_are_none(self):
        """
        Absence means "no assertion": an omitted optional array must surface as None, never as 0.0 or "".
        0.0 is a legitimate confidence value, so a fabricated default would be indistinguishable from a real one.
        """
        axis = _list_axis([(_EPOCH_SECONDS, 0), (_EPOCH_SECONDS, 1)])
        rows = bucket_to_rows(_bucket(axis, status_codes=[1, 2]))

        self.assertEqual([r.confidence for r in rows], [None, None])
        self.assertEqual([r.reason for r in rows], [None, None])

    def test_populated_confidence_and_reasons_are_carried(self):
        axis = _list_axis([(_EPOCH_SECONDS, 0), (_EPOCH_SECONDS, 1)])
        bucket = _bucket(axis, status_codes=[1, 2], confidence=[0.25, 0.75], reasons=["spike", "dropout"])
        rows = bucket_to_rows(bucket)

        self.assertEqual([r.confidence for r in rows], [0.25, 0.75])
        self.assertEqual([r.reason for r in rows], ["spike", "dropout"])

    def test_zero_confidence_is_preserved_not_treated_as_absent(self):
        """A real 0.0 confidence must survive; only an omitted array becomes None."""
        axis = _list_axis([(_EPOCH_SECONDS, 0), (_EPOCH_SECONDS, 1)])
        rows = bucket_to_rows(_bucket(axis, status_codes=[1, 2], confidence=[0.0, 0.0]))
        self.assertEqual([r.confidence for r in rows], [0.0, 0.0])
        self.assertIsNotNone(rows[0].confidence)

    def test_empty_reason_string_within_populated_array_is_none(self):
        """A blank entry in a partially-populated reasons array means "no reason for this sample"."""
        axis = _list_axis([(_EPOCH_SECONDS, 0), (_EPOCH_SECONDS, 1), (_EPOCH_SECONDS, 2)])
        bucket = _bucket(axis, status_codes=[1, 2, 3], reasons=["spike", "", "dropout"])
        rows = bucket_to_rows(bucket)
        self.assertEqual([r.reason for r in rows], ["spike", None, "dropout"])

    def test_misaligned_status_codes_raise(self):
        axis = _clock_axis(_EPOCH_SECONDS, 0, 1_000, 3)
        bucket = _bucket(axis, status_codes=[1, 2])
        with self.assertRaises(ValueError) as ctx:
            bucket_to_rows(bucket)
        message = str(ctx.exception)
        self.assertIn("statusCodes", message)
        self.assertIn("ABC:1", message, "the error must name the offending PV")

    def test_misaligned_confidence_raises(self):
        axis = _clock_axis(_EPOCH_SECONDS, 0, 1_000, 3)
        bucket = _bucket(axis, status_codes=[1, 2, 3], confidence=[0.5])
        with self.assertRaises(ValueError) as ctx:
            bucket_to_rows(bucket)
        self.assertIn("confidence", str(ctx.exception))

    def test_misaligned_reasons_raise(self):
        axis = _clock_axis(_EPOCH_SECONDS, 0, 1_000, 3)
        bucket = _bucket(axis, status_codes=[1, 2, 3], reasons=["a", "b"])
        with self.assertRaises(ValueError) as ctx:
            bucket_to_rows(bucket)
        self.assertIn("reasons", str(ctx.exception))

    def test_unset_axis_raises(self):
        bucket = common_pb2.SampleStatusBucket()
        bucket.statusColumn.pvName = "ABC:1"
        with self.assertRaises(ValueError):
            bucket_to_rows(bucket)


class TestBucketsToRows(unittest.TestCase):
    """Tests for buckets_to_rows()."""

    def test_concatenates_in_bucket_order(self):
        axis = _list_axis([(_EPOCH_SECONDS, 0)])
        first = _bucket(axis, pv_name="ABC:1", status_codes=[1])
        second = _bucket(axis, pv_name="ABC:2", status_codes=[2])
        rows = buckets_to_rows([first, second])

        self.assertEqual([r.pv_name for r in rows], ["ABC:1", "ABC:2"])
        self.assertEqual([r.status_code for r in rows], [1, 2])

    def test_same_pv_and_timestamp_in_different_layers_both_survive(self):
        """
        The identity key is (pvName, timestamp, domain, layer), so the same PV at the same instant in two layers is
        two distinct statuses.  Neither may be merged away or deduplicated.
        """
        axis = _list_axis([(_EPOCH_SECONDS, 0)])
        first = _bucket(axis, status_codes=[1], layer="ml_model_v1")
        second = _bucket(axis, status_codes=[9], layer="operator_override")
        rows = buckets_to_rows([first, second])

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].epoch_nanos, rows[1].epoch_nanos)
        self.assertEqual([r.layer for r in rows], ["ml_model_v1", "operator_override"])
        self.assertEqual([r.status_code for r in rows], [1, 9])

    def test_empty_list_yields_no_rows(self):
        self.assertEqual(buckets_to_rows([]), [])


class _FakeResult:
    """Stands in for a QuerySampleStatusesApiResult, exposing only the sample_status_buckets the converter reads."""

    def __init__(self, buckets):
        self.sample_status_buckets = buckets


class TestIterRows(unittest.TestCase):
    """Tests for iter_rows()."""

    def test_flattens_pages_in_order(self):
        axis = _list_axis([(_EPOCH_SECONDS, 0)])
        pages = [
            _FakeResult([_bucket(axis, pv_name="ABC:1", status_codes=[1])]),
            _FakeResult([_bucket(axis, pv_name="ABC:2", status_codes=[2])]),
        ]
        rows = list(iter_rows(iter(pages)))
        self.assertEqual([r.pv_name for r in rows], ["ABC:1", "ABC:2"])

    def test_is_lazy(self):
        """Pages must be expanded as they arrive, so a large query never materializes every row at once."""
        axis = _list_axis([(_EPOCH_SECONDS, 0)])
        consumed = []

        def pages():
            for name in ("ABC:1", "ABC:2", "ABC:3"):
                consumed.append(name)
                yield _FakeResult([_bucket(axis, pv_name=name, status_codes=[1])])

        iterator = iter_rows(pages())
        first = next(iterator)
        self.assertEqual(first.pv_name, "ABC:1")
        self.assertEqual(consumed, ["ABC:1"], "later pages must not be pulled before they are needed")

    def test_page_error_propagates(self):
        """iter_sample_statuses() raises RuntimeError on an error page; that must not be swallowed here."""

        def pages():
            yield _FakeResult([])
            raise RuntimeError("querySampleStatuses failed during paging: boom")

        with self.assertRaises(RuntimeError) as ctx:
            list(iter_rows(pages()))
        self.assertIn("boom", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
