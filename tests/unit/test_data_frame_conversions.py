import os
import sys
import unittest
from datetime import datetime, timezone

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client import data_frame as dfb
from dp_python_lib.client import data_frame_conversions as dfc
from dp_python_lib.client.annotations_client import calculations
from dp_python_lib.client.query_conversions import Image
from dp_python_lib.grpc import annotation_pb2, common_pb2

# The pandas half depends on the optional [analysis] extra; skip those tests cleanly when it is absent.  The
# pure-Python read side needs no optional deps and always runs.
try:
    import pandas as pd

    _HAVE_ANALYSIS = True
except ImportError:
    _HAVE_ANALYSIS = False

T0 = datetime(2026, 7, 14, 18, 0, 0, tzinfo=timezone.utc)
T0_NANOS = int(T0.timestamp()) * 1_000_000_000


def _axis(count=3, period_nanos=1_000_000_000):
    return dfb.sampling_clock(T0, period_nanos, count)


# ----------------------------------------------------------------------
# pure Python read side (no optional dependencies)
# ----------------------------------------------------------------------


class TestDataFrameTimestamps(unittest.TestCase):
    def test_sampling_clock_expands_in_exact_integer_nanoseconds(self):
        # The whole point of the integer path: a float64 cannot hold present-day epoch nanos, so a float
        # round-trip would move every one of these.
        frame = dfb.data_frame(_axis(3, period_nanos=1), [dfb.double_column("d", [1.0, 2.0, 3.0])])
        self.assertEqual(dfc.data_frame_timestamps(frame), [T0_NANOS, T0_NANOS + 1, T0_NANOS + 2])

    def test_timestamp_list_is_returned_in_order(self):
        axis = dfb.timestamp_list([100, 200, 300])
        frame = dfb.data_frame(axis, [dfb.double_column("d", [1.0, 2.0, 3.0])])
        self.assertEqual(
            dfc.data_frame_timestamps(frame),
            [100_000_000_000, 200_000_000_000, 300_000_000_000],
        )

    def test_frame_without_axis_raises(self):
        frame = common_pb2.DataFrame()
        with self.assertRaises(ValueError) as ctx:
            dfc.data_frame_timestamps(frame)
        self.assertIn("dataTimestamps", str(ctx.exception))


class TestColumnValues(unittest.TestCase):
    """column_values() is a standalone per-column converter, reusable by the bucket query (#16)."""

    def test_each_scalar_column_type(self):
        cases = [
            (dfb.double_column("d", [1.5, 2.5]), [1.5, 2.5]),
            (dfb.int64_column("i", [1, 2]), [1, 2]),
            (dfb.int32_column("i32", [3, 4]), [3, 4]),
            (dfb.bool_column("b", [True, False]), [True, False]),
            (dfb.string_column("s", ["a", "b"]), ["a", "b"]),
            (dfb.enum_column("e", [0, 1], enum_id="x"), [0, 1]),
        ]
        for column, expected in cases:
            with self.subTest(column=column.name):
                self.assertEqual(dfc.column_values(column), expected)

    def test_float_column_values_round_to_float32(self):
        # float32 storage is lossy by nature; assert the round trip rather than exact float64 equality.
        column = dfb.float_column("f", [1.5, 2.25])
        self.assertEqual(dfc.column_values(column), [1.5, 2.25])

    def test_data_column_unset_value_becomes_none(self):
        # The one representation of a gap in this API.
        column = dfb.data_column("sparse", [1.0, None, 3.0])
        self.assertEqual(dfc.column_values(column), [1.0, None, 3.0])

    def test_data_column_complex_arms(self):
        column = common_pb2.DataColumn()
        column.name = "mixed"
        column.dataValues.add().stringValue = "s"
        image = column.dataValues.add()
        image.imageValue.image = b"png-bytes"
        image.imageValue.fileType = common_pb2.Image.FileType.PNG

        values = dfc.column_values(column)
        self.assertEqual(values[0], "s")
        self.assertEqual(values[1], Image(b"png-bytes", "PNG"))

    def test_array_column_is_reshaped_per_sample(self):
        column = common_pb2.DoubleArrayColumn()
        column.name = "waveform"
        column.dimensions.dims.extend([3])
        column.values[:] = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        self.assertEqual(dfc.column_values(column), [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])

    def test_multidimensional_array_stays_flat_within_a_sample(self):
        column = common_pb2.Int32ArrayColumn()
        column.name = "grid"
        column.dimensions.dims.extend([2, 2])
        column.values[:] = [1, 2, 3, 4, 5, 6, 7, 8]
        self.assertEqual(dfc.column_values(column), [[1, 2, 3, 4], [5, 6, 7, 8]])

    def test_array_column_without_dims_raises(self):
        column = common_pb2.DoubleArrayColumn()
        column.name = "waveform"
        column.values[:] = [1.0, 2.0]
        with self.assertRaises(ValueError) as ctx:
            dfc.column_values(column)
        self.assertIn("dimensions", str(ctx.exception))

    def test_array_column_with_indivisible_values_raises(self):
        column = common_pb2.DoubleArrayColumn()
        column.name = "waveform"
        column.dimensions.dims.extend([3])
        column.values[:] = [1.0, 2.0, 3.0, 4.0]  # not a whole multiple of 3
        with self.assertRaises(ValueError) as ctx:
            dfc.column_values(column)
        self.assertIn("not a whole multiple", str(ctx.exception))

    def test_image_column_yields_bytes_per_sample(self):
        column = common_pb2.ImageColumn()
        column.name = "frames"
        column.images.extend([b"a", b"b"])
        self.assertEqual(dfc.column_values(column), [b"a", b"b"])

    def test_unsupported_type_raises(self):
        with self.assertRaises(ValueError):
            dfc.column_values("not a column")


class TestDataFrameColumns(unittest.TestCase):
    def test_collects_every_column_type_by_name(self):
        frame = dfb.data_frame(
            _axis(2),
            [
                dfb.double_column("d", [1.0, 2.0]),
                dfb.string_column("s", ["a", "b"]),
                dfb.data_column("legacy", [1.0, None]),
            ],
        )
        self.assertEqual(dfc.data_frame_columns(frame), {"d": [1.0, 2.0], "s": ["a", "b"], "legacy": [1.0, None]})

    def test_duplicate_names_raise_rather_than_dropping(self):
        # data_frame() prevents this, but a hand-built or older-server frame can still carry it.
        frame = common_pb2.DataFrame()
        frame.dataTimestamps.CopyFrom(_axis(1))
        for field in (frame.doubleColumns, frame.stringColumns):
            column = field.add()
            column.name = "dup"
        frame.doubleColumns[0].values[:] = [1.0]
        frame.stringColumns[0].values[:] = ["a"]

        with self.assertRaises(ValueError) as ctx:
            dfc.data_frame_columns(frame)
        self.assertIn("dup", str(ctx.exception))

    def test_serialized_columns_are_skipped(self):
        frame = common_pb2.DataFrame()
        frame.dataTimestamps.CopyFrom(_axis(1))
        double = frame.doubleColumns.add()
        double.name = "d"
        double.values[:] = [1.0]
        serialized = frame.serializedDataColumns.add()
        serialized.name = "opaque"
        serialized.payload = b"blob"

        self.assertEqual(dfc.data_frame_columns(frame), {"d": [1.0]})


class TestColumnMetadataDict(unittest.TestCase):
    def test_full_metadata(self):
        column = dfb.double_column(
            "d",
            [1.0],
            metadata=dfb.column_metadata(
                tags=["derived"],
                attributes={"unit": "mm"},
                provenance=dfb.provenance(
                    source="rig",
                    process="RMS",
                    derived_from=[dfb.pv_source("A:1"), dfb.calculations_source("c", "f", "x")],
                ),
            ),
        )
        result = dfc.column_metadata_dict(column)

        self.assertEqual(result["tags"], ["derived"])
        self.assertEqual(result["attributes"], {"unit": "mm"})
        self.assertEqual(result["provenance"]["source"], "rig")
        self.assertEqual(result["provenance"]["process"], "RMS")
        self.assertEqual(result["provenance"]["derived_from"][0]["pv_name"], "A:1")
        self.assertIsNone(result["provenance"]["derived_from"][0]["calculations_column"])
        self.assertEqual(
            result["provenance"]["derived_from"][1]["calculations_column"],
            {"calculations_id": "c", "frame_name": "f", "column_name": "x"},
        )

    def test_absent_metadata(self):
        result = dfc.column_metadata_dict(dfb.double_column("d", [1.0]))
        self.assertEqual(result, {"tags": [], "attributes": {}, "provenance": None})


# ----------------------------------------------------------------------
# pandas bridges ([analysis] extra)
# ----------------------------------------------------------------------


@unittest.skipUnless(_HAVE_ANALYSIS, "requires the [analysis] extra (pandas)")
class TestDataFrameToPandas(unittest.TestCase):
    def test_basic_conversion(self):
        frame = dfb.data_frame(
            _axis(3),
            [dfb.double_column("x_rms", [12.7, 12.8, 12.9]), dfb.string_column("label", ["a", "b", "c"])],
        )
        df = dfc.data_frame_to_pandas(frame)

        self.assertEqual(list(df.columns), ["x_rms", "label"])
        self.assertEqual(df["x_rms"].tolist(), [12.7, 12.8, 12.9])
        self.assertEqual(len(df), 3)

    def test_index_is_utc_and_nanosecond_exact(self):
        # A SamplingClock at 1 ns spacing: only an integer path reproduces these instants.
        frame = dfb.data_frame(_axis(3, period_nanos=1), [dfb.double_column("d", [1.0, 2.0, 3.0])])
        df = dfc.data_frame_to_pandas(frame)

        self.assertEqual(str(df.index.tz), "UTC")
        self.assertEqual(
            [int(v) for v in df.index.view("int64")],
            [T0_NANOS, T0_NANOS + 1, T0_NANOS + 2],
        )

    def test_metadata_lands_in_attrs(self):
        frame = dfb.data_frame(
            _axis(1),
            [dfb.double_column("d", [1.0], metadata=dfb.column_metadata(tags=["derived"]))],
        )
        df = dfc.data_frame_to_pandas(frame)

        self.assertEqual(df.attrs["column_metadata"]["d"]["tags"], ["derived"])

    def test_metadata_can_be_excluded(self):
        frame = dfb.data_frame(_axis(1), [dfb.double_column("d", [1.0])])
        df = dfc.data_frame_to_pandas(frame, exclude_column_metadata=True)
        self.assertNotIn("column_metadata", df.attrs)

    def test_gap_becomes_nan(self):
        frame = dfb.data_frame(_axis(3), [dfb.data_column("sparse", [1.0, None, 3.0])])
        df = dfc.data_frame_to_pandas(frame)
        self.assertTrue(df["sparse"].isna().tolist() == [False, True, False])

    def test_misaligned_column_raises(self):
        # Hand-built: data_frame() would have caught this on the way out.
        frame = common_pb2.DataFrame()
        frame.dataTimestamps.CopyFrom(_axis(3))
        column = frame.doubleColumns.add()
        column.name = "short"
        column.values[:] = [1.0, 2.0]

        with self.assertRaises(ValueError) as ctx:
            dfc.data_frame_to_pandas(frame)
        message = str(ctx.exception)
        self.assertIn("short", message)
        self.assertIn("index-aligned", message)


@unittest.skipUnless(_HAVE_ANALYSIS, "requires the [analysis] extra (pandas)")
class TestDataFrameFromPandas(unittest.TestCase):
    def _index(self, periods=3):
        return pd.date_range("2026-07-14 18:00", periods=periods, freq="s", tz="UTC")

    def test_dtype_mapping(self):
        import numpy as np

        df = pd.DataFrame(
            {
                "d": [1.0, 2.0],
                "f": np.array([1.5, 2.5], dtype="float32"),
                "i64": np.array([1, 2], dtype="int64"),
                "i32": np.array([1, 2], dtype="int32"),
                "b": [True, False],
                "s": ["a", "b"],
            },
            index=self._index(2),
        )
        frame = dfc.data_frame_from_pandas(df)

        self.assertEqual([c.name for c in frame.doubleColumns], ["d"])
        self.assertEqual([c.name for c in frame.floatColumns], ["f"])
        self.assertEqual([c.name for c in frame.int64Columns], ["i64"])
        self.assertEqual([c.name for c in frame.int32Columns], ["i32"])
        self.assertEqual([c.name for c in frame.boolColumns], ["b"])
        self.assertEqual([c.name for c in frame.stringColumns], ["s"])

    def test_index_becomes_a_timestamp_list(self):
        # Never a SamplingClock: inferring uniformity from an index that merely looks regular would move
        # timestamps, which this API cannot tolerate.
        df = pd.DataFrame({"d": [1.0, 2.0, 3.0]}, index=self._index())
        frame = dfc.data_frame_from_pandas(df)

        self.assertEqual(frame.dataTimestamps.WhichOneof("value"), "timestampList")
        self.assertEqual(len(frame.dataTimestamps.timestampList.timestamps), 3)

    def test_round_trip_preserves_values_and_instants(self):
        index = self._index()
        df = pd.DataFrame({"d": [1.0, 2.0, 3.0], "s": ["a", "b", "c"]}, index=index)

        back = dfc.data_frame_to_pandas(dfc.data_frame_from_pandas(df))

        self.assertEqual(back["d"].tolist(), [1.0, 2.0, 3.0])
        self.assertEqual(back["s"].tolist(), ["a", "b", "c"])
        self.assertEqual(
            [int(v) for v in back.index.view("int64")],
            [int(v) for v in index.view("int64")],
        )

    def test_nan_is_fail_loud_with_sparsity_guidance(self):
        df = pd.DataFrame({"x": [1.0, None, 3.0]}, index=self._index())

        with self.assertRaises(ValueError) as ctx:
            dfc.data_frame_from_pandas(df)
        message = str(ctx.exception)
        self.assertIn("x", message)
        self.assertIn("missing values", message)
        self.assertIn("data_column", message)

    def test_naive_index_is_rejected(self):
        df = pd.DataFrame({"x": [1.0]}, index=pd.date_range("2026-07-14", periods=1, freq="s"))
        with self.assertRaises(ValueError) as ctx:
            dfc.data_frame_from_pandas(df)
        self.assertIn("timezone-aware", str(ctx.exception))

    def test_non_datetime_index_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            dfc.data_frame_from_pandas(pd.DataFrame({"x": [1.0]}))
        self.assertIn("DatetimeIndex", str(ctx.exception))

    def test_non_increasing_index_is_rejected(self):
        index = pd.DatetimeIndex(["2026-07-14 18:00:01", "2026-07-14 18:00:00"], tz="UTC")
        with self.assertRaises(ValueError) as ctx:
            dfc.data_frame_from_pandas(pd.DataFrame({"x": [1.0, 2.0]}, index=index))
        self.assertIn("strictly increasing", str(ctx.exception))

    def test_no_columns_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            dfc.data_frame_from_pandas(pd.DataFrame(index=self._index()))
        self.assertIn("at least one column", str(ctx.exception))

    def test_unmappable_dtype_is_rejected(self):
        df = pd.DataFrame({"c": [1 + 2j, 2 + 0j, 3 + 0j]}, index=self._index())
        with self.assertRaises(ValueError) as ctx:
            dfc.data_frame_from_pandas(df)
        message = str(ctx.exception)
        self.assertIn("complex128", message)
        self.assertIn("no typed-column mapping", message)

    def test_object_column_holding_non_strings_is_rejected(self):
        df = pd.DataFrame({"o": [{"a": 1}, {"b": 2}, {"c": 3}]}, index=self._index())
        with self.assertRaises(ValueError) as ctx:
            dfc.data_frame_from_pandas(df)
        self.assertIn("non-string value", str(ctx.exception))


@unittest.skipUnless(_HAVE_ANALYSIS, "requires the [analysis] extra (pandas)")
class TestCalculationsBridges(unittest.TestCase):
    def _index(self, periods=3):
        return pd.date_range("2026-07-14 18:00", periods=periods, freq="s", tz="UTC")

    def test_round_trip_multiple_frames(self):
        frames = {
            "bpm-statistics": pd.DataFrame({"x_rms": [1.0, 2.0, 3.0]}, index=self._index()),
            "rf-statistics": pd.DataFrame({"y_rms": [4.0, 5.0, 6.0]}, index=self._index()),
        }
        calcs = dfc.calculations_from_dataframes(frames)

        self.assertEqual(
            {f.name for f in calcs.calculationDataFrames},
            {"bpm-statistics", "rf-statistics"},
        )

        back = dfc.calculations_to_dataframes(calcs)
        self.assertEqual(set(back), set(frames))
        self.assertEqual(back["bpm-statistics"]["x_rms"].tolist(), [1.0, 2.0, 3.0])
        self.assertEqual(back["rf-statistics"]["y_rms"].tolist(), [4.0, 5.0, 6.0])

    def test_to_dataframes_none_is_empty(self):
        self.assertEqual(dfc.calculations_to_dataframes(None), {})

    def test_to_dataframes_no_frames_is_empty(self):
        self.assertEqual(dfc.calculations_to_dataframes(annotation_pb2.Calculations()), {})

    def test_to_dataframes_rejects_duplicate_frame_names(self):
        calcs = annotation_pb2.Calculations()
        for _ in range(2):
            entry = calcs.calculationDataFrames.add()
            entry.name = "dup"
            entry.frame.CopyFrom(dfb.data_frame(_axis(1), [dfb.double_column("d", [1.0])]))

        with self.assertRaises(ValueError) as ctx:
            dfc.calculations_to_dataframes(calcs)
        self.assertIn("dup", str(ctx.exception))

    def test_from_dataframes_rejects_empty(self):
        with self.assertRaises(ValueError) as ctx:
            dfc.calculations_from_dataframes({})
        self.assertIn("at least one frame", str(ctx.exception))

    def test_from_dataframes_rejects_empty_frame_name(self):
        with self.assertRaises(ValueError):
            dfc.calculations_from_dataframes({"": pd.DataFrame({"x": [1.0]}, index=self._index(1))})

    def test_metadata_survives_to_pandas(self):
        frame = dfb.data_frame(
            _axis(1),
            [dfb.double_column("d", [1.0], metadata=dfb.column_metadata(tags=["derived"]))],
        )
        back = dfc.calculations_to_dataframes(calculations({"f1": frame}))
        self.assertEqual(back["f1"].attrs["column_metadata"]["d"]["tags"], ["derived"])


if __name__ == "__main__":
    unittest.main()
