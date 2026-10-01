import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client import bucket_conversions as bc
from dp_python_lib.client import data_frame as dfb
from dp_python_lib.client.query_client import PvQuery, QueryBucketsApiResult, QueryParams
from dp_python_lib.client.time_conversions import from_epoch_nanos, to_epoch_nanos
from dp_python_lib.grpc import common_pb2, query_pb2

# The pandas half depends on the optional [analysis] extra; skip those tests cleanly when it is absent.  The
# pure-Python half needs no optional deps and always runs.
try:
    import pandas  # noqa: F401 -- availability probe for the [analysis] extra

    _HAVE_ANALYSIS = True
except ImportError:
    _HAVE_ANALYSIS = False

# A present-day epoch: its nanoseconds need ~61 bits, so any float round trip would move them.
T0 = datetime(2026, 9, 30, 18, 0, 0, 123456, tzinfo=timezone.utc)
T0_NANOS = to_epoch_nanos(dfb.sampling_clock(T0, 1, 1).samplingClock.startTime)
PERIOD = 1_000_000


def _clock(count=5, start_nanos=T0_NANOS, period=PERIOD):
    return dfb.sampling_clock(from_epoch_nanos(start_nanos), period, count)


def _raw_list(epoch_nanos):
    """A TimestampList built without timestamp_list(), which refuses the repeated/decreasing axes some tests need."""
    axis = common_pb2.DataTimestamps()
    for nanos in epoch_nanos:
        axis.timestampList.timestamps.add().CopyFrom(from_epoch_nanos(nanos))
    return axis


def _bucket(pv, axis, column, provider_id="prov-1", provider_name="daq"):
    bucket = common_pb2.DataBucket(pvName=pv, providerId=provider_id, providerName=provider_name)
    bucket.dataTimestamps.CopyFrom(axis)
    arm = {
        common_pb2.DataColumn: "dataColumn",
        common_pb2.SerializedDataColumn: "serializedDataColumn",
        common_pb2.DoubleColumn: "doubleColumn",
        common_pb2.FloatColumn: "floatColumn",
        common_pb2.Int64Column: "int64Column",
        common_pb2.Int32Column: "int32Column",
        common_pb2.BoolColumn: "boolColumn",
        common_pb2.StringColumn: "stringColumn",
        common_pb2.EnumColumn: "enumColumn",
        common_pb2.ImageColumn: "imageColumn",
        common_pb2.StructColumn: "structColumn",
        common_pb2.DoubleArrayColumn: "doubleArrayColumn",
        common_pb2.FloatArrayColumn: "floatArrayColumn",
        common_pb2.Int32ArrayColumn: "int32ArrayColumn",
        common_pb2.Int64ArrayColumn: "int64ArrayColumn",
        common_pb2.BoolArrayColumn: "boolArrayColumn",
    }[type(column)]
    getattr(bucket.dataValues, arm).CopyFrom(column)
    return bucket


def _double_bucket(pv="PV:A", count=5, start_nanos=T0_NANOS, values=None, **kwargs):
    values = values if values is not None else [float(i) for i in range(count)]
    return _bucket(pv, _clock(count, start_nanos), dfb.double_column(pv, values), **kwargs)


# Every column arm a DataBucket can carry except the serialized one, with three samples apiece and the values
# bucket_values() should yield.
def _all_arm_cases(pv="PV:X"):
    return [
        (dfb.double_column(pv, [1.5, 2.5, 3.5]), [1.5, 2.5, 3.5]),
        (dfb.float_column(pv, [0.5, 1.5, 2.5]), [0.5, 1.5, 2.5]),
        (dfb.int64_column(pv, [2**40, -1, 0]), [2**40, -1, 0]),
        (dfb.int32_column(pv, [1, 2, 3]), [1, 2, 3]),
        (dfb.bool_column(pv, [True, False, True]), [True, False, True]),
        (dfb.string_column(pv, ["a", "b", "c"]), ["a", "b", "c"]),
        (dfb.enum_column(pv, [0, 2, 1], "mode:v1"), [0, 2, 1]),
        (dfb.image_column(pv, [b"i0", b"i1", b"i2"], 2, 1, 1, "raw-u8"), [b"i0", b"i1", b"i2"]),
        (dfb.struct_column(pv, [b"s0", b"s1", b"s2"], "schema:v1"), [b"s0", b"s1", b"s2"]),
        (dfb.double_array_column(pv, [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]), [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]),
        (dfb.float_array_column(pv, [[0.5], [1.5], [2.5]]), [[0.5], [1.5], [2.5]]),
        (dfb.int32_array_column(pv, [[1, 2], [3, 4], [5, 6]]), [[1, 2], [3, 4], [5, 6]]),
        (dfb.int64_array_column(pv, [[1], [2], [3]]), [[1], [2], [3]]),
        (dfb.bool_array_column(pv, [[True], [False], [True]]), [[True], [False], [True]]),
        (dfb.data_column(pv, [1.5, None, "x"]), [1.5, None, "x"]),
    ]


# ----------------------------------------------------------------------
# one bucket, pure Python
# ----------------------------------------------------------------------


class TestBucketColumn(unittest.TestCase):
    def test_returns_the_set_arm(self):
        bucket = _double_bucket()
        self.assertIsInstance(bc.bucket_column(bucket), common_pb2.DoubleColumn)

    def test_serialized_column_is_reachable(self):
        column = dfb.serialized_column("PV:S", b"\x01\x02", "my-codec")
        bucket = _bucket("PV:S", _clock(2), column)
        reached = bc.bucket_column(bucket)
        self.assertEqual(reached.payload, b"\x01\x02")
        self.assertEqual(reached.encoding, "my-codec")

    def test_unset_data_values_raises(self):
        bucket = common_pb2.DataBucket(pvName="PV:E")
        bucket.dataTimestamps.CopyFrom(_clock(1))
        with self.assertRaises(ValueError) as ctx:
            bc.bucket_column(bucket)
        self.assertIn("PV:E", str(ctx.exception))


class TestBucketToDataFrame(unittest.TestCase):
    def test_every_arm_lands_in_its_field_and_reads_back(self):
        from dp_python_lib.client import data_frame_conversions as dfc

        for column, expected in _all_arm_cases():
            with self.subTest(kind=type(column).__name__):
                bucket = _bucket("PV:X", _clock(3), column)
                frame = bc.bucket_to_data_frame(bucket)
                self.assertEqual(dfc.data_frame_columns(frame), {"PV:X": expected})
                self.assertEqual(frame.dataTimestamps, bucket.dataTimestamps)

    def test_serialized_lands_in_serialized_columns(self):
        from dp_python_lib.client import data_frame_conversions as dfc

        bucket = _bucket("PV:S", _clock(2), dfb.serialized_column("PV:S", b"xy", "codec"))
        frame = bc.bucket_to_data_frame(bucket)
        self.assertEqual(len(frame.serializedDataColumns), 1)
        self.assertEqual(dfc.data_frame_columns(frame), {})  # skipped by design

    def test_unset_axis_raises(self):
        bucket = common_pb2.DataBucket(pvName="PV:N")
        bucket.dataValues.doubleColumn.CopyFrom(dfb.double_column("PV:N", [1.0]))
        with self.assertRaises(ValueError) as ctx:
            bc.bucket_to_data_frame(bucket)
        self.assertIn("PV:N", str(ctx.exception))
        self.assertIn("dataTimestamps", str(ctx.exception))


class TestBucketTimestampsAndValues(unittest.TestCase):
    def test_sampling_clock_is_nanosecond_exact(self):
        bucket = _bucket("PV:A", _clock(3, period=7), dfb.double_column("PV:A", [1.0, 2.0, 3.0]))
        self.assertEqual(bc.bucket_timestamps(bucket), [T0_NANOS, T0_NANOS + 7, T0_NANOS + 14])

    def test_timestamp_list_is_nanosecond_exact(self):
        nanos = [T0_NANOS + 1, T0_NANOS + 999, T0_NANOS + 1_000_000_001]
        bucket = _bucket("PV:A", _raw_list(nanos), dfb.double_column("PV:A", [1.0, 2.0, 3.0]))
        self.assertEqual(bc.bucket_timestamps(bucket), nanos)

    def test_empty_list_axis_raises(self):
        bucket = _bucket("PV:A", _raw_list([]), dfb.double_column("PV:A", [1.0]))
        bucket.dataTimestamps.timestampList.SetInParent()
        with self.assertRaises(ValueError):
            bc.bucket_timestamps(bucket)

    def test_every_arm_values(self):
        for column, expected in _all_arm_cases():
            with self.subTest(kind=type(column).__name__):
                self.assertEqual(bc.bucket_values(_bucket("PV:X", _clock(3), column)), expected)

    def test_legacy_gap_is_none(self):
        bucket = _bucket("PV:G", _clock(3), dfb.data_column("PV:G", [1, None, 3]))
        self.assertEqual(bc.bucket_values(bucket), [1, None, 3])

    def test_serialized_values_refused(self):
        bucket = _bucket("PV:S", _clock(2), dfb.serialized_column("PV:S", b"xy", "my-codec"))
        with self.assertRaises(ValueError) as ctx:
            bc.bucket_values(bucket)
        self.assertIn("PV:S", str(ctx.exception))
        self.assertIn("my-codec", str(ctx.exception))
        self.assertIn("bucket_column()", str(ctx.exception))

    def test_misaligned_bucket_raises(self):
        bucket = _bucket("PV:M", _clock(3), dfb.double_column("PV:M", [1.0, 2.0]))
        with self.assertRaises(ValueError) as ctx:
            bc.bucket_values(bucket)
        self.assertIn("PV:M", str(ctx.exception))

    def test_repeated_timestamps_convert_without_error(self):
        # Ingestion allows a non-decreasing list; the client's write-side strictly-increasing rule must not apply.
        nanos = [T0_NANOS, T0_NANOS, T0_NANOS + 5]
        bucket = _bucket("PV:D", _raw_list(nanos), dfb.double_column("PV:D", [1.0, 2.0, 3.0]))
        self.assertEqual(bc.bucket_timestamps(bucket), nanos)
        self.assertEqual(bc.bucket_values(bucket), [1.0, 2.0, 3.0])


# ----------------------------------------------------------------------
# trimming
# ----------------------------------------------------------------------


class TestTrimBucket(unittest.TestCase):
    def _ns(self, offset):
        return from_epoch_nanos(T0_NANOS + offset)

    def test_begin_kept_end_dropped(self):
        bucket = _double_bucket(count=5)  # samples at 0, P, 2P, 3P, 4P
        trimmed = bc.trim_bucket(bucket, self._ns(PERIOD), self._ns(3 * PERIOD))
        self.assertEqual(bc.bucket_values(trimmed), [1.0, 2.0])
        self.assertEqual(bc.bucket_timestamps(trimmed), [T0_NANOS + PERIOD, T0_NANOS + 2 * PERIOD])

    def test_sampling_clock_start_is_shifted_and_stays_a_clock(self):
        trimmed = bc.trim_bucket(_double_bucket(count=5), self._ns(PERIOD + 1), self._ns(10 * PERIOD))
        self.assertEqual(trimmed.dataTimestamps.WhichOneof("value"), "samplingClock")
        clock = trimmed.dataTimestamps.samplingClock
        self.assertEqual(to_epoch_nanos(clock.startTime), T0_NANOS + 2 * PERIOD)
        self.assertEqual(clock.periodNanos, PERIOD)
        self.assertEqual(clock.count, 3)

    def test_trim_to_nothing_returns_none(self):
        bucket = _double_bucket(count=3)
        self.assertIsNone(bc.trim_bucket(bucket, self._ns(10 * PERIOD), self._ns(11 * PERIOD)))
        self.assertIsNone(bc.trim_bucket(bucket, self._ns(1), self._ns(PERIOD)))  # between two samples

    def test_range_covering_everything_returns_an_equal_copy(self):
        bucket = _double_bucket(count=3)
        trimmed = bc.trim_bucket(bucket, self._ns(-1), self._ns(10 * PERIOD))
        self.assertEqual(trimmed, bucket)
        self.assertIsNot(trimmed, bucket)

    def test_input_is_not_modified_and_provider_and_metadata_carry_over(self):
        metadata = dfb.column_metadata(tags=["t"])
        bucket = _bucket("PV:A", _clock(3), dfb.double_column("PV:A", [1.0, 2.0, 3.0], metadata=metadata))
        before = common_pb2.DataBucket()
        before.CopyFrom(bucket)
        trimmed = bc.trim_bucket(bucket, self._ns(PERIOD), self._ns(10 * PERIOD))
        self.assertEqual(bucket, before)
        self.assertEqual((trimmed.pvName, trimmed.providerId, trimmed.providerName), ("PV:A", "prov-1", "daq"))
        self.assertEqual(list(trimmed.dataValues.doubleColumn.metadata.tags), ["t"])

    def test_timestamp_list_is_sliced(self):
        nanos = [T0_NANOS, T0_NANOS + 10, T0_NANOS + 20, T0_NANOS + 30]
        bucket = _bucket("PV:L", _raw_list(nanos), dfb.int32_column("PV:L", [1, 2, 3, 4]))
        trimmed = bc.trim_bucket(bucket, self._ns(10), self._ns(30))
        self.assertEqual(bc.bucket_timestamps(trimmed), nanos[1:3])
        self.assertEqual(bc.bucket_values(trimmed), [2, 3])

    def test_repeated_timestamps_at_each_bound(self):
        # Two samples at begin, two at end: both copies at begin are kept, both at end dropped.
        nanos = [T0_NANOS, T0_NANOS + 10, T0_NANOS + 10, T0_NANOS + 20, T0_NANOS + 30, T0_NANOS + 30]
        bucket = _bucket("PV:D", _raw_list(nanos), dfb.double_column("PV:D", [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]))
        trimmed = bc.trim_bucket(bucket, self._ns(10), self._ns(30))
        self.assertEqual(bc.bucket_values(trimmed), [1.0, 2.0, 3.0])

    def test_decreasing_axis_raises_naming_position(self):
        nanos = [T0_NANOS, T0_NANOS + 20, T0_NANOS + 10]
        bucket = _bucket("PV:R", _raw_list(nanos), dfb.double_column("PV:R", [1.0, 2.0, 3.0]))
        with self.assertRaises(ValueError) as ctx:
            bc.trim_bucket(bucket, self._ns(0), self._ns(100))
        self.assertIn("PV:R", str(ctx.exception))
        self.assertIn("position 2", str(ctx.exception))

    def test_begin_not_before_end_raises(self):
        bucket = _double_bucket()
        for begin, end in ((10, 10), (20, 10)):
            with self.subTest(begin=begin, end=end), self.assertRaises(ValueError) as ctx:
                bc.trim_bucket(bucket, self._ns(begin), self._ns(end))
            self.assertIn("strictly before", str(ctx.exception))

    def test_array_bucket_trims_in_whole_samples(self):
        column = dfb.double_array_column("PV:W", [[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]], [[9.0] * 2] * 2])
        trimmed = bc.trim_bucket(_bucket("PV:W", _clock(3), column), self._ns(PERIOD), self._ns(2 * PERIOD))
        self.assertEqual(bc.bucket_values(trimmed), [[5.0, 6.0, 7.0, 8.0]])
        self.assertEqual(list(trimmed.dataValues.doubleArrayColumn.dimensions.dims), [2, 2])

    def test_array_bucket_with_absent_dims_raises_rather_than_slicing(self):
        column = common_pb2.DoubleArrayColumn(name="PV:W", values=[1.0, 2.0, 3.0, 4.0])  # no dims
        bucket = _bucket("PV:W", _clock(2), column)
        with self.assertRaises(ValueError) as ctx:
            bc.trim_bucket(bucket, self._ns(0), self._ns(PERIOD))
        self.assertIn("dimensions", str(ctx.exception))

    def test_image_bucket_keeps_its_descriptor(self):
        column = dfb.image_column("PV:I", [b"a", b"b", b"c"], 4, 3, 1, "png")
        trimmed = bc.trim_bucket(_bucket("PV:I", _clock(3), column), self._ns(PERIOD), self._ns(10 * PERIOD))
        self.assertEqual(bc.bucket_values(trimmed), [b"b", b"c"])
        self.assertEqual(trimmed.dataValues.imageColumn.imageDescriptor.encoding, "png")

    def test_serialized_bucket_raises(self):
        bucket = _bucket("PV:S", _clock(2), dfb.serialized_column("PV:S", b"xy", "codec"))
        with self.assertRaises(ValueError) as ctx:
            bc.trim_bucket(bucket, self._ns(0), self._ns(PERIOD))
        self.assertIn("trimmed", str(ctx.exception))

    def test_repeated_timestamps_trim_without_error(self):
        nanos = [T0_NANOS, T0_NANOS, T0_NANOS]
        bucket = _bucket("PV:D", _raw_list(nanos), dfb.double_column("PV:D", [1.0, 2.0, 3.0]))
        self.assertEqual(bc.bucket_values(bc.trim_bucket(bucket, self._ns(0), self._ns(1))), [1.0, 2.0, 3.0])


# ----------------------------------------------------------------------
# grouping
# ----------------------------------------------------------------------


class TestBucketsByPv(unittest.TestCase):
    def test_interleaved_pvs_and_out_of_order_buckets(self):
        a_late = _double_bucket("PV:A", start_nanos=T0_NANOS + 100 * PERIOD)
        b_only = _double_bucket("PV:B")
        a_early = _double_bucket("PV:A")
        groups = bc.buckets_by_pv([a_late, b_only, a_early])
        self.assertEqual(list(groups), ["PV:A", "PV:B"])
        self.assertEqual(groups["PV:A"], [a_early, a_late])
        self.assertEqual(groups["PV:B"], [b_only])

    def test_equal_first_timestamps_keep_input_order(self):
        first = _double_bucket("PV:A", values=[1.0] * 5)
        second = _double_bucket("PV:A", values=[2.0] * 5)
        self.assertEqual(bc.buckets_by_pv([first, second])["PV:A"], [first, second])

    def test_empty_input(self):
        self.assertEqual(bc.buckets_by_pv([]), {})


# ----------------------------------------------------------------------
# pandas ([analysis] extra)
# ----------------------------------------------------------------------


@unittest.skipUnless(_HAVE_ANALYSIS, "requires the [analysis] extra (pandas)")
class TestBucketsToDataFrames(unittest.TestCase):
    def test_one_frame_per_pv_with_exact_index(self):
        a1 = _double_bucket("PV:A", count=2)
        a2 = _double_bucket("PV:A", count=2, start_nanos=T0_NANOS + 10 * PERIOD, values=[7.0, 8.0])
        b1 = _bucket("PV:B", _clock(3), dfb.int32_column("PV:B", [1, 2, 3]))
        frames = bc.buckets_to_dataframes([a2, b1, a1])
        self.assertEqual(list(frames), ["PV:A", "PV:B"])
        df_a = frames["PV:A"]
        self.assertEqual(list(df_a.columns), ["PV:A"])
        self.assertEqual(list(df_a["PV:A"]), [0.0, 1.0, 7.0, 8.0])
        self.assertEqual(
            [ts.value for ts in df_a.index],
            [T0_NANOS, T0_NANOS + PERIOD, T0_NANOS + 10 * PERIOD, T0_NANOS + 11 * PERIOD],
        )
        self.assertEqual(str(df_a.index.tz), "UTC")
        self.assertEqual(str(frames["PV:B"]["PV:B"].dtype), "int32")

    def test_narrow_dtypes_survive_concatenation(self):
        f1 = _bucket("PV:F", _clock(2), dfb.float_column("PV:F", [0.5, 1.5]))
        f2 = _bucket("PV:F", _clock(2, start_nanos=T0_NANOS + 10 * PERIOD), dfb.float_column("PV:F", [2.5, 3.5]))
        self.assertEqual(str(bc.buckets_to_dataframes([f1, f2])["PV:F"]["PV:F"].dtype), "float32")

    def test_every_non_serialized_arm_converts(self):
        for column, expected in _all_arm_cases():
            with self.subTest(kind=type(column).__name__):
                df = bc.buckets_to_dataframes([_bucket("PV:X", _clock(3), column)])["PV:X"]
                self.assertEqual(list(df["PV:X"]), expected)

    def test_overlapping_buckets_are_kept(self):
        a1 = _double_bucket("PV:A", count=3)
        a2 = _double_bucket("PV:A", count=3, start_nanos=T0_NANOS + PERIOD)
        df = bc.buckets_to_dataframes([a1, a2])["PV:A"]
        self.assertEqual(len(df), 6)
        self.assertFalse(df.index.is_unique)

    def test_column_named_for_the_pv(self):
        bucket = _bucket("PV:A", _clock(2), dfb.double_column("ingested-name", [1.0, 2.0]))
        self.assertEqual(list(bc.buckets_to_dataframes([bucket])["PV:A"].columns), ["PV:A"])

    def test_buckets_attr_descriptors(self):
        a1 = _double_bucket("PV:A", count=2, provider_id="p1", provider_name="one")
        a2 = _double_bucket("PV:A", count=3, start_nanos=T0_NANOS + 10 * PERIOD, provider_id="p2", provider_name="two")
        entries = bc.buckets_to_dataframes([a1, a2])["PV:A"].attrs["buckets"]
        self.assertEqual(
            [
                (e["first_nanos"], e["last_nanos"], e["sample_count"], e["provider_id"], e["provider_name"])
                for e in entries
            ],
            [
                (T0_NANOS, T0_NANOS + PERIOD, 2, "p1", "one"),
                (T0_NANOS + 10 * PERIOD, T0_NANOS + 12 * PERIOD, 3, "p2", "two"),
            ],
        )

    def test_uniform_metadata_is_summarized(self):
        metadata = dfb.column_metadata(tags=["vacuum"], attributes={"unit": "Torr"})
        buckets = [
            _bucket(
                "PV:A",
                _clock(2, start_nanos=T0_NANOS + i * 10 * PERIOD),
                dfb.double_column("PV:A", [1.0, 2.0], metadata=metadata),
            )
            for i in range(2)
        ]
        attrs = bc.buckets_to_dataframes(buckets)["PV:A"].attrs
        self.assertEqual(attrs["column_metadata"]["PV:A"]["tags"], ["vacuum"])
        self.assertEqual(attrs["buckets"][1]["column_metadata"]["attributes"], {"unit": "Torr"})

    def test_differing_metadata_is_kept_per_bucket_only(self):
        buckets = [
            _bucket(
                "PV:A",
                _clock(2, start_nanos=T0_NANOS + i * 10 * PERIOD),
                dfb.double_column("PV:A", [1.0, 2.0], metadata=dfb.column_metadata(tags=[f"run-{i}"])),
            )
            for i in range(2)
        ]
        attrs = bc.buckets_to_dataframes(buckets)["PV:A"].attrs
        self.assertNotIn("column_metadata", attrs)
        self.assertEqual([e["column_metadata"]["tags"] for e in attrs["buckets"]], [["run-0"], ["run-1"]])

    def test_exclude_column_metadata_keeps_structural_attrs(self):
        bucket = _bucket(
            "PV:E", _clock(3), dfb.enum_column("PV:E", [0, 1, 2], "mode:v1", metadata=dfb.column_metadata(tags=["t"]))
        )
        attrs = bc.buckets_to_dataframes([bucket], exclude_column_metadata=True)["PV:E"].attrs
        self.assertNotIn("column_metadata", attrs)
        self.assertNotIn("column_metadata", attrs["buckets"][0])
        self.assertEqual(attrs["enum_ids"], {"PV:E": "mode:v1"})

    def test_structural_attrs_for_arrays_images_structs(self):
        array = _bucket("PV:W", _clock(2), dfb.double_array_column("PV:W", [[[1.0, 2.0]], [[3.0, 4.0]]]))
        image = _bucket("PV:I", _clock(2), dfb.image_column("PV:I", [b"a", b"b"], 4, 3, 1, "png"))
        struct = _bucket("PV:S", _clock(2), dfb.struct_column("PV:S", [b"x", b"y"], "schema:v1"))
        frames = bc.buckets_to_dataframes([array, image, struct])
        self.assertEqual(frames["PV:W"].attrs["dimensions"], {"PV:W": [1, 2]})
        self.assertEqual(frames["PV:I"].attrs["image_descriptors"]["PV:I"]["encoding"], "png")
        self.assertEqual(frames["PV:S"].attrs["schema_ids"], {"PV:S": "schema:v1"})

    def test_kind_mismatch_raises(self):
        a1 = _double_bucket("PV:A", count=2)
        a2 = _bucket("PV:A", _clock(2, start_nanos=T0_NANOS + 10 * PERIOD), dfb.int32_column("PV:A", [1, 2]))
        with self.assertRaises(ValueError) as ctx:
            bc.buckets_to_dataframes([a1, a2])
        message = str(ctx.exception)
        self.assertIn("PV:A", message)
        self.assertIn(str(T0_NANOS), message)
        self.assertIn(str(T0_NANOS + 10 * PERIOD), message)

    def test_structure_mismatch_raises(self):
        cases = {
            "enum": (dfb.enum_column("PV:A", [0, 1], "a:v1"), dfb.enum_column("PV:A", [0, 1], "b:v1")),
            "dims": (
                dfb.double_array_column("PV:A", [[1.0, 2.0, 3.0, 4.0]] * 2),
                dfb.double_array_column("PV:A", [[[1.0, 2.0], [3.0, 4.0]]] * 2),
            ),
            "image": (
                dfb.image_column("PV:A", [b"a", b"b"], 4, 3, 1, "png"),
                dfb.image_column("PV:A", [b"a", b"b"], 4, 3, 1, "jpeg"),
            ),
            "struct": (dfb.struct_column("PV:A", [b"x"] * 2, "s:v1"), dfb.struct_column("PV:A", [b"x"] * 2, "s:v2")),
        }
        for label, (first, second) in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                bc.buckets_to_dataframes(
                    [
                        _bucket("PV:A", _clock(2), first),
                        _bucket("PV:A", _clock(2, start_nanos=T0_NANOS + 10 * PERIOD), second),
                    ]
                )

    def test_serialized_bucket_refused(self):
        bucket = _bucket("PV:S", _clock(2), dfb.serialized_column("PV:S", b"xy", "my-codec"))
        with self.assertRaises(ValueError) as ctx:
            bc.buckets_to_dataframes([bucket])
        self.assertIn("my-codec", str(ctx.exception))

    def test_serialized_bucket_outside_time_range_does_not_block_other_pvs(self):
        a = _double_bucket("PV:A", count=5)
        s = _bucket("PV:S", _clock(2, start_nanos=T0_NANOS + 100 * PERIOD), dfb.serialized_column("PV:S", b"xy", "c"))
        frames = bc.buckets_to_dataframes(
            [a, s], time_range=(from_epoch_nanos(T0_NANOS), from_epoch_nanos(T0_NANOS + 5 * PERIOD))
        )
        self.assertEqual(list(frames), ["PV:A"])

    def test_serialized_bucket_outside_time_range_dropped_from_its_own_pv(self):
        whole = _double_bucket("PV:S", count=2)
        s = _bucket("PV:S", _clock(2, start_nanos=T0_NANOS + 100 * PERIOD), dfb.serialized_column("PV:S", b"xy", "c"))
        frames = bc.buckets_to_dataframes(
            [whole, s], time_range=(from_epoch_nanos(T0_NANOS), from_epoch_nanos(T0_NANOS + 5 * PERIOD))
        )
        self.assertEqual(list(frames["PV:S"]["PV:S"]), [0.0, 1.0])

    def test_serialized_bucket_overlapping_time_range_still_refused(self):
        a = _double_bucket("PV:A", count=5)
        s = _bucket("PV:S", _clock(2), dfb.serialized_column("PV:S", b"xy", "my-codec"))
        with self.assertRaises(ValueError) as ctx:
            bc.buckets_to_dataframes(
                [a, s], time_range=(from_epoch_nanos(T0_NANOS), from_epoch_nanos(T0_NANOS + 5 * PERIOD))
            )
        self.assertIn("my-codec", str(ctx.exception))

    def test_legacy_data_column_buckets_widen_rather_than_raise(self):
        # Documented exception to the consistency check: a DataColumn has no column-level type.
        ints = _bucket("PV:L", _clock(2), dfb.data_column("PV:L", [1, None]))
        doubles = _bucket("PV:L", _clock(2, start_nanos=T0_NANOS + 10 * PERIOD), dfb.data_column("PV:L", [2.5, 3.5]))
        df = bc.buckets_to_dataframes([ints, doubles])["PV:L"]
        self.assertEqual(str(df["PV:L"].dtype), "float64")
        self.assertEqual(df["PV:L"].tolist()[0], 1.0)
        self.assertEqual(df["PV:L"].tolist()[2:], [2.5, 3.5])

    def test_time_range_trims_and_drops_empty_pvs(self):
        a = _double_bucket("PV:A", count=5)
        b = _double_bucket("PV:B", count=2, start_nanos=T0_NANOS + 100 * PERIOD)
        frames = bc.buckets_to_dataframes(
            [a, b], time_range=(from_epoch_nanos(T0_NANOS + PERIOD), from_epoch_nanos(T0_NANOS + 3 * PERIOD))
        )
        self.assertEqual(list(frames), ["PV:A"])
        self.assertEqual(list(frames["PV:A"]["PV:A"]), [1.0, 2.0])
        self.assertEqual(frames["PV:A"].attrs["buckets"][0]["sample_count"], 2)

    def test_time_range_begin_not_before_end_raises(self):
        same = from_epoch_nanos(T0_NANOS)
        with self.assertRaises(ValueError) as ctx:
            bc.buckets_to_dataframes([_double_bucket()], time_range=(same, same))
        self.assertIn("strictly before", str(ctx.exception))

    def test_repeated_timestamps_convert(self):
        nanos = [T0_NANOS, T0_NANOS, T0_NANOS + 1]
        bucket = _bucket("PV:D", _raw_list(nanos), dfb.double_column("PV:D", [1.0, 2.0, 3.0]))
        df = bc.buckets_to_dataframes([bucket])["PV:D"]
        self.assertEqual([ts.value for ts in df.index], nanos)

    def test_empty_input(self):
        self.assertEqual(bc.buckets_to_dataframes([]), {})


@unittest.skipUnless(_HAVE_ANALYSIS, "requires the [analysis] extra (pandas)")
class TestQueryBucketsToDataFrames(unittest.TestCase):
    def setUp(self):
        self.params = QueryParams(
            from_epoch_nanos(T0_NANOS + PERIOD),
            from_epoch_nanos(T0_NANOS + 3 * PERIOD),
            pv_selector=PvQuery.name_list(["PV:A"]),
        )

    def _pages(self, *bucket_lists):
        pages = []
        for buckets in bucket_lists:
            response = query_pb2.QueryBucketsResponse()
            response.bucketQueryResult.dataBuckets.extend(buckets)
            pages.append(QueryBucketsApiResult(is_error=False, message="", response=response))
        client = Mock()
        client.iter_query_buckets = Mock(return_value=iter(pages))
        return client

    def test_assembles_a_pv_split_across_pages(self):
        a1 = _double_bucket("PV:A", count=2)
        a2 = _double_bucket("PV:A", count=2, start_nanos=T0_NANOS + 2 * PERIOD, values=[2.0, 3.0])
        frames = bc.query_buckets_to_dataframes(self._pages([a1], [a2]), self.params)
        self.assertEqual(list(frames["PV:A"]["PV:A"]), [0.0, 1.0, 2.0, 3.0])

    def test_trim_uses_the_params_range(self):
        frames = bc.query_buckets_to_dataframes(self._pages([_double_bucket("PV:A")]), self.params, trim=True)
        self.assertEqual(list(frames["PV:A"]["PV:A"]), [1.0, 2.0])

    def test_untrimmed_by_default(self):
        frames = bc.query_buckets_to_dataframes(self._pages([_double_bucket("PV:A")]), self.params)
        self.assertEqual(len(frames["PV:A"]), 5)

    def test_max_buckets(self):
        client = self._pages([_double_bucket("PV:A")], [_double_bucket("PV:B")])
        with self.assertRaises(ValueError) as ctx:
            bc.query_buckets_to_dataframes(client, self.params, max_buckets=1)
        self.assertIn("max_buckets=1", str(ctx.exception))
        client = self._pages([_double_bucket("PV:A")], [_double_bucket("PV:B")])
        self.assertEqual(len(bc.query_buckets_to_dataframes(client, self.params, max_buckets=2)), 2)

    def test_result_to_dataframes_delegates(self):
        response = query_pb2.QueryBucketsResponse()
        response.bucketQueryResult.dataBuckets.extend([_double_bucket("PV:A")])
        result = QueryBucketsApiResult(is_error=False, message="", response=response)
        frames = result.to_dataframes(time_range=(from_epoch_nanos(T0_NANOS), from_epoch_nanos(T0_NANOS + PERIOD)))
        self.assertEqual(list(frames["PV:A"]["PV:A"]), [0.0])


if __name__ == "__main__":
    unittest.main()
