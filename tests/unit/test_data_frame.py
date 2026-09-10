import os
import sys
import unittest
from datetime import datetime, timezone

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client import data_frame as dfb
from dp_python_lib.grpc import common_pb2

try:
    import numpy as np

    _HAVE_NUMPY = True
except ImportError:
    _HAVE_NUMPY = False

T0 = datetime(2026, 7, 14, 18, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 7, 14, 19, 0, 0, tzinfo=timezone.utc)


def _axis(count=3):
    """A SamplingClock axis of the given length, at 1 Hz."""
    return dfb.sampling_clock(T0, 1_000_000_000, count)


class TestSamplingClock(unittest.TestCase):
    """The axis builders relocated here from sample_status_client in Phase 2; behavior is unchanged."""

    def test_builds_clock(self):
        axis = dfb.sampling_clock(T0, 1_000_000, 3)
        self.assertEqual(axis.samplingClock.startTime.epochSeconds, int(T0.timestamp()))
        self.assertEqual(axis.samplingClock.periodNanos, 1_000_000)
        self.assertEqual(axis.samplingClock.count, 3)

    def test_rejects_non_positive_period(self):
        for period in (0, -1):
            with self.assertRaises(ValueError):
                dfb.sampling_clock(T0, period, 3)

    def test_rejects_count_below_one(self):
        with self.assertRaises(ValueError):
            dfb.sampling_clock(T0, 1_000_000, 0)

    def test_still_importable_from_sample_status_client(self):
        # The relocation must not break existing imports.
        from dp_python_lib.client.sample_status_client import sampling_clock, timestamp_list

        self.assertIs(sampling_clock, dfb.sampling_clock)
        self.assertIs(timestamp_list, dfb.timestamp_list)


class TestTimestampList(unittest.TestCase):
    def test_builds_list(self):
        axis = dfb.timestamp_list([100, 200, 300])
        self.assertEqual([t.epochSeconds for t in axis.timestampList.timestamps], [100, 200, 300])

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            dfb.timestamp_list([])

    def test_rejects_non_increasing(self):
        for values in ([100, 300, 200], [100, 100]):
            with self.assertRaises(ValueError):
                dfb.timestamp_list(values)

    def test_rejects_non_increasing_at_nanosecond_precision(self):
        with self.assertRaises(ValueError):
            dfb.timestamp_list([100.000_002, 100.000_001])


class TestTimestampCount(unittest.TestCase):
    def test_counts_both_axis_forms(self):
        self.assertEqual(dfb.timestamp_count(dfb.sampling_clock(T0, 1_000, 7)), 7)
        self.assertEqual(dfb.timestamp_count(dfb.timestamp_list([1, 2])), 2)

    def test_rejects_unset_axis(self):
        with self.assertRaises(ValueError):
            dfb.timestamp_count(common_pb2.DataTimestamps())

    def test_rejects_hand_built_empty_axes(self):
        # The builders make these unreachable, but a hand-built axis can still carry them.
        empty_clock = common_pb2.DataTimestamps()
        empty_clock.samplingClock.count = 0
        empty_clock.samplingClock.periodNanos = 1
        with self.assertRaises(ValueError):
            dfb.timestamp_count(empty_clock)

        empty_list = common_pb2.DataTimestamps()
        empty_list.timestampList.SetInParent()
        with self.assertRaises(ValueError):
            dfb.timestamp_count(empty_list)


class TestScalarColumnBuilders(unittest.TestCase):
    """Each typed scalar builder produces the right message with the right values."""

    def test_each_builder_and_field(self):
        cases = [
            (dfb.double_column("d", [1.0, 2.0]), common_pb2.DoubleColumn, [1.0, 2.0]),
            (dfb.int64_column("i64", [1, 2]), common_pb2.Int64Column, [1, 2]),
            (dfb.int32_column("i32", [1, 2]), common_pb2.Int32Column, [1, 2]),
            (dfb.bool_column("b", [True, False]), common_pb2.BoolColumn, [True, False]),
            (dfb.string_column("s", ["a", "b"]), common_pb2.StringColumn, ["a", "b"]),
        ]
        for column, expected_type, expected_values in cases:
            with self.subTest(column=column.name):
                self.assertIsInstance(column, expected_type)
                self.assertEqual(list(column.values), expected_values)

    def test_float_column_is_float32(self):
        column = dfb.float_column("f", [1.5, 2.5])
        self.assertIsInstance(column, common_pb2.FloatColumn)
        self.assertEqual(list(column.values), [1.5, 2.5])

    def test_enum_column_carries_enum_id(self):
        column = dfb.enum_column("state", [0, 1, 2], enum_id="beam-state")
        self.assertEqual(list(column.values), [0, 1, 2])
        self.assertEqual(column.enumId, "beam-state")

    def test_enum_column_requires_enum_id(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.enum_column("state", [0], enum_id="")
        self.assertIn("enum_id", str(ctx.exception))

    def test_metadata_is_attached(self):
        metadata = dfb.column_metadata(tags=["derived"])
        column = dfb.double_column("d", [1.0], metadata=metadata)
        self.assertEqual(list(column.metadata.tags), ["derived"])

    def test_rejects_empty_name_and_values(self):
        for builder in (dfb.double_column, dfb.float_column, dfb.int64_column, dfb.int32_column, dfb.string_column):
            with self.subTest(builder=builder.__name__):
                with self.assertRaises(ValueError):
                    builder("", [1])
                with self.assertRaises(ValueError):
                    builder("name", [])

    def test_empty_values_error_names_the_column(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.double_column("x_rms", [])
        self.assertIn("x_rms", str(ctx.exception))


class TestDataColumnEscapeHatch(unittest.TestCase):
    """The legacy DataColumn is the only way to express a gap on a shared axis."""

    def test_none_becomes_an_unset_oneof(self):
        column = dfb.data_column("sparse", [1.0, None, 3.0])
        arms = [v.WhichOneof("value") for v in column.dataValues]
        self.assertEqual(arms, ["doubleValue", None, "doubleValue"])

    def test_maps_each_python_type(self):
        column = dfb.data_column("mixed", [True, 7, 1.5, "s", b"bytes"])
        arms = [v.WhichOneof("value") for v in column.dataValues]
        self.assertEqual(arms, ["booleanValue", "longValue", "doubleValue", "stringValue", "byteArrayValue"])

    def test_bool_is_checked_before_int(self):
        # bool is a subclass of int, so an int-first check would store True as longValue 1.
        column = dfb.data_column("flags", [True, False])
        self.assertEqual([v.WhichOneof("value") for v in column.dataValues], ["booleanValue", "booleanValue"])
        self.assertEqual([v.booleanValue for v in column.dataValues], [True, False])

    def test_accepts_prebuilt_data_value(self):
        prebuilt = common_pb2.DataValue()
        prebuilt.uintValue = 42
        column = dfb.data_column("u", [prebuilt])
        self.assertEqual(column.dataValues[0].WhichOneof("value"), "uintValue")
        self.assertEqual(column.dataValues[0].uintValue, 42)

    @unittest.skipUnless(_HAVE_NUMPY, "requires the [analysis] extra (numpy)")
    def test_maps_numpy_scalars_like_their_python_counterparts(self):
        # np.float64 subclasses float, but np.int64 does not subclass int and np.bool_ does not subclass bool.
        # Mapping by exact Python type would accept the first and reject the other two -- an arbitrary split for a
        # caller coming from pandas or NumPy.
        column = dfb.data_column("np", [np.float64(1.5), np.int64(2), np.int32(3), np.bool_(True), np.float32(0.5)])
        arms = [v.WhichOneof("value") for v in column.dataValues]
        self.assertEqual(arms, ["doubleValue", "longValue", "longValue", "booleanValue", "doubleValue"])
        self.assertEqual(column.dataValues[1].longValue, 2)
        self.assertIs(column.dataValues[3].booleanValue, True)

    @unittest.skipUnless(_HAVE_NUMPY, "requires the [analysis] extra (numpy)")
    def test_numpy_bool_is_checked_before_the_integer_branch(self):
        # np.bool_ is not Integral, but this pins the ordering the way test_bool_is_checked_before_int does.
        column = dfb.data_column("flags", [np.bool_(True), np.bool_(False)])
        self.assertEqual([v.WhichOneof("value") for v in column.dataValues], ["booleanValue", "booleanValue"])
        self.assertEqual([v.booleanValue for v in column.dataValues], [True, False])

    def test_rejects_unmappable_type(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.data_column("c", [1 + 2j])
        message = str(ctx.exception)
        self.assertIn("complex", message)
        self.assertIn("index 0", message)

    def test_rejects_whitespace_only_name(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.data_column("   ", [1])
        self.assertIn("non-blank", str(ctx.exception))

    def test_rejects_empty_name_and_values(self):
        with self.assertRaises(ValueError):
            dfb.data_column("", [1])
        with self.assertRaises(ValueError):
            dfb.data_column("name", [])


class TestProvenanceHelpers(unittest.TestCase):
    def test_pv_source_sets_the_pv_name_arm(self):
        source = dfb.pv_source("BPMS:GUNB:314:X")
        self.assertEqual(source.WhichOneof("origin"), "pvName")
        self.assertEqual(source.pvName, "BPMS:GUNB:314:X")
        self.assertFalse(source.HasField("timeRange"))

    def test_pv_source_with_time_range(self):
        source = dfb.pv_source("A:1", (T0, T1))
        self.assertTrue(source.HasField("timeRange"))
        self.assertEqual(source.timeRange.beginTime.epochSeconds, int(T0.timestamp()))
        self.assertEqual(source.timeRange.endTime.epochSeconds, int(T1.timestamp()))

    def test_calculations_source_sets_the_calculations_arm(self):
        source = dfb.calculations_source("calc-1", "f1", "x_rms")
        self.assertEqual(source.WhichOneof("origin"), "calculationsColumn")
        self.assertEqual(source.calculationsColumn.calculationsId, "calc-1")
        self.assertEqual(source.calculationsColumn.frameName, "f1")
        self.assertEqual(source.calculationsColumn.columnName, "x_rms")

    def test_sources_reject_empty_identifiers(self):
        with self.assertRaises(ValueError):
            dfb.pv_source("")
        with self.assertRaises(ValueError):
            dfb.calculations_source("", "f", "c")
        with self.assertRaises(ValueError):
            dfb.calculations_source("calc", "", "c")
        with self.assertRaises(ValueError):
            dfb.calculations_source("calc", "f", "")

    def test_sources_reject_reversed_time_range(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.pv_source("A:1", (T1, T0))
        self.assertIn("strictly before", str(ctx.exception))

    def test_provenance_fields(self):
        result = dfb.provenance(
            source="analysis-rig",
            process="1 Hz RMS",
            derived_from=[dfb.pv_source("A:1"), dfb.calculations_source("c", "f", "x")],
        )
        self.assertEqual(result.source, "analysis-rig")
        self.assertEqual(result.process, "1 Hz RMS")
        self.assertEqual(len(result.derivedFrom), 2)

    def test_provenance_omits_unset_fields(self):
        result = dfb.provenance()
        self.assertEqual(result.source, "")
        self.assertEqual(result.process, "")
        self.assertEqual(len(result.derivedFrom), 0)

    def test_column_metadata_fields(self):
        metadata = dfb.column_metadata(
            tags=["derived", "reviewed"],
            attributes={"unit": "mm"},
            provenance=dfb.provenance(process="p"),
        )
        self.assertEqual(list(metadata.tags), ["derived", "reviewed"])
        self.assertEqual({(a.name, a.value) for a in metadata.attributes}, {("unit", "mm")})
        self.assertEqual(metadata.provenance.process, "p")

    def test_column_metadata_empty(self):
        metadata = dfb.column_metadata()
        self.assertEqual(list(metadata.tags), [])
        self.assertEqual(len(metadata.attributes), 0)
        self.assertFalse(metadata.HasField("provenance"))


class TestDataFrameAssembly(unittest.TestCase):
    """data_frame() routes columns by type and enforces the server's shape rules client-side."""

    def test_routes_each_column_type_to_its_field(self):
        frame = dfb.data_frame(
            _axis(2),
            [
                dfb.double_column("d", [1.0, 2.0]),
                dfb.float_column("f", [1.0, 2.0]),
                dfb.int64_column("i64", [1, 2]),
                dfb.int32_column("i32", [1, 2]),
                dfb.bool_column("b", [True, False]),
                dfb.string_column("s", ["a", "b"]),
                dfb.enum_column("e", [0, 1], enum_id="enum-1"),
                dfb.data_column("legacy", [1.0, None]),
            ],
        )
        self.assertEqual([c.name for c in frame.doubleColumns], ["d"])
        self.assertEqual([c.name for c in frame.floatColumns], ["f"])
        self.assertEqual([c.name for c in frame.int64Columns], ["i64"])
        self.assertEqual([c.name for c in frame.int32Columns], ["i32"])
        self.assertEqual([c.name for c in frame.boolColumns], ["b"])
        self.assertEqual([c.name for c in frame.stringColumns], ["s"])
        self.assertEqual([c.name for c in frame.enumColumns], ["e"])
        self.assertEqual([c.name for c in frame.dataColumns], ["legacy"])

    def test_copies_the_axis(self):
        axis = _axis(3)
        frame = dfb.data_frame(axis, [dfb.double_column("d", [1.0, 2.0, 3.0])])
        self.assertEqual(frame.dataTimestamps.samplingClock.count, 3)

    def test_rejects_empty_column_list(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(1), [])
        self.assertIn("at least one column", str(ctx.exception))

    def test_rejects_count_mismatch_naming_the_column(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(3), [dfb.double_column("x_rms", [1.0, 2.0])])
        message = str(ctx.exception)
        self.assertIn("x_rms", message)
        self.assertIn("2 values", message)
        self.assertIn("3 timestamps", message)

    def test_rejects_duplicate_names_across_types(self):
        # Uniqueness is across ALL column types, not just within one.
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(1), [dfb.double_column("dup", [1.0]), dfb.string_column("dup", ["a"])])
        self.assertIn("dup", str(ctx.exception))

    def test_rejects_unnamed_prebuilt_column(self):
        unnamed = common_pb2.DoubleColumn()
        unnamed.values[:] = [1.0]
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(1), [unnamed])
        self.assertIn("index 0", str(ctx.exception))

    def test_rejects_unsupported_column_type(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(1), ["not a column"])
        self.assertIn("unsupported column type", str(ctx.exception))

    def test_rejects_empty_axis(self):
        with self.assertRaises(ValueError):
            dfb.data_frame(common_pb2.DataTimestamps(), [dfb.double_column("d", [1.0])])

    def test_accepts_prebuilt_array_column(self):
        # Array builders are #17's; a hand-built one passes through, with dims giving the sample count.
        column = common_pb2.DoubleArrayColumn()
        column.name = "waveform"
        column.dimensions.dims.extend([4])
        column.values[:] = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]  # 2 samples x 4
        frame = dfb.data_frame(_axis(2), [column])
        self.assertEqual([c.name for c in frame.doubleArrayColumns], ["waveform"])

    def test_array_column_count_uses_dims_product(self):
        column = common_pb2.DoubleArrayColumn()
        column.name = "waveform"
        column.dimensions.dims.extend([4])
        column.values[:] = [1.0] * 8  # 2 samples, but the axis says 3
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(3), [column])
        self.assertIn("2 values", str(ctx.exception))

    def test_accepts_prebuilt_image_column(self):
        # ImageColumn keeps one payload per sample in `images`; it has no `values` field at all, so a generic
        # len(column.values) count raises AttributeError on a column type data_frame() claims to support.
        column = common_pb2.ImageColumn()
        column.name = "camera"
        column.images.extend([b"frame-0", b"frame-1"])
        frame = dfb.data_frame(_axis(2), [column])
        self.assertEqual([c.name for c in frame.imageColumns], ["camera"])

    def test_image_column_count_is_validated_against_the_axis(self):
        column = common_pb2.ImageColumn()
        column.name = "camera"
        column.images.extend([b"only-one"])
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(3), [column])
        self.assertIn("1 values", str(ctx.exception))

    def test_rejects_whitespace_only_column_name(self):
        # The rule is a non-blank name; "  " is empty of content while passing a bare falsiness check.
        for blank in ("  ", "\t", "\n"):
            with self.subTest(name=blank):
                column = common_pb2.DoubleColumn()
                column.name = blank
                column.values[:] = [1.0]
                with self.assertRaises(ValueError) as ctx:
                    dfb.data_frame(_axis(1), [column])
                self.assertIn("non-blank", str(ctx.exception))

    def test_array_column_with_ragged_values_is_rejected(self):
        # 5 values with a per-sample size of 2 is not a whole number of samples.  Floor division would round it to
        # a passing count of 2 and build a frame that data_frame_conversions then refuses to read back.
        column = common_pb2.DoubleArrayColumn()
        column.name = "waveform"
        column.dimensions.dims.extend([2])
        column.values[:] = [1.0, 2.0, 3.0, 4.0, 5.0]
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(2), [column])
        message = str(ctx.exception)
        self.assertIn("whole multiple", message)
        self.assertIn("waveform", message)

    def test_array_column_accepts_multidimensional_dims(self):
        column = common_pb2.DoubleArrayColumn()
        column.name = "image"
        column.dimensions.dims.extend([2, 3])
        column.values[:] = [float(i) for i in range(12)]  # 2 samples x (2 x 3)
        frame = dfb.data_frame(_axis(2), [column])
        self.assertEqual([c.name for c in frame.doubleArrayColumns], ["image"])

    def test_array_column_without_dims_is_rejected(self):
        column = common_pb2.DoubleArrayColumn()
        column.name = "waveform"
        column.values[:] = [1.0, 2.0]
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(2), [column])
        self.assertIn("dimensions", str(ctx.exception))

    def test_serialized_column_gets_name_checks_only(self):
        # Serialized payloads are opaque, so the server checks names only; this must not demand a count match.
        column = common_pb2.SerializedDataColumn()
        column.name = "serialized"
        column.encoding = "arrow"
        column.payload = b"opaque"
        frame = dfb.data_frame(_axis(3), [column])
        self.assertEqual([c.name for c in frame.serializedDataColumns], ["serialized"])

    def test_serialized_column_still_needs_a_unique_name(self):
        column = common_pb2.SerializedDataColumn()
        column.name = "dup"
        with self.assertRaises(ValueError):
            dfb.data_frame(_axis(1), [dfb.double_column("dup", [1.0]), column])


if __name__ == "__main__":
    unittest.main()
