import os
import sys
import unittest
from datetime import datetime, timezone

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client import data_frame as dfb
from dp_python_lib.grpc import common_pb2, ingestion_pb2

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


def _hand_built_image_column(images, name="camera"):
    """An ImageColumn built from the proto directly, with a valid descriptor."""
    column = common_pb2.ImageColumn()
    column.name = name
    column.imageDescriptor.width = 2
    column.imageDescriptor.height = 2
    column.imageDescriptor.channels = 1
    column.imageDescriptor.encoding = "raw-u8"
    column.images.extend(images)
    return column


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

    def test_rejects_out_of_order_prebuilt_timestamp_list(self):
        # timestamp_list() enforces strict ordering, but a hand-built axis bypasses that builder.  Two samples
        # claiming one instant is inexpressible in the identity model, and data_frame_from_pandas() rejects the
        # index such an axis produces -- so accepting it here would build an un-round-trippable frame.
        for label, seconds in (("decreasing", [2, 1]), ("duplicate", [1, 1])):
            with self.subTest(case=label):
                axis = common_pb2.DataTimestamps()
                for second in seconds:
                    entry = axis.timestampList.timestamps.add()
                    entry.epochSeconds = 1_700_000_000 + second
                with self.assertRaises(ValueError) as ctx:
                    dfb.data_frame(axis, [dfb.double_column("d", [1.0, 2.0])])
                self.assertIn("strictly increasing", str(ctx.exception))

    def test_rejects_prebuilt_timestamp_list_duplicated_at_nanosecond_precision(self):
        axis = common_pb2.DataTimestamps()
        for _ in range(2):
            entry = axis.timestampList.timestamps.add()
            entry.epochSeconds = 1_700_000_000
            entry.nanoseconds = 5
        with self.assertRaises(ValueError):
            dfb.data_frame(axis, [dfb.double_column("d", [1.0, 2.0])])

    def test_accepts_strictly_increasing_prebuilt_timestamp_list(self):
        axis = common_pb2.DataTimestamps()
        for second in (1, 2):
            entry = axis.timestampList.timestamps.add()
            entry.epochSeconds = 1_700_000_000 + second
        frame = dfb.data_frame(axis, [dfb.double_column("d", [1.0, 2.0])])
        self.assertEqual(len(frame.dataTimestamps.timestampList.timestamps), 2)

    def test_rejects_zero_period_sampling_clock(self):
        # sampling_clock() makes this unreachable, but a hand-built axis bypasses it.  expand_data_timestamps()
        # rejects a non-positive period on the read side, so accepting it here would build an unreadable frame --
        # and every sample after the first would carry the first one's timestamp.
        axis = common_pb2.DataTimestamps()
        axis.samplingClock.startTime.epochSeconds = 1_700_000_000
        axis.samplingClock.periodNanos = 0
        axis.samplingClock.count = 3
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(axis, [dfb.double_column("d", [1.0, 2.0, 3.0])])
        self.assertIn("periodNanos > 0", str(ctx.exception))

    def test_rejects_empty_axis(self):
        with self.assertRaises(ValueError):
            dfb.data_frame(common_pb2.DataTimestamps(), [dfb.double_column("d", [1.0])])

    def test_accepts_prebuilt_array_column(self):
        # A hand-built array column passes through alongside the builders' output, with dims giving the count.
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
        column = _hand_built_image_column([b"frame-0", b"frame-1"])
        frame = dfb.data_frame(_axis(2), [column])
        self.assertEqual([c.name for c in frame.imageColumns], ["camera"])

    def test_image_column_count_is_validated_against_the_axis(self):
        # A valid descriptor, so the count is what fails -- not the descriptor check that runs before it.
        column = _hand_built_image_column([b"only-one"])
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
        # Given an encoding, so the duplicate name is the only thing wrong with it.
        column = common_pb2.SerializedDataColumn()
        column.name = "dup"
        column.encoding = "arrow"
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(1), [dfb.double_column("dup", [1.0]), column])
        self.assertIn("unique column names", str(ctx.exception))


class TestStructuralColumnChecks(unittest.TestCase):
    """
    Each column kind's structural fields, checked on HAND-BUILT columns (#17, T7).

    The builders enforce these rules too, but a column built from the proto bypasses every builder -- the recurring
    #6 defect shape -- so the checks that matter are the ones data_frame() applies to any column.
    """

    def test_rejects_enum_column_without_enum_id(self):
        for enum_id in ("", "  "):
            with self.subTest(enum_id=enum_id):
                column = common_pb2.EnumColumn()
                column.name = "state"
                column.enumId = enum_id
                column.values[:] = [0, 1]
                with self.assertRaises(ValueError) as ctx:
                    dfb.data_frame(_axis(2), [column])
                self.assertIn("enumId", str(ctx.exception))

    def test_enum_builder_rejects_blank_enum_id(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.enum_column("state", [0], enum_id="  ")
        self.assertIn("non-blank enum_id", str(ctx.exception))

    def test_rejects_struct_column_without_schema_id(self):
        column = common_pb2.StructColumn()
        column.name = "bpm"
        column.values.extend([b"a", b"b"])
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(2), [column])
        self.assertIn("schemaId", str(ctx.exception))

    def test_rejects_serialized_column_without_encoding(self):
        for encoding in ("", "\t"):
            with self.subTest(encoding=encoding):
                column = common_pb2.SerializedDataColumn()
                column.name = "blob"
                column.encoding = encoding
                with self.assertRaises(ValueError) as ctx:
                    dfb.data_frame(_axis(1), [dfb.double_column("d", [1.0]), column])
                self.assertIn("encoding", str(ctx.exception))

    def test_rejects_image_column_without_descriptor(self):
        column = common_pb2.ImageColumn()
        column.name = "camera"
        column.images.extend([b"x"])
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(1), [column])
        self.assertIn("imageDescriptor", str(ctx.exception))

    def test_rejects_image_descriptor_with_a_zero_dimension(self):
        for field in ("width", "height", "channels"):
            with self.subTest(field=field):
                column = _hand_built_image_column([b"x"])
                setattr(column.imageDescriptor, field, 0)
                with self.assertRaises(ValueError) as ctx:
                    dfb.data_frame(_axis(1), [column])
                self.assertIn(f"imageDescriptor.{field} > 0", str(ctx.exception))

    def test_rejects_image_descriptor_with_blank_encoding(self):
        column = _hand_built_image_column([b"x"])
        column.imageDescriptor.encoding = " "
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(1), [column])
        self.assertIn("imageDescriptor.encoding", str(ctx.exception))

    def test_rejects_array_column_with_more_than_three_dims(self):
        column = common_pb2.DoubleArrayColumn()
        column.name = "tensor"
        column.dimensions.dims.extend([1, 1, 1, 2])
        column.values[:] = [1.0, 2.0]
        with self.assertRaises(ValueError) as ctx:
            dfb.data_frame(_axis(1), [column])
        self.assertIn("1 to 3", str(ctx.exception))

    def test_accepts_three_dims(self):
        column = common_pb2.DoubleArrayColumn()
        column.name = "tensor"
        column.dimensions.dims.extend([1, 1, 2])
        column.values[:] = [1.0, 2.0]
        frame = dfb.data_frame(_axis(1), [column])
        self.assertEqual(len(frame.doubleArrayColumns), 1)


class TestValidateDataFrame(unittest.TestCase):
    """validate_data_frame() applies data_frame()'s checks to a frame assembled any other way."""

    def test_accepts_a_frame_from_data_frame(self):
        frame = dfb.data_frame(_axis(2), [dfb.double_column("d", [1.0, 2.0]), dfb.enum_column("e", [0, 1], "E")])
        dfb.validate_data_frame(frame)

    def test_rejects_a_hand_built_frame_with_a_hand_built_enum_lacking_its_id(self):
        # The T7 point at frame level: nothing about a hand-built frame goes through data_frame().
        frame = common_pb2.DataFrame()
        frame.dataTimestamps.CopyFrom(_axis(2))
        column = frame.enumColumns.add()
        column.name = "state"
        column.values[:] = [0, 1]
        with self.assertRaises(ValueError) as ctx:
            dfb.validate_data_frame(frame)
        self.assertIn("enumId", str(ctx.exception))

    def test_rejects_a_frame_with_no_columns(self):
        frame = common_pb2.DataFrame()
        frame.dataTimestamps.CopyFrom(_axis(2))
        with self.assertRaises(ValueError) as ctx:
            dfb.validate_data_frame(frame)
        self.assertIn("at least one column", str(ctx.exception))

    def test_rejects_a_frame_with_no_axis(self):
        frame = common_pb2.DataFrame()
        frame.doubleColumns.add(name="d").values[:] = [1.0]
        with self.assertRaises(ValueError):
            dfb.validate_data_frame(frame)

    def test_rejects_duplicate_names_across_column_types(self):
        frame = common_pb2.DataFrame()
        frame.dataTimestamps.CopyFrom(_axis(1))
        frame.doubleColumns.add(name="x").values[:] = [1.0]
        frame.int32Columns.add(name="x").values[:] = [1]
        with self.assertRaises(ValueError) as ctx:
            dfb.validate_data_frame(frame)
        self.assertIn("unique column names", str(ctx.exception))

    def test_rejects_a_count_mismatch(self):
        frame = common_pb2.DataFrame()
        frame.dataTimestamps.CopyFrom(_axis(3))
        frame.doubleColumns.add(name="d").values[:] = [1.0]
        with self.assertRaises(ValueError) as ctx:
            dfb.validate_data_frame(frame)
        self.assertIn("1 values", str(ctx.exception))


class TestArrayColumnBuilders(unittest.TestCase):
    def test_one_dimensional_samples_infer_their_dims(self):
        column = dfb.double_array_column("wf", [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
        self.assertEqual(list(column.dimensions.dims), [3])
        self.assertEqual(list(column.values), [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

    def test_multidimensional_samples_flatten_row_major(self):
        column = dfb.int32_array_column("m", [[[1, 2, 3], [4, 5, 6]]])
        self.assertEqual(list(column.dimensions.dims), [2, 3])
        self.assertEqual(list(column.values), [1, 2, 3, 4, 5, 6])

    def test_three_dimensions_are_accepted_and_four_are_not(self):
        column = dfb.int64_array_column("t", [[[[1, 2]]]])
        self.assertEqual(list(column.dimensions.dims), [1, 1, 2])
        with self.assertRaises(ValueError) as ctx:
            dfb.int64_array_column("t", [[[[[1, 2]]]]])
        self.assertIn("1 to 3", str(ctx.exception))

    def test_each_array_type_builds_its_own_message(self):
        cases = (
            (dfb.double_array_column, common_pb2.DoubleArrayColumn, [[1.0, 2.0]]),
            (dfb.float_array_column, common_pb2.FloatArrayColumn, [[1.5, 2.5]]),
            (dfb.int32_array_column, common_pb2.Int32ArrayColumn, [[1, 2]]),
            (dfb.int64_array_column, common_pb2.Int64ArrayColumn, [[1, 2]]),
            (dfb.bool_array_column, common_pb2.BoolArrayColumn, [[True, False]]),
        )
        for builder, message_type, samples in cases:
            with self.subTest(builder=builder.__name__):
                column = builder("c", samples)
                self.assertIsInstance(column, message_type)
                self.assertEqual(list(column.values), samples[0])

    def test_rejects_a_ragged_sample(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.double_array_column("wf", [[[1.0, 2.0], [3.0]]])
        message = str(ctx.exception)
        self.assertIn("sample 0", message)
        self.assertIn("rectangular", message)

    def test_rejects_samples_of_different_shapes(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.double_array_column("wf", [[1.0, 2.0], [1.0, 2.0, 3.0]])
        message = str(ctx.exception)
        self.assertIn("sample 1", message)
        self.assertIn("same shape", message)

    def test_rejects_scalar_samples(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.double_array_column("wf", [1.0, 2.0])
        self.assertIn("scalar samples", str(ctx.exception))

    def test_rejects_no_samples_and_empty_samples(self):
        with self.assertRaises(ValueError):
            dfb.double_array_column("wf", [])
        with self.assertRaises(ValueError) as ctx:
            dfb.double_array_column("wf", [[], []])
        self.assertIn("> 0", str(ctx.exception))

    def test_rejects_blank_name(self):
        with self.assertRaises(ValueError):
            dfb.double_array_column(" ", [[1.0]])

    def test_explicit_dims_shape_flat_samples(self):
        column = dfb.double_array_column("img", [[1.0, 2.0, 3.0, 4.0]], dims=[2, 2])
        self.assertEqual(list(column.dimensions.dims), [2, 2])
        self.assertEqual(list(column.values), [1.0, 2.0, 3.0, 4.0])

    def test_explicit_dims_may_restate_the_shape(self):
        column = dfb.double_array_column("img", [[[1.0, 2.0], [3.0, 4.0]]], dims=[2, 2])
        self.assertEqual(list(column.dimensions.dims), [2, 2])

    def test_explicit_dims_that_do_not_describe_the_samples_are_rejected(self):
        for samples, dims in (
            ([[1.0, 2.0, 3.0, 4.0]], [3]),  # wrong size for flat samples
            ([[[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]], [3, 2]),  # would reinterpret a 2x3 layout
        ):
            with self.subTest(dims=dims):
                with self.assertRaises(ValueError) as ctx:
                    dfb.double_array_column("img", samples, dims=dims)
                self.assertIn("does not describe", str(ctx.exception))

    def test_rejects_a_value_the_type_cannot_hold(self):
        for samples in ([[1.5]], [[2**40]]):
            with self.subTest(samples=samples):
                with self.assertRaises(ValueError) as ctx:
                    dfb.int32_array_column("i", samples)
                self.assertIn("column 'i'", str(ctx.exception))

    def test_metadata_is_carried(self):
        metadata = dfb.column_metadata(tags=["raw"])
        column = dfb.double_array_column("wf", [[1.0]], metadata=metadata)
        self.assertEqual(list(column.metadata.tags), ["raw"])

    def test_built_column_passes_data_frame(self):
        column = dfb.double_array_column("wf", [[1.0, 2.0], [3.0, 4.0]])
        frame = dfb.data_frame(_axis(2), [column])
        self.assertEqual(len(frame.doubleArrayColumns), 1)

    @unittest.skipUnless(_HAVE_NUMPY, "numpy not installed ([analysis] extra)")
    def test_a_two_dimensional_numpy_array_is_one_sample_per_row(self):
        samples = np.arange(6, dtype=np.float64).reshape(2, 3)
        column = dfb.double_array_column("wf", samples)
        self.assertEqual(list(column.dimensions.dims), [3])
        self.assertEqual(list(column.values), [0.0, 1.0, 2.0, 3.0, 4.0, 5.0])

    @unittest.skipUnless(_HAVE_NUMPY, "numpy not installed ([analysis] extra)")
    def test_numpy_samples_keep_their_shape_and_flatten_row_major(self):
        samples = [np.array([[1, 2, 3], [4, 5, 6]], dtype=np.int32)]
        column = dfb.int32_array_column("m", samples)
        self.assertEqual(list(column.dimensions.dims), [2, 3])
        self.assertEqual(list(column.values), [1, 2, 3, 4, 5, 6])

    @unittest.skipUnless(_HAVE_NUMPY, "numpy not installed ([analysis] extra)")
    def test_numpy_bools_are_accepted(self):
        column = dfb.bool_array_column("flags", [np.array([True, False])])
        self.assertEqual(list(column.values), [True, False])

    @unittest.skipUnless(_HAVE_NUMPY, "numpy not installed ([analysis] extra)")
    def test_numpy_samples_of_different_shapes_are_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.double_array_column("wf", [np.zeros(2), np.zeros(3)])
        self.assertIn("same shape", str(ctx.exception))


class TestImageStructSerializedBuilders(unittest.TestCase):
    def test_image_column(self):
        column = dfb.image_column("cam", [b"a", b"b"], width=4, height=3, channels=1, encoding="png")
        descriptor = column.imageDescriptor
        self.assertEqual(
            (descriptor.width, descriptor.height, descriptor.channels, descriptor.encoding), (4, 3, 1, "png")
        )
        self.assertEqual(list(column.images), [b"a", b"b"])
        dfb.data_frame(_axis(2), [column])

    def test_image_column_rejections(self):
        good = {"width": 4, "height": 3, "channels": 1, "encoding": "png"}
        for label, overrides, images in (
            ("no images", {}, []),
            ("zero width", {"width": 0}, [b"a"]),
            ("zero height", {"height": 0}, [b"a"]),
            ("zero channels", {"channels": 0}, [b"a"]),
            ("blank encoding", {"encoding": " "}, [b"a"]),
        ):
            with self.subTest(case=label), self.assertRaises(ValueError):
                dfb.image_column("cam", images, **{**good, **overrides})

    def test_struct_column(self):
        column = dfb.struct_column("bpm", [b"s0", b"s1"], schema_id="beam_position:v3")
        self.assertEqual(column.schemaId, "beam_position:v3")
        self.assertEqual(list(column.values), [b"s0", b"s1"])
        dfb.data_frame(_axis(2), [column])

    def test_struct_column_rejections(self):
        with self.assertRaises(ValueError):
            dfb.struct_column("bpm", [b"s0"], schema_id=" ")
        with self.assertRaises(ValueError):
            dfb.struct_column("bpm", [], schema_id="s")

    def test_serialized_column(self):
        column = dfb.serialized_column("blob", b"payload", encoding="proto:Image")
        self.assertEqual((column.encoding, column.payload), ("proto:Image", b"payload"))
        dfb.data_frame(_axis(3), [dfb.double_column("d", [1.0, 2.0, 3.0]), column])

    def test_serialized_column_rejections(self):
        with self.assertRaises(ValueError):
            dfb.serialized_column("blob", b"p", encoding="")
        with self.assertRaises(ValueError):
            dfb.serialized_column("", b"p", encoding="e")


# A present-day start with a nanosecond fraction, and a period that does not divide a second: a float anywhere in
# the chunk start-time arithmetic would show up here.
_ODD_START = common_pb2.Timestamp(epochSeconds=int(T0.timestamp()), nanoseconds=123_456_789)
_ODD_PERIOD = 333_333_333


def _nanos(timestamp):
    return timestamp.epochSeconds * 1_000_000_000 + timestamp.nanoseconds


def _every_kind_frame(count):
    """A frame carrying one column of every splittable kind, each with metadata, on an odd SamplingClock."""
    metadata = dfb.column_metadata(tags=["t"], attributes={"k": "v"})
    columns = [
        dfb.double_column("double", [float(i) for i in range(count)], metadata=metadata),
        dfb.float_column("float", [i + 0.5 for i in range(count)]),
        dfb.int64_column("int64", list(range(count))),
        dfb.int32_column("int32", list(range(count))),
        dfb.bool_column("bool", [i % 2 == 0 for i in range(count)]),
        dfb.string_column("string", [f"s{i}" for i in range(count)]),
        dfb.enum_column("enum", [i % 3 for i in range(count)], enum_id="E"),
        dfb.struct_column("struct", [bytes([i]) for i in range(count)], schema_id="S:v1"),
        dfb.image_column("image", [bytes([i, i]) for i in range(count)], 2, 1, 1, "raw-u8"),
        dfb.double_array_column("darr", [[i, i + 0.5] for i in range(count)], metadata=metadata),
        dfb.float_array_column("farr", [[[float(i)], [float(i)]] for i in range(count)]),
        dfb.int32_array_column("iarr", [[i, -i] for i in range(count)]),
        dfb.int64_array_column("larr", [[i] for i in range(count)]),
        dfb.bool_array_column("barr", [[True, i % 2 == 0] for i in range(count)]),
        dfb.data_column("legacy", [i if i % 2 else None for i in range(count)]),
    ]
    return dfb.data_frame(dfb.sampling_clock(_ODD_START, _ODD_PERIOD, count), columns)


def _assert_chunks_reproduce(test, frame, chunks):
    """Concatenating the chunks' timestamps and per-column values gives back the frame's."""
    from dp_python_lib.client import data_frame_conversions as dfc

    test.assertEqual(
        [t for chunk in chunks for t in dfc.data_frame_timestamps(chunk)], dfc.data_frame_timestamps(frame)
    )
    expected = dfc.data_frame_columns(frame)
    combined = {name: [] for name in expected}
    for chunk in chunks:
        test.assertEqual(set(dfc.data_frame_columns(chunk)), set(expected))
        for name, values in dfc.data_frame_columns(chunk).items():
            combined[name].extend(values)
    test.assertEqual(combined, expected)


class TestSplitDataFrame(unittest.TestCase):
    def test_requires_a_limit(self):
        with self.assertRaises(ValueError) as ctx:
            dfb.split_data_frame(_every_kind_frame(3))
        self.assertIn("at least one of", str(ctx.exception))

    def test_rejects_out_of_range_limits(self):
        frame = _every_kind_frame(3)
        for kwargs in ({"max_rows": 0}, {"max_bytes": 0}, {"max_span_nanos": -1}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                dfb.split_data_frame(frame, **kwargs)

    def test_validates_eagerly_not_on_first_iteration(self):
        frame = common_pb2.DataFrame()
        frame.dataTimestamps.CopyFrom(_axis(2))
        frame.enumColumns.add(name="e").values[:] = [0, 1]  # no enumId
        with self.assertRaises(ValueError):
            dfb.split_data_frame(frame, max_rows=1)

    def test_rejects_a_frame_with_a_serialized_column(self):
        frame = dfb.data_frame(_axis(2), [dfb.double_column("d", [1.0, 2.0]), dfb.serialized_column("s", b"x", "e")])
        with self.assertRaises(ValueError) as ctx:
            dfb.split_data_frame(frame, max_rows=1)
        self.assertIn("serialized", str(ctx.exception))

    def test_is_lazy(self):
        chunks = dfb.split_data_frame(_every_kind_frame(10), max_rows=3)
        self.assertNotIsInstance(chunks, (list, tuple))
        self.assertEqual(next(chunks).dataTimestamps.samplingClock.count, 3)

    def test_max_rows_splits_every_column_kind_and_reproduces_the_frame(self):
        frame = _every_kind_frame(10)
        chunks = list(dfb.split_data_frame(frame, max_rows=4))
        self.assertEqual([c.dataTimestamps.samplingClock.count for c in chunks], [4, 4, 2])
        for chunk in chunks:
            dfb.validate_data_frame(chunk)
        _assert_chunks_reproduce(self, frame, chunks)

    def test_sampling_clock_chunk_starts_are_exact_integer_nanoseconds(self):
        frame = _every_kind_frame(10)
        chunks = list(dfb.split_data_frame(frame, max_rows=3))
        starts = [_nanos(c.dataTimestamps.samplingClock.startTime) for c in chunks]
        self.assertEqual(starts, [_nanos(_ODD_START) + row * _ODD_PERIOD for row in (0, 3, 6, 9)])
        for chunk in chunks:
            self.assertEqual(chunk.dataTimestamps.samplingClock.periodNanos, _ODD_PERIOD)

    def test_structural_fields_and_metadata_travel_with_every_chunk(self):
        for chunk in dfb.split_data_frame(_every_kind_frame(5), max_rows=2):
            self.assertEqual(chunk.enumColumns[0].enumId, "E")
            self.assertEqual(chunk.structColumns[0].schemaId, "S:v1")
            self.assertEqual(chunk.imageColumns[0].imageDescriptor.encoding, "raw-u8")
            self.assertEqual(list(chunk.floatArrayColumns[0].dimensions.dims), [2, 1])
            self.assertEqual(list(chunk.doubleColumns[0].metadata.tags), ["t"])
            self.assertEqual(list(chunk.doubleArrayColumns[0].metadata.tags), ["t"])

    def test_timestamp_list_is_sliced(self):
        axis = dfb.timestamp_list([100, 101, 105, 200, 201])
        frame = dfb.data_frame(axis, [dfb.double_column("d", [1.0, 2.0, 3.0, 4.0, 5.0])])
        chunks = list(dfb.split_data_frame(frame, max_rows=2))
        self.assertEqual(
            [[t.epochSeconds for t in c.dataTimestamps.timestampList.timestamps] for c in chunks],
            [[100, 101], [105, 200], [201]],
        )
        _assert_chunks_reproduce(self, frame, chunks)

    def test_max_span_on_a_sampling_clock_allows_exactly_the_limit(self):
        # The server rejects a span GREATER than its cap, measured first-to-last: (count - 1) * period.
        frame = dfb.data_frame(_axis(10), [dfb.double_column("d", [float(i) for i in range(10)])])
        chunks = list(dfb.split_data_frame(frame, max_span_nanos=3 * 1_000_000_000))
        self.assertEqual([c.dataTimestamps.samplingClock.count for c in chunks], [4, 4, 2])

    def test_max_span_on_a_timestamp_list(self):
        axis = dfb.timestamp_list([100, 101, 105, 200, 201])
        frame = dfb.data_frame(axis, [dfb.double_column("d", [1.0, 2.0, 3.0, 4.0, 5.0])])
        chunks = list(dfb.split_data_frame(frame, max_span_nanos=5 * 1_000_000_000))
        self.assertEqual(
            [[t.epochSeconds for t in c.dataTimestamps.timestampList.timestamps] for c in chunks],
            [[100, 101, 105], [200, 201]],
        )

    def test_zero_span_gives_one_row_per_chunk(self):
        frame = dfb.data_frame(_axis(3), [dfb.double_column("d", [1.0, 2.0, 3.0])])
        self.assertEqual(len(list(dfb.split_data_frame(frame, max_span_nanos=0))), 3)

    def test_the_tightest_limit_wins(self):
        frame = dfb.data_frame(_axis(10), [dfb.double_column("d", [float(i) for i in range(10)])])
        chunks = list(dfb.split_data_frame(frame, max_rows=3, max_span_nanos=1_000_000_000))
        self.assertEqual([c.dataTimestamps.samplingClock.count for c in chunks], [2, 2, 2, 2, 2])

    def test_budgeted_request_size_matches_a_real_request_with_worst_case_ids(self):
        # The envelope arithmetic must agree with protobuf's own measurement of the message the server receives.
        worst_id = "\U0010ffff" * dfb.MAX_BUDGETED_ID_CHARS
        for count in (1, 100, 20_000):
            with self.subTest(count=count):
                frame = dfb.data_frame(_axis(count), [dfb.double_column("d", [0.5] * count)])
                request = ingestion_pb2.IngestDataRequest(
                    providerId=worst_id, clientRequestId=worst_id, ingestionDataFrame=frame
                )
                self.assertEqual(dfb._budgeted_request_bytes(frame.ByteSize()), request.ByteSize())

    def test_every_chunk_fits_max_bytes_as_a_request_with_worst_case_ids(self):
        worst_id = "\U0010ffff" * dfb.MAX_BUDGETED_ID_CHARS
        frame = _every_kind_frame(200)
        max_bytes = 6_000
        chunks = list(dfb.split_data_frame(frame, max_bytes=max_bytes))
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            request = ingestion_pb2.IngestDataRequest(
                providerId=worst_id, clientRequestId=worst_id, ingestionDataFrame=chunk
            )
            self.assertLessEqual(request.ByteSize(), max_bytes)
        _assert_chunks_reproduce(self, frame, chunks)

    def test_max_bytes_holds_when_row_sizes_vary(self):
        # Rows early in the frame are tiny and later ones large, so a size estimated from the first rows overshoots.
        values = ["x"] * 50 + ["y" * 200] * 50
        frame = dfb.data_frame(_axis(100), [dfb.string_column("s", values)])
        max_bytes = 4_000
        chunks = list(dfb.split_data_frame(frame, max_bytes=max_bytes))
        for chunk in chunks:
            self.assertLessEqual(dfb._budgeted_request_bytes(chunk.ByteSize()), max_bytes)
        _assert_chunks_reproduce(self, frame, chunks)

    def test_a_row_too_large_for_max_bytes_raises_naming_it(self):
        frame = dfb.data_frame(_axis(3), [dfb.string_column("s", ["a", "b" * 5_000, "c"])])
        chunks = dfb.split_data_frame(frame, max_bytes=4_000)
        self.assertEqual(next(chunks).dataTimestamps.samplingClock.count, 1)
        with self.assertRaises(ValueError) as ctx:
            next(chunks)
        self.assertIn("row 1", str(ctx.exception))

    def test_server_default_is_exported_as_a_reference(self):
        self.assertEqual(dfb.SERVER_DEFAULT_MAX_MESSAGE_BYTES, 4_096_000)


if __name__ == "__main__":
    unittest.main()
