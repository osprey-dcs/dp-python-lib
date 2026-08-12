import unittest
from unittest.mock import Mock
import sys
import os
import tempfile

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client import query_conversions as conv
from dp_python_lib.client.query_conversions import Image, data_value_to_python
from dp_python_lib.grpc import common_pb2
from dp_python_lib.grpc import query_pb2

# The conversion layer depends on the optional [analysis] extra; skip the DataFrame/NumPy/Excel tests cleanly when
# it is not installed.  The pure DataValue-extraction tests need no optional deps and always run.
try:
    import pandas as pd
    import numpy as np

    _HAVE_ANALYSIS = True
except ImportError:
    _HAVE_ANALYSIS = False


def _ts(seconds, nanos=0):
    ts = common_pb2.Timestamp()
    ts.epochSeconds = seconds
    ts.nanoseconds = nanos
    return ts


def _scalar(**kwargs):
    """Build a DataValue with a single scalar oneof arm set via keyword (e.g. doubleValue=1.5)."""
    v = common_pb2.DataValue()
    for k, val in kwargs.items():
        setattr(v, k, val)
    return v


def _timestamp_value(seconds, nanos=0):
    v = common_pb2.DataValue()
    v.timestampValue.CopyFrom(_ts(seconds, nanos))
    return v


def _array_value(*inner_values):
    v = common_pb2.DataValue()
    v.arrayValue.dataValues.extend(inner_values)
    return v


def _structure_value(**named_values):
    v = common_pb2.DataValue()
    for name, inner in named_values.items():
        field = v.structureValue.fields.add()
        field.name = name
        field.value.CopyFrom(inner)
    return v


def _image_value(data, file_type):
    v = common_pb2.DataValue()
    v.imageValue.image = data
    v.imageValue.fileType = file_type
    return v


def _column_table(timestamps, columns):
    """
    Build a ColumnTable.
    :param timestamps: list of (seconds, nanos) or seconds ints.
    :param columns: list of (name, [DataValue], optional metadata dict) tuples.
    """
    ct = query_pb2.ColumnTable()
    for t in timestamps:
        if isinstance(t, tuple):
            ct.timestampList.timestamps.append(_ts(*t))
        else:
            ct.timestampList.timestamps.append(_ts(t))
    for spec in columns:
        name, values = spec[0], spec[1]
        meta = spec[2] if len(spec) > 2 else None
        col = ct.dataColumns.add()
        col.name = name
        col.dataValues.extend(values)
        if meta is not None:
            col.metadata.tags.extend(meta.get("tags", []))
            for k, val in meta.get("attributes", {}).items():
                attr = col.metadata.attributes.add()
                attr.name = k
                attr.value = val
    return ct


# ----------------------------------------------------------------------
# DataValue extraction (no optional deps)
# ----------------------------------------------------------------------


class TestDataValueToPython(unittest.TestCase):
    def test_unset_returns_none(self):
        self.assertIsNone(data_value_to_python(common_pb2.DataValue()))

    def test_string(self):
        self.assertEqual(data_value_to_python(_scalar(stringValue="hi")), "hi")

    def test_bool(self):
        self.assertIs(data_value_to_python(_scalar(booleanValue=True)), True)

    def test_int_arms(self):
        self.assertEqual(data_value_to_python(_scalar(uintValue=7)), 7)
        self.assertEqual(data_value_to_python(_scalar(ulongValue=8)), 8)
        self.assertEqual(data_value_to_python(_scalar(intValue=-3)), -3)
        self.assertEqual(data_value_to_python(_scalar(longValue=-4)), -4)

    def test_float_double(self):
        self.assertAlmostEqual(data_value_to_python(_scalar(floatValue=1.5)), 1.5)
        self.assertEqual(data_value_to_python(_scalar(doubleValue=2.5)), 2.5)

    def test_byte_array(self):
        self.assertEqual(data_value_to_python(_scalar(byteArrayValue=b"\x00\x01")), b"\x00\x01")

    def test_timestamp_returns_epoch_nanos(self):
        # 1704067200 s + 500 ns
        self.assertEqual(
            data_value_to_python(_timestamp_value(1704067200, 500)),
            1704067200 * 1_000_000_000 + 500,
        )

    def test_array_recurses_to_list(self):
        v = _array_value(_scalar(intValue=1), _scalar(intValue=2))
        self.assertEqual(data_value_to_python(v), [1, 2])

    def test_structure_recurses_to_dict(self):
        v = _structure_value(x=_scalar(intValue=7), y=_scalar(stringValue="s"))
        self.assertEqual(data_value_to_python(v), {"x": 7, "y": "s"})

    def test_image_wrapper(self):
        v = _image_value(b"\x89PNG", common_pb2.Image.PNG)
        result = data_value_to_python(v)
        self.assertEqual(result, Image(b"\x89PNG", "PNG"))
        self.assertEqual(result.file_type, "PNG")

    def test_nested_array_of_structures(self):
        v = _array_value(_structure_value(a=_scalar(intValue=1)))
        self.assertEqual(data_value_to_python(v), [{"a": 1}])


# ----------------------------------------------------------------------
# ColumnTable -> DataFrame
# ----------------------------------------------------------------------


@unittest.skipUnless(_HAVE_ANALYSIS, "requires the [analysis] extra (pandas/numpy)")
class TestColumnTableToDataFrame(unittest.TestCase):
    def test_none_yields_empty(self):
        df = conv.column_table_to_dataframe(None)
        self.assertTrue(df.empty)

    def test_basic_scalar_columns(self):
        ct = _column_table(
            [1704067200, 1704067201],
            [("temp", [_scalar(doubleValue=1.5), _scalar(doubleValue=2.5)])],
        )
        df = conv.column_table_to_dataframe(ct)
        self.assertEqual(list(df.columns), ["temp"])
        self.assertEqual(list(df["temp"]), [1.5, 2.5])
        self.assertEqual(str(df["temp"].dtype), "float64")
        self.assertIsInstance(df.index, pd.DatetimeIndex)
        self.assertEqual(str(df.index.tz), "UTC")

    def test_int_gap_upcasts_to_float(self):
        ct = _column_table([1, 2], [("count", [_scalar(intValue=10), common_pb2.DataValue()])])
        df = conv.column_table_to_dataframe(ct)
        self.assertEqual(str(df["count"].dtype), "float64")
        self.assertEqual(df["count"].iloc[0], 10.0)
        self.assertTrue(pd.isna(df["count"].iloc[1]))

    def test_complex_arms_preserved_as_objects(self):
        ct = _column_table([1], [("arr", [_array_value(_scalar(intValue=1), _scalar(intValue=2))])])
        df = conv.column_table_to_dataframe(ct)
        self.assertEqual(str(df["arr"].dtype), "object")
        self.assertEqual(df["arr"].iloc[0], [1, 2])

    def test_timestamp_column_is_datetime(self):
        ct = _column_table([1704067200], [("event_time", [_timestamp_value(1704070800)])])
        df = conv.column_table_to_dataframe(ct)
        self.assertIsInstance(df["event_time"].dtype, pd.DatetimeTZDtype)

    def test_metadata_attached_to_attrs(self):
        ct = _column_table(
            [1],
            [
                (
                    "temp",
                    [_scalar(doubleValue=1.5)],
                    {"tags": ["vacuum"], "attributes": {"unit": "V"}},
                )
            ],
        )
        df = conv.column_table_to_dataframe(ct)
        self.assertEqual(df.attrs["column_metadata"]["temp"]["tags"], ["vacuum"])
        self.assertEqual(df.attrs["column_metadata"]["temp"]["attributes"], {"unit": "V"})

    def test_metadata_excluded(self):
        ct = _column_table([1], [("temp", [_scalar(doubleValue=1.5)], {"tags": ["vacuum"]})])
        df = conv.column_table_to_dataframe(ct, exclude_column_metadata=True)
        self.assertNotIn("column_metadata", df.attrs)

    def test_alignment_mismatch_raises(self):
        ct = _column_table([1, 2], [("short", [_scalar(intValue=1)])])  # 1 value, 2 timestamps
        with self.assertRaises(ValueError):
            conv.column_table_to_dataframe(ct)

    def test_serialized_columns_not_implemented(self):
        ct = query_pb2.ColumnTable()
        ct.serializedDataColumns.add().name = "s"
        with self.assertRaises(NotImplementedError):
            conv.column_table_to_dataframe(ct)

    def test_empty_timestamp_list(self):
        ct = _column_table([], [("temp", [])])
        df = conv.column_table_to_dataframe(ct)
        self.assertEqual(len(df), 0)
        self.assertEqual(list(df.columns), ["temp"])

    def test_duplicate_column_names_raise(self):
        # Columns are keyed by name, so a duplicate would silently overwrite the earlier column and drop a
        # whole PV's data.  Fail loud instead, and name the offending column.
        ct = _column_table([1], [("dup", [_scalar(intValue=10)]), ("dup", [_scalar(intValue=20)])])
        with self.assertRaises(ValueError) as cm:
            conv.column_table_to_dataframe(ct)
        self.assertIn("dup", str(cm.exception))

    def test_distinct_column_names_do_not_raise(self):
        ct = _column_table([1], [("a", [_scalar(intValue=10)]), ("b", [_scalar(intValue=20)])])
        df = conv.column_table_to_dataframe(ct)
        self.assertEqual(list(df.columns), ["a", "b"])


# ----------------------------------------------------------------------
# ColumnTable -> NumPy
# ----------------------------------------------------------------------


@unittest.skipUnless(_HAVE_ANALYSIS, "requires the [analysis] extra (pandas/numpy)")
class TestColumnTableToNumpy(unittest.TestCase):
    def test_none_yields_empty(self):
        self.assertEqual(conv.column_table_to_numpy(None), {})

    def test_dict_of_arrays(self):
        ct = _column_table(
            [1704067200, 1704067201],
            [("temp", [_scalar(doubleValue=1.5), _scalar(doubleValue=2.5)])],
        )
        out = conv.column_table_to_numpy(ct)
        self.assertIn("timestamps", out)
        self.assertEqual(str(out["timestamps"].dtype), "datetime64[ns]")
        self.assertEqual(list(out["temp"]), [1.5, 2.5])

    def test_complex_column_object_array(self):
        ct = _column_table(
            [1, 2], [("arr", [_array_value(_scalar(intValue=1)), common_pb2.DataValue()])]
        )
        out = conv.column_table_to_numpy(ct)
        self.assertEqual(out["arr"].dtype, object)
        self.assertEqual(out["arr"][0], [1])
        self.assertIsNone(out["arr"][1])

    def test_alignment_mismatch_raises(self):
        ct = _column_table([1, 2], [("short", [_scalar(intValue=1)])])
        with self.assertRaises(ValueError):
            conv.column_table_to_numpy(ct)

    def test_serialized_columns_not_implemented(self):
        ct = query_pb2.ColumnTable()
        ct.serializedDataColumns.add().name = "s"
        with self.assertRaises(NotImplementedError):
            conv.column_table_to_numpy(ct)

    def test_uniform_length_arrays_stay_1d_object(self):
        # Regression: equal-length arrayValues used to succeed as np.array(...) and collapse into a (2, 2)
        # int64 array, so a column's shape depended on whether its rows happened to match in length.
        ct = _column_table(
            [1, 2],
            [
                (
                    "arr",
                    [
                        _array_value(_scalar(intValue=1), _scalar(intValue=2)),
                        _array_value(_scalar(intValue=3), _scalar(intValue=4)),
                    ],
                )
            ],
        )
        out = conv.column_table_to_numpy(ct)
        self.assertEqual(out["arr"].shape, (2,))
        self.assertEqual(out["arr"].dtype, object)
        self.assertEqual(out["arr"][0], [1, 2])
        self.assertEqual(out["arr"][1], [3, 4])

    def test_ragged_arrays_stay_1d_object(self):
        ct = _column_table(
            [1, 2],
            [
                (
                    "arr",
                    [
                        _array_value(_scalar(intValue=1)),
                        _array_value(_scalar(intValue=2), _scalar(intValue=3)),
                    ],
                )
            ],
        )
        out = conv.column_table_to_numpy(ct)
        self.assertEqual(out["arr"].shape, (2,))
        self.assertEqual(out["arr"].dtype, object)
        self.assertEqual(out["arr"][1], [2, 3])

    def test_structure_and_image_columns_stay_1d_object(self):
        ct = _column_table(
            [1, 2],
            [
                (
                    "st",
                    [
                        _structure_value(a=_scalar(intValue=1)),
                        _structure_value(a=_scalar(intValue=2)),
                    ],
                ),
                (
                    "img",
                    [
                        _image_value(b"\x89PNG", common_pb2.Image.PNG),
                        _image_value(b"\xff\xd8", common_pb2.Image.JPEG),
                    ],
                ),
            ],
        )
        out = conv.column_table_to_numpy(ct)
        for name in ("st", "img"):
            self.assertEqual(out[name].shape, (2,))
            self.assertEqual(out[name].dtype, object)
        self.assertEqual(out["st"][0], {"a": 1})
        self.assertEqual(out["img"][0], Image(b"\x89PNG", "PNG"))

    def test_scalar_columns_keep_native_dtype(self):
        # The complex-arm branch must not swallow ordinary scalar columns into object arrays.
        ct = _column_table([1, 2], [("i", [_scalar(intValue=1), _scalar(intValue=2)])])
        out = conv.column_table_to_numpy(ct)
        self.assertEqual(out["i"].shape, (2,))
        self.assertNotEqual(out["i"].dtype, object)

    def test_duplicate_column_names_raise(self):
        ct = _column_table([1], [("dup", [_scalar(intValue=10)]), ("dup", [_scalar(intValue=20)])])
        with self.assertRaises(ValueError) as cm:
            conv.column_table_to_numpy(ct)
        self.assertIn("dup", str(cm.exception))


# ----------------------------------------------------------------------
# Excel export
# ----------------------------------------------------------------------


@unittest.skipUnless(_HAVE_ANALYSIS, "requires the [analysis] extra (pandas/numpy/openpyxl)")
class TestDataFrameToExcel(unittest.TestCase):
    def _sample_df(self):
        ct = _column_table(
            [1704067200],
            [("v", [_scalar(doubleValue=1.5)]), ("arr", [_array_value(_scalar(intValue=1))])],
        )
        return conv.column_table_to_dataframe(ct)

    def test_writes_file(self):
        df = self._sample_df()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.xlsx")
            conv.dataframe_to_excel(df, path)
            self.assertTrue(os.path.exists(path))

    def test_complex_cells_stringified(self):
        df = self._sample_df()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.xlsx")
            conv.dataframe_to_excel(df, path)
            back = pd.read_excel(path, engine="openpyxl")
            # The array cell (a Python list) round-trips as a JSON string, not a list.
            self.assertEqual(back["arr"].iloc[0], "[1]")

    def test_max_rows_guard(self):
        df = self._sample_df()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.xlsx")
            with self.assertRaises(ValueError):
                conv.dataframe_to_excel(df, path, max_rows=0)

    def test_tz_dropped(self):
        df = self._sample_df()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.xlsx")
            conv.dataframe_to_excel(df, path)  # should not raise on tz-aware index

    def test_bytes_cells_written_natively(self):
        # byteArrayValue lands in an object column but needs no stringification: openpyxl writes bytes
        # natively.  A blanket repr() fallback would instead persist the string "b'\\x00\\x01'".
        ct = _column_table([1704067200], [("b", [_scalar(byteArrayValue=b"\x00\x01")])])
        df = conv.column_table_to_dataframe(ct)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.xlsx")
            conv.dataframe_to_excel(df, path)
            self.assertTrue(os.path.exists(path))

    def test_image_cells_stringified_via_repr(self):
        ct = _column_table(
            [1704067200], [("img", [_image_value(b"\x89PNG", common_pb2.Image.PNG)])]
        )
        df = conv.column_table_to_dataframe(ct)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.xlsx")
            conv.dataframe_to_excel(df, path)
            back = pd.read_excel(path, engine="openpyxl")
            self.assertIn("Image(file_type='PNG'", back["img"].iloc[0])


# ----------------------------------------------------------------------
# Whole-query conveniences (page internally; concat by name)
# ----------------------------------------------------------------------


@unittest.skipUnless(_HAVE_ANALYSIS, "requires the [analysis] extra (pandas/numpy)")
class TestQuerySamplesToDataFrame(unittest.TestCase):
    def _page(self, ct):
        page = Mock()
        page.column_table = ct
        return page

    def _client(self, pages, stream_pages=None):
        client = Mock()
        client.iter_query_samples = Mock(return_value=iter(pages))
        if stream_pages is not None:
            client.iter_query_samples_stream = Mock(return_value=iter(stream_pages))
        return client

    def _params(self, exclude_metadata=False):
        params = Mock()
        params.exclude_column_metadata = exclude_metadata
        return params

    def test_concat_rows_by_name(self):
        ct1 = _column_table([1, 2], [("a", [_scalar(intValue=1), _scalar(intValue=2)])])
        ct2 = _column_table([3], [("a", [_scalar(intValue=3)])])
        client = self._client([self._page(ct1), self._page(ct2)])
        df = conv.query_samples_to_dataframe(client, self._params())
        self.assertEqual(list(df["a"]), [1, 2, 3])
        self.assertEqual(len(df), 3)

    def test_concat_outer_join_disjoint_columns(self):
        # A column appears on page 2 only (live-metadata race): outer join tolerates it.
        ct1 = _column_table([1], [("a", [_scalar(intValue=1)])])
        ct2 = _column_table([2], [("a", [_scalar(intValue=2)]), ("b", [_scalar(intValue=9)])])
        client = self._client([self._page(ct1), self._page(ct2)])
        df = conv.query_samples_to_dataframe(client, self._params())
        self.assertEqual(sorted(df.columns), ["a", "b"])
        self.assertEqual(len(df), 2)

    def test_empty_result(self):
        client = self._client([])
        df = conv.query_samples_to_dataframe(client, self._params())
        self.assertTrue(df.empty)

    def test_max_rows_exceeded_raises(self):
        ct = _column_table(
            [1, 2, 3], [("a", [_scalar(intValue=1), _scalar(intValue=2), _scalar(intValue=3)])]
        )
        client = self._client([self._page(ct)])
        with self.assertRaises(ValueError):
            conv.query_samples_to_dataframe(client, self._params(), max_rows=2)

    def test_stream_yields_frames_lazily(self):
        ct1 = _column_table([1], [("a", [_scalar(intValue=1)])])
        ct2 = _column_table([2], [("a", [_scalar(intValue=2)])])
        client = self._client([], stream_pages=[self._page(ct1), self._page(ct2)])
        frames = list(conv.stream_query_samples_to_dataframes(client, self._params()))
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0]["a"].iloc[0], 1)


if __name__ == "__main__":
    unittest.main()
