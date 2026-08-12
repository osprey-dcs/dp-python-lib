"""
Pythonic conversions for v2 query results (issue #7, Phase 2).

Turns the raw protobuf ColumnTable returned by QueryClient into common analysis formats: pandas DataFrame, NumPy
arrays, and Excel export.  These conveniences depend on the optional [analysis] extra (pandas / numpy / openpyxl),
which is NOT part of the core install.  pandas/numpy are imported lazily inside each entry point so that importing
QueryClient (and this module) never requires them; a missing extra raises a clear, actionable error.

Design decisions (see .dev/plan/issue-7/plan.md, Q6 and section 2):
  - DataValue -> Python value: the scalar/timestamp arms map to native pandas dtypes; the complex arms (array,
    structure, byteArray, image) are preserved losslessly as Python objects in an object-dtype column, never
    auto-flattened.  An unset value in an otherwise-populated column becomes None (pandas renders NaN); an
    *unhandled* oneof arm raises (fail-loud).
  - DataValue.valueStatus is ignored by design (never populated in querySamples() results).
  - Columns are dense and index-aligned with the timestampList; a length mismatch is a fail-loud ValueError.
  - Serialized columns are not decoded (deferred); a ColumnTable carrying serializedDataColumns raises
    NotImplementedError.
"""

from typing import Any, Dict, List, Optional, Iterator
from dp_python_lib.grpc import common_pb2
from dp_python_lib.grpc import query_pb2


# Excel's hard row ceiling (1,048,576 rows including a header row).
_EXCEL_MAX_ROWS = 1_048_576 - 1

# Mapping from the Image.FileType enum number to its name, resolved once from the descriptor.
_IMAGE_FILE_TYPE_NAMES = {
    v.number: v.name
    for v in common_pb2.Image.DESCRIPTOR.fields_by_name["fileType"].enum_type.values
}


class Image:
    """
    A lossless, dependency-free wrapper for a common.Image DataValue arm: the raw image bytes plus the declared file
    type (e.g. "PNG", "JPEG").  Preserves the file type that a bare `bytes` would discard.
    """

    def __init__(self, data: bytes, file_type: str) -> None:
        """
        :param data: The raw image bytes.
        :param file_type: The image file type name (one of the common.Image.FileType enum names, e.g. "PNG").
        """
        self.data = data
        self.file_type = file_type

    def __repr__(self) -> str:
        return f"Image(file_type={self.file_type!r}, bytes={len(self.data)})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Image)
            and self.data == other.data
            and self.file_type == other.file_type
        )


def _require_pandas():
    """Imports and returns pandas, or raises an actionable error if the optional [analysis] extra is missing."""
    try:
        import pandas

        return pandas
    except ImportError as e:
        raise ImportError(
            "pandas is required for this conversion; install it with: pip install dp-python-lib[analysis]"
        ) from e


def _require_numpy():
    """Imports and returns numpy, or raises an actionable error if the optional [analysis] extra is missing."""
    try:
        import numpy

        return numpy
    except ImportError as e:
        raise ImportError(
            "numpy is required for this conversion; install it with: pip install dp-python-lib[analysis]"
        ) from e


# ----------------------------------------------------------------------
# DataValue -> Python value
# ----------------------------------------------------------------------

# The scalar oneof arms that map directly to a native Python value via getattr.
_SCALAR_ARMS = frozenset(
    {
        "stringValue",
        "booleanValue",
        "uintValue",
        "ulongValue",
        "intValue",
        "longValue",
        "floatValue",
        "doubleValue",
        "byteArrayValue",
    }
)


def _timestamp_to_epoch_nanos(ts: common_pb2.Timestamp) -> int:
    """Converts a common.Timestamp to an integer count of nanoseconds since the Unix epoch."""
    return ts.epochSeconds * 1_000_000_000 + ts.nanoseconds


def data_value_to_python(value: common_pb2.DataValue) -> Any:
    """
    Extracts the native Python value from a DataValue oneof.

    Scalars map to native types (str/bool/int/float/bytes); timestampValue maps to an integer epoch-nanoseconds
    count (assembled into a proper datetime column at the DataFrame level); the complex arms are preserved
    losslessly: arrayValue -> list, structureValue -> dict, imageValue -> Image.  A cleanly-unset oneof returns
    None.  An unhandled/unknown arm raises ValueError (fail-loud) rather than silently dropping data.

    :param value: The DataValue to extract.
    :return: The extracted Python value, or None if the oneof is unset.
    :raises ValueError: if the set oneof arm is not one this converter handles.
    """
    arm = value.WhichOneof("value")
    if arm is None:
        return None
    if arm in _SCALAR_ARMS:
        return getattr(value, arm)
    if arm == "timestampValue":
        return _timestamp_to_epoch_nanos(value.timestampValue)
    if arm == "arrayValue":
        return [data_value_to_python(v) for v in value.arrayValue.dataValues]
    if arm == "structureValue":
        return {
            field.name: data_value_to_python(field.value) for field in value.structureValue.fields
        }
    if arm == "imageValue":
        file_type = _IMAGE_FILE_TYPE_NAMES.get(
            value.imageValue.fileType, str(value.imageValue.fileType)
        )
        return Image(value.imageValue.image, file_type)
    raise ValueError(f"Unhandled DataValue arm: {arm!r}")


def _column_has_timestamp_arm(column: common_pb2.DataColumn) -> bool:
    """True if every set value in the column is a timestampValue (so the column becomes a datetime column)."""
    saw_value = False
    for v in column.dataValues:
        arm = v.WhichOneof("value")
        if arm is None:
            continue
        saw_value = True
        if arm != "timestampValue":
            return False
    return saw_value


# The oneof arms whose Python values are containers (list/dict/Image).  NumPy would either collapse these into a
# higher-dimensional array (uniform-length arrayValue) or raise (ragged), so such columns are always built as 1-D
# object arrays instead -- matching the DataFrame path, which keeps them as object-dtype cells.
_COMPLEX_ARMS = frozenset({"arrayValue", "structureValue", "imageValue"})


def _column_has_complex_arm(column: common_pb2.DataColumn) -> bool:
    """True if any set value in the column uses a complex (container-valued) oneof arm."""
    return any(v.WhichOneof("value") in _COMPLEX_ARMS for v in column.dataValues)


# ----------------------------------------------------------------------
# ColumnTable -> pandas DataFrame
# ----------------------------------------------------------------------


def _check_no_serialized_columns(column_table: query_pb2.ColumnTable) -> None:
    """Raises NotImplementedError if the ColumnTable carries serialized columns (deferred; not decoded)."""
    if len(column_table.serializedDataColumns) > 0:
        raise NotImplementedError(
            "serializedDataColumns are not supported yet; query with the default (dense) column representation"
        )


def _check_column_alignment(column: common_pb2.DataColumn, n_rows: int) -> None:
    """Raises ValueError if the column is not dense and index-aligned with the timestampList."""
    if len(column.dataValues) != n_rows:
        raise ValueError(
            f"column {column.name!r} has {len(column.dataValues)} values but the timestampList has "
            f"{n_rows}; dense index alignment is required"
        )


def _check_no_duplicate_column_names(column_table: query_pb2.ColumnTable) -> None:
    """
    Raises ValueError if two DataColumns share a name.  Both conversions key their output by column name, so a
    duplicate would silently overwrite the earlier column and drop a whole PV's data -- fail loud instead, matching
    the module's other invariants (dense alignment, unhandled oneof arms, serialized columns).
    """
    seen = set()
    duplicates = []
    for column in column_table.dataColumns:
        if column.name in seen and column.name not in duplicates:
            duplicates.append(column.name)
        seen.add(column.name)
    if duplicates:
        raise ValueError(
            f"ColumnTable contains duplicate DataColumn name(s): {sorted(duplicates)!r}; conversions key "
            f"columns by name and cannot represent duplicates without dropping data"
        )


def _column_metadata_dict(metadata: common_pb2.ColumnMetadata) -> Dict[str, Any]:
    """Flattens a ColumnMetadata into a plain dict (tags list + attribute name/value pairs)."""
    return {
        "tags": list(metadata.tags),
        "attributes": {attr.name: attr.value for attr in metadata.attributes},
    }


def column_table_to_dataframe(
    column_table: Optional[query_pb2.ColumnTable], exclude_column_metadata: bool = False
) -> Any:
    """
    Converts a ColumnTable into a pandas DataFrame: a UTC datetime index built from the timestampList, and one column
    per DataColumn (column label = DataColumn.name).

    Columns are assumed dense and index-aligned with the timestampList (the verified server contract); a length
    mismatch raises ValueError.  Timestamp-valued columns are rendered as datetime64[ns, UTC]; other scalar columns
    take their natural pandas dtype (integer columns with unset gaps upcast to float64 per pandas); complex arms are
    preserved as Python objects in object-dtype columns.

    When metadata is present and not excluded, per-column ColumnMetadata is attached to df.attrs["column_metadata"]
    as a {column_name: {"tags": [...], "attributes": {...}}} dict (df.attrs survives most pandas operations and keeps
    the DataFrame's cells purely data).

    :param column_table: The ColumnTable to convert (None yields an empty DataFrame).
    :param exclude_column_metadata: If True, do not attach ColumnMetadata to df.attrs.
    :return: A pandas.DataFrame.
    :raises ValueError: if any DataColumn length does not match the timestampList length, or if two DataColumns
        share a name (columns are keyed by name, so a duplicate cannot be represented without dropping data).
    :raises NotImplementedError: if the ColumnTable carries serializedDataColumns.
    """
    pd = _require_pandas()

    if column_table is None:
        return pd.DataFrame()

    _check_no_serialized_columns(column_table)
    _check_no_duplicate_column_names(column_table)

    timestamps = column_table.timestampList.timestamps
    n_rows = len(timestamps)
    index = pd.to_datetime(
        [_timestamp_to_epoch_nanos(ts) for ts in timestamps], utc=True, unit="ns"
    )

    data: Dict[str, Any] = {}
    metadata: Dict[str, Any] = {}
    for column in column_table.dataColumns:
        _check_column_alignment(column, n_rows)

        if _column_has_timestamp_arm(column):
            # Build a UTC datetime column from epoch-nanos, preserving None gaps as NaT.
            epoch_nanos = [data_value_to_python(v) for v in column.dataValues]
            data[column.name] = pd.to_datetime(
                pd.Series(epoch_nanos, index=index), utc=True, unit="ns"
            )
        else:
            data[column.name] = [data_value_to_python(v) for v in column.dataValues]

        if not exclude_column_metadata and column.HasField("metadata"):
            metadata[column.name] = _column_metadata_dict(column.metadata)

    df = pd.DataFrame(data, index=index)
    if metadata:
        df.attrs["column_metadata"] = metadata
    return df


# ----------------------------------------------------------------------
# ColumnTable -> NumPy
# ----------------------------------------------------------------------


def column_table_to_numpy(column_table: Optional[query_pb2.ColumnTable]) -> Dict[str, Any]:
    """
    Converts a ColumnTable into NumPy arrays: a dict of column name -> ndarray, plus a "timestamps" entry holding the
    datetime64[ns] index.  A dict-of-arrays is used (rather than a single structured/2-D array) so mixed and object
    dtypes are handled without forcing a common type.

    Every returned array is 1-D with one element per row.  Scalar columns take their natural inferred dtype;
    timestamp columns become datetime64[ns]; complex arms (arrayValue/structureValue/imageValue) are kept as
    Python objects in a 1-D object array, so an array-valued column never collapses into a 2-D array just because
    its rows happen to be equal-length.

    NOTE (future PyTorch support): this dict-of-ndarrays is the intended substrate for a future `torch` extra -- a
    column_table_to_torch() would wrap each numeric column via torch.from_numpy() (object/complex columns and the
    datetime index need a defined policy first).  PyTorch is deliberately NOT a dependency here; it would be added
    as a separate optional extra + conversion function, touching neither QueryClient nor this NumPy path.

    :param column_table: The ColumnTable to convert (None yields an empty dict).
    :return: A dict mapping "timestamps" and each column name to a 1-D numpy.ndarray.
    :raises ValueError: if any DataColumn length does not match the timestampList length, or if two DataColumns
        share a name (columns are keyed by name, so a duplicate cannot be represented without dropping data).
    :raises NotImplementedError: if the ColumnTable carries serializedDataColumns.
    """
    np = _require_numpy()

    if column_table is None:
        return {}

    _check_no_serialized_columns(column_table)
    _check_no_duplicate_column_names(column_table)

    timestamps = column_table.timestampList.timestamps
    n_rows = len(timestamps)

    result: Dict[str, Any] = {
        "timestamps": np.array(
            [_timestamp_to_epoch_nanos(ts) for ts in timestamps], dtype="datetime64[ns]"
        )
    }
    for column in column_table.dataColumns:
        _check_column_alignment(column, n_rows)
        values = [data_value_to_python(v) for v in column.dataValues]
        if _column_has_timestamp_arm(column):
            result[column.name] = np.array(values, dtype="datetime64[ns]")
        elif _column_has_complex_arm(column):
            # Decide by arm, not by exception: uniform-length arrayValues would otherwise succeed as a 2-D
            # array while ragged ones raised, making a column's shape depend on its data.  Always 1-D object.
            arr = np.empty(len(values), dtype=object)
            arr[:] = values
            result[column.name] = arr
        else:
            result[column.name] = np.array(values)
    return result


# ----------------------------------------------------------------------
# Excel export
# ----------------------------------------------------------------------


def _stringify_complex_cell(value: Any) -> Any:
    """
    Renders a complex DataFrame cell into an Excel-safe value: list/dict as JSON, Image via repr.  Everything else
    -- scalars, datetimes, and bytes from byteArrayValue -- is returned unchanged and left to openpyxl, which
    handles those cell types natively.  Deliberately no blanket repr() fallback: it would turn b'abc' into the
    string "b'abc'" and lose the value's type for no gain.
    """
    import json

    if isinstance(value, (list, dict)):
        return json.dumps(value, default=repr)
    if isinstance(value, Image):
        return repr(value)
    return value


def dataframe_to_excel(df: Any, path: str, max_rows: Optional[int] = None) -> None:
    """
    Writes a DataFrame to an Excel .xlsx file: a thin wrapper over df.to_excel() (openpyxl engine) that guards
    Excel's row ceiling and stringifies complex cells (Excel has no cell type for lists/dicts/images).

    Timezone-aware datetimes are written as naive UTC (Excel has no tz-aware cells); complex cells are stringified
    (JSON for list/dict, repr for Image).

    :param df: The DataFrame to write.
    :param path: Destination .xlsx path.
    :param max_rows: Optional caller-supplied row cap; if the frame exceeds it, raise before writing.
    :raises ValueError: if the frame exceeds max_rows, or exceeds Excel's own row ceiling.
    """
    pd = _require_pandas()

    n_rows = len(df)
    if max_rows is not None and n_rows > max_rows:
        raise ValueError(
            f"DataFrame has {n_rows} rows, exceeding the requested max_rows={max_rows}"
        )
    if n_rows > _EXCEL_MAX_ROWS:
        raise ValueError(
            f"DataFrame has {n_rows} rows, exceeding Excel's ceiling of {_EXCEL_MAX_ROWS}; "
            f"narrow the query range or export another format"
        )

    out = df.copy()

    # Drop tz info on datetime columns (Excel cannot store tz-aware datetimes); the values stay in UTC.
    if isinstance(out.index, pd.DatetimeIndex) and out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    for col in out.columns:
        if isinstance(out[col].dtype, pd.DatetimeTZDtype):
            out[col] = out[col].dt.tz_localize(None)
        elif out[col].dtype == object:
            out[col] = out[col].map(_stringify_complex_cell)

    out.to_excel(path, engine="openpyxl")


# ----------------------------------------------------------------------
# Whole-query conveniences (page internally)
# ----------------------------------------------------------------------


def query_samples_to_dataframe(
    query_client: Any, request_params: Any, max_rows: Optional[int] = None
) -> Any:
    """
    Runs a unary query, pages through the entire result via iter_query_samples(), and concatenates the pages into a
    single DataFrame.  Pages are concatenated on the row axis (axis=0) and aligned by column NAME (an outer join,
    tolerant of a column appearing/disappearing across pages -- see the live-metadata race note below).

    NOTE (live-metadata race): a pvNamePattern / metadataQuery selector re-resolves against the metadata store on each
    page's RPC, so a concurrent PV-metadata mutation mid-sequence can shift the column set.  The outer-join concat here
    tolerates that; if a hard column guarantee is needed, resolve the pattern to an explicit pvNameList first and query
    with that.

    :param query_client: A QueryClient.
    :param request_params: The QueryParams describing the query.
    :param max_rows: Optional total row cap across all pages; raise once the accumulated frame would exceed it
        (unbounded "give me everything" is an OOM foot-gun on large ranges).
    :return: A single pandas.DataFrame spanning all pages.
    :raises ValueError: if the accumulated result exceeds max_rows.
    :raises RuntimeError: if any page returns an error (propagated from iter_query_samples()).
    """
    pd = _require_pandas()

    frames: List[Any] = []
    total = 0
    for page in query_client.iter_query_samples(request_params):
        frame = column_table_to_dataframe(
            page.column_table, exclude_column_metadata=request_params.exclude_column_metadata
        )
        total += len(frame)
        if max_rows is not None and total > max_rows:
            raise ValueError(
                f"query result exceeds max_rows={max_rows} (at least {total} rows); "
                f"narrow the range or raise max_rows"
            )
        frames.append(frame)

    if not frames:
        return pd.DataFrame()
    # Outer-join on columns by name; sort=False keeps first-seen column order.
    return pd.concat(frames, axis=0, join="outer", sort=False)


def stream_query_samples_to_dataframes(query_client: Any, request_params: Any) -> Iterator[Any]:
    """
    Runs a server-streaming query and yields one DataFrame per streamed message (lazy; the stream is NOT concatenated,
    preserving streaming's bounded-memory benefit).  Callers who want the whole result use pd.concat(...) or the unary
    query_samples_to_dataframe().

    :param query_client: A QueryClient.
    :param request_params: The QueryParams describing the query.
    :return: A lazy iterator of pandas.DataFrame, one per streamed page.
    :raises RuntimeError: on a mid-stream error (propagated from iter_query_samples_stream()).
    """
    for page in query_client.iter_query_samples_stream(request_params):
        yield column_table_to_dataframe(
            page.column_table, exclude_column_metadata=request_params.exclude_column_metadata
        )
