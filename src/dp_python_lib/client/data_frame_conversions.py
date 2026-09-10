"""
Pythonic conversions for common.DataFrame (issue #6, Phase 2 and 3).

Reads a DataFrame -- an annotation's calculations, and in future a bucket query's typed columns -- back into plain
Python, and, behind the optional [analysis] extra, into pandas.

The pure-Python half has no third-party dependencies.  pandas is imported lazily inside each entry point that needs
it, so importing this module never requires the extra.

Design decisions (see plan/tickets/6/plan.md, D7):
  - Timestamps are computed in INTEGER NANOSECONDS via expand_data_timestamps(), never in float seconds.  A float64
    carries 53 bits of mantissa and present-day epoch nanoseconds need ~61, so a float round-trip would silently
    move every timestamp.  The pandas index is built from those int64 nanoseconds directly for the same reason.
  - column_values() is written as a standalone per-column converter because the bucket query (#16) reuses these
    same 14 typed column messages; it takes one column and needs to know nothing about the frame.
  - Array columns are reshaped into one list per sample using their declared dims, rather than returned flat: a
    flat list would silently lose the sample boundaries.
  - Dense typed columns cannot express a gap, so the pandas->DataFrame direction rejects NaN/None fail-loud with a
    message pointing at the two ways to express one (a separate frame, or data_column()).
"""

from typing import Any

from dp_python_lib.client.query_conversions import data_value_to_python
from dp_python_lib.client.sample_status_conversions import expand_data_timestamps
from dp_python_lib.client.time_conversions import to_epoch_nanos
from dp_python_lib.grpc import annotation_pb2, common_pb2

# The DataFrame fields holding typed scalar columns, in the proto's declaration order.  Each carries `values`
# directly parallel to the time axis.
_SCALAR_COLUMN_FIELDS = (
    "doubleColumns",
    "floatColumns",
    "int64Columns",
    "int32Columns",
    "boolColumns",
    "stringColumns",
    "enumColumns",
    "structColumns",
)

# The DataFrame fields holding array columns, whose `values` are flat (samples x prod(dims)).
_ARRAY_COLUMN_FIELDS = (
    "doubleArrayColumns",
    "floatArrayColumns",
    "int32ArrayColumns",
    "int64ArrayColumns",
    "boolArrayColumns",
)

# ImageColumn stores its per-sample payloads in `images` rather than `values`.
_IMAGE_COLUMN_FIELD = "imageColumns"

# The array column message types, whose flat values are delimited by their declared dims.
_ARRAY_COLUMN_MESSAGE_TYPES = (
    common_pb2.DoubleArrayColumn,
    common_pb2.FloatArrayColumn,
    common_pb2.Int32ArrayColumn,
    common_pb2.Int64ArrayColumn,
    common_pb2.BoolArrayColumn,
)


def _require_pandas():
    """Imports and returns pandas, or raises an actionable error if the optional [analysis] extra is missing."""
    try:
        import pandas
    except ImportError as e:
        raise ImportError(
            "pandas is required for DataFrame conversions.  Install the optional analysis extra: "
            'pip install "dp-python-lib[analysis]"'
        ) from e
    return pandas


def data_frame_timestamps(frame: common_pb2.DataFrame) -> list[int]:
    """
    Expands a frame's time axis into one integer epoch-nanosecond value per sample.

    A SamplingClock is expanded arithmetically in integer nanoseconds, so the result reproduces the producer's
    timestamps exactly; see expand_data_timestamps().

    :param frame: The DataFrame whose axis to expand.
    :return: Epoch nanoseconds for each sample, in axis order.
    :raises ValueError: if the frame has no time axis, or the axis is empty or malformed.
    """
    if not frame.HasField("dataTimestamps"):
        raise ValueError("DataFrame has no dataTimestamps; every frame must carry a time axis")

    epoch_nanos = expand_data_timestamps(frame.dataTimestamps)
    if not epoch_nanos:
        # expand_data_timestamps() returns [] for a set-but-empty timestampList, which reports its oneof arm as
        # set.  Rejecting it here honors this function's documented contract and matches timestamp_count() on the
        # write side; otherwise a corrupt frame converts to a zero-row table instead of failing loudly.
        raise ValueError("DataFrame time axis describes no timestamps; every frame must cover at least one sample")
    return epoch_nanos


def _reshape_array_values(column: Any) -> list[list]:
    """
    Reshapes an array column's flat values into one list per sample, using its declared dimensions.

    :param column: An array column (DoubleArrayColumn, Int32ArrayColumn, ...).
    :return: One list of values per sample; multi-dimensional arrays stay flat WITHIN a sample.
    :raises ValueError: if dims are missing/zero, or the value count is not a whole multiple of prod(dims).
    """
    # Absent dims are underivable rather than an empty product of 1, which would silently make every element its
    # own sample.
    dims = list(column.dimensions.dims)
    product = 1
    for dim in dims:
        product *= dim
    if not dims or product <= 0:
        raise ValueError(
            f"array column '{column.name}' has missing or zero dimensions, so its samples cannot be delimited"
        )

    values = list(column.values)
    if len(values) % product != 0:
        raise ValueError(
            f"array column '{column.name}' has {len(values)} values, which is not a whole multiple of its "
            f"per-sample size {product} (from dims {list(column.dimensions.dims)})"
        )
    return [values[i : i + product] for i in range(0, len(values), product)]


def column_dimensions(column: Any) -> list[int] | None:
    """
    Returns an array column's declared dimensions, or None for any other column kind.

    column_values() reshapes an array column into one flat list per sample, which delimits the samples but does
    not preserve the shape WITHIN one: a 2x2 sample and a 4-element sample both come back as four values.  Plan
    D7 calls for the dims to travel alongside those values, and this is the accessor that supplies them, kept
    separate so column_values()'s "one entry per sample" contract stays uniform across all column kinds.

    :param column: Any column message.
    :return: The dims as a list of ints for an array column, or None if the column is not an array column.
    """
    if not isinstance(column, _ARRAY_COLUMN_MESSAGE_TYPES):
        return None
    return list(column.dimensions.dims)


def data_frame_column_dimensions(frame: common_pb2.DataFrame) -> dict[str, list[int]]:
    """
    Returns the declared dimensions of every array column in a frame, keyed by column name.

    Pairs with data_frame_columns(): that gives one flat list per sample, this gives the shape those values have.
    Non-array columns are absent from the result rather than mapped to None, so a caller can test membership.

    :param frame: The DataFrame to inspect.
    :return: A dict mapping each array column's name to its dims; empty when the frame has no array columns.
    """
    dimensions: dict[str, list[int]] = {}
    for column in iter_frame_columns(frame):
        dims = column_dimensions(column)
        if dims is not None:
            dimensions[column.name] = dims
    return dimensions


def column_values(column: Any) -> list:
    """
    Extracts one Python value per sample from any supported column message.

    Written as a standalone converter because the bucket query (#16) carries the same 14 typed column messages and
    can reuse this without going through a DataFrame.

    Mapping: typed scalar columns yield their native values; an EnumColumn yields its integer codes (the enumeration
    naming them is `enumId` on the column); an array column yields one list per sample; an ImageColumn yields one
    bytes payload per sample; and a legacy DataColumn is converted per value by data_value_to_python(), so an unset
    oneof becomes None -- the only representation of a gap in this API.

    An array column's per-sample list is flat: the dims that give it shape are available separately from
    column_dimensions(), so that every column kind here yields exactly one entry per sample.

    :param column: A typed column, a legacy DataColumn, or an ImageColumn.
    :return: One value per sample, in axis order.
    :raises ValueError: if the column type is unsupported, or an array column's dims do not divide its values.
    """
    if isinstance(column, common_pb2.DataColumn):
        return [data_value_to_python(value) for value in column.dataValues]
    if isinstance(column, common_pb2.ImageColumn):
        return list(column.images)
    if isinstance(column, _ARRAY_COLUMN_MESSAGE_TYPES):
        return _reshape_array_values(column)
    if hasattr(column, "values"):
        return list(column.values)
    raise ValueError(f"unsupported column type for value extraction: {type(column).__name__}")


def iter_frame_columns(frame: common_pb2.DataFrame):
    """
    Yields every column in a frame, across all of its repeated fields, in a stable order.

    Serialized columns are skipped: their payloads are opaque encoded blobs that this library does not decode
    (deferred, as in query_conversions).  A caller needing them can read frame.serializedDataColumns directly.

    :param frame: The DataFrame to walk.
    :return: An iterator over the frame's column messages.
    """
    for field in (*_SCALAR_COLUMN_FIELDS, _IMAGE_COLUMN_FIELD, *_ARRAY_COLUMN_FIELDS):
        yield from getattr(frame, field)
    yield from frame.dataColumns


def data_frame_columns(frame: common_pb2.DataFrame) -> dict[str, list]:
    """
    Converts every column in a frame into a dict of column name -> one Python value per sample.

    Column names are unique across all types within a frame (the server enforces it on save, and data_frame()
    checks it on the way out), so keying by name is lossless.  A frame that violates it anyway -- hand-built, or
    from an older server -- raises rather than silently dropping the earlier column.

    :param frame: The DataFrame to convert.
    :return: A dict mapping column name to its per-sample values.
    :raises ValueError: if two columns share a name, or a column cannot be converted.
    """
    columns: dict[str, list] = {}
    for column in iter_frame_columns(frame):
        if column.name in columns:
            raise ValueError(
                f"DataFrame carries more than one column named '{column.name}'; names must be unique across all "
                f"column types within a frame"
            )
        columns[column.name] = column_values(column)
    return columns


def _column_source_dict(entry: common_pb2.ColumnProvenance.ColumnSource) -> dict[str, Any]:
    """
    Summarizes one ColumnProvenance.ColumnSource -- where a column's values came from -- as a plain dict.

    Only the origin arm actually set is present: a pvName source has no 'calculations_column' key and vice versa,
    rather than the unset arm standing in as an empty string.  Absence means "not this arm", the same absent-vs-
    empty discipline the sample status conversions apply to confidence and reason.

    The optional timeRange -- which part of the source was used -- is reported as epoch nanoseconds, matching this
    module's integer-nanosecond convention.  It is the substantive half of provenance for a derived column: the
    source PV without its window says much less than the pair does.

    :param entry: The ColumnSource to summarize.
    :return: A dict carrying the set origin arm and, when present, 'time_range' as (begin_nanos, end_nanos).
    """
    result: dict[str, Any] = {}

    origin = entry.WhichOneof("origin")
    if origin == "pvName":
        result["pv_name"] = entry.pvName
    elif origin == "calculationsColumn":
        result["calculations_column"] = {
            "calculations_id": entry.calculationsColumn.calculationsId,
            "frame_name": entry.calculationsColumn.frameName,
            "column_name": entry.calculationsColumn.columnName,
        }

    if entry.HasField("timeRange"):
        result["time_range"] = (
            to_epoch_nanos(entry.timeRange.beginTime),
            to_epoch_nanos(entry.timeRange.endTime),
        )
    return result


def column_metadata_dict(column: Any) -> dict[str, Any]:
    """
    Summarizes a column's ColumnMetadata as a plain dict, for carrying alongside converted values.

    :param column: A column message that may have a `metadata` field.
    :return: A dict with 'tags', 'attributes', and 'provenance' keys; empty containers when unset.
    """
    metadata = getattr(column, "metadata", None)
    if metadata is None:
        return {"tags": [], "attributes": {}, "provenance": None}

    provenance = None
    if metadata.HasField("provenance"):
        source = metadata.provenance
        provenance = {
            "source": source.source,
            "process": source.process,
            "derived_from": [_column_source_dict(entry) for entry in source.derivedFrom],
        }

    return {
        "tags": list(metadata.tags),
        "attributes": {attribute.name: attribute.value for attribute in metadata.attributes},
        "provenance": provenance,
    }


def _check_frame_alignment(frame: common_pb2.DataFrame, columns: dict[str, list], n_rows: int) -> None:
    """
    Rejects a frame whose columns do not all match its time axis.

    The server validates this on save, so a mismatch here means a hand-built frame or a corrupt record; failing
    loudly beats producing a silently truncated or NaN-padded table.

    :param frame: The frame being converted, named in the error message.
    :param columns: The already-converted columns.
    :param n_rows: The number of timestamps on the axis.
    :raises ValueError: if any column's length differs from n_rows.
    """
    for name, values in columns.items():
        if len(values) != n_rows:
            raise ValueError(
                f"DataFrame column '{name}' has {len(values)} values but the time axis has {n_rows} timestamps; "
                f"columns must be dense and index-aligned with the axis"
            )


# The protobuf column types whose pandas dtype is narrower than what inference from a plain Python list yields.
# Double/Int64/Bool are listed too so the mapping is explicit rather than half-stated.
_NARROW_DTYPE_BY_COLUMN_TYPE = {
    common_pb2.FloatColumn: "float32",
    common_pb2.Int32Column: "int32",
    common_pb2.DoubleColumn: "float64",
    common_pb2.Int64Column: "int64",
    common_pb2.BoolColumn: "bool",
    # EnumColumn's values field is int32 too; without this its codes widen to int64 and it rebuilds as an
    # Int64Column.  The enumId that gives those codes meaning travels separately -- see _enum_ids().
    common_pb2.EnumColumn: "int32",
}


def _narrow_column_dtypes(frame: common_pb2.DataFrame) -> dict[str, str]:
    """
    Maps each scalar column's name to the pandas dtype its protobuf type implies.

    Only the columns whose type pins a dtype appear; strings, arrays, images, and DataColumns are absent, so the
    caller leaves those to pandas' inference.

    :param frame: The frame whose columns to inspect.
    :return: A dict of column name -> pandas dtype string.
    """
    return {
        column.name: _NARROW_DTYPE_BY_COLUMN_TYPE[type(column)]
        for column in iter_frame_columns(frame)
        if type(column) in _NARROW_DTYPE_BY_COLUMN_TYPE
    }


def _enum_ids(frame: common_pb2.DataFrame) -> dict[str, str]:
    """
    Maps each EnumColumn's name to its enumId.

    An enum column's values are bare integer codes; the enumId is what says which enumeration names them, so
    dropping it would leave the codes meaningless.  It has nowhere to live in a pandas Series, so it rides in
    df.attrs["enum_ids"] and is consulted when rebuilding the column.

    :param frame: The frame whose enum columns to inspect.
    :return: A dict of column name -> enumId; empty when the frame has no enum columns.
    """
    return {column.name: column.enumId for column in frame.enumColumns}


def data_frame_to_pandas(frame: common_pb2.DataFrame, exclude_column_metadata: bool = False) -> Any:
    """
    Converts a DataFrame into a pandas DataFrame with a UTC DatetimeIndex.

    Requires the optional [analysis] extra.

    The index is built from int64 epoch nanoseconds directly, so a SamplingClock axis stays exact -- routing
    through float seconds would move present-day timestamps by hundreds of nanoseconds.  Per-column ColumnMetadata
    lands in df.attrs["column_metadata"] (a dict keyed by column name), matching query_conversions' convention.

    Note columns come back GROUPED BY TYPE, not in their original order: a DataFrame stores each column kind in its
    own repeated field, so the wire format does not preserve a single ordering across kinds.  A round trip through
    data_frame_from_pandas() therefore preserves every value, dtype, and timestamp, but can reorder the columns
    (float64, then int, then bool, then string, matching the proto's field order).  Select by name, or reindex to
    the order you want.

    :param frame: The DataFrame to convert.
    :param exclude_column_metadata: When True, skip populating df.attrs["column_metadata"].
    :return: A pandas.DataFrame indexed by UTC timestamp.
    :raises ImportError: if the [analysis] extra is not installed.
    :raises ValueError: if the frame's axis is missing/empty, or a column is not aligned with it.
    """
    pd = _require_pandas()

    epoch_nanos = data_frame_timestamps(frame)
    columns = data_frame_columns(frame)
    _check_frame_alignment(frame, columns, len(epoch_nanos))

    index = pd.DatetimeIndex(pd.to_datetime(pd.Series(epoch_nanos, dtype="int64"), unit="ns", utc=True))

    # Build each Series with the dtype its protobuf column type implies.  Handing pandas an untyped list widens
    # FloatColumn to float64 and Int32Column to int64, so a frame -> pandas -> frame round trip would come back
    # as DoubleColumn/Int64Column.  Columns with no narrow equivalent (strings, arrays, images, DataColumn) are
    # left to pandas' own inference.
    narrow_dtypes = _narrow_column_dtypes(frame)
    df = pd.DataFrame(
        {
            name: pd.Series(values, index=index, dtype=narrow_dtypes[name]) if name in narrow_dtypes else values
            for name, values in columns.items()
        },
        index=index,
    )

    # enum_ids is structural, not metadata: without it an enum column cannot be rebuilt as one at all, so it is
    # carried even when exclude_column_metadata drops the descriptive attrs.
    enum_ids = _enum_ids(frame)
    if enum_ids:
        df.attrs["enum_ids"] = enum_ids

    if not exclude_column_metadata:
        df.attrs["column_metadata"] = {
            column.name: column_metadata_dict(column) for column in iter_frame_columns(frame)
        }
    return df


def _timestamps_from_index(index: Any) -> common_pb2.DataTimestamps:
    """
    Builds a DataTimestamps from a pandas DatetimeIndex, in integer nanoseconds throughout.

    Always emits a TimestampList rather than trying to detect a regular interval and emit a SamplingClock.  A clock
    is only correct if the spacing is exactly uniform at nanosecond precision, and inferring that from an index
    that merely looks regular would quietly change the timestamps -- the one thing this API cannot tolerate.  Build
    a clock explicitly with sampling_clock() when that is what the data is.

    Each instant is read as Timestamp.value, which is nanoseconds whatever the index's own storage unit; a raw
    int64 view is in that unit, so a datetime64[us] index would land 1000x too early.  NaT is rejected rather than
    passed through, since its integer form is a valid-looking instant.

    :param index: A pandas DatetimeIndex.
    :return: A DataTimestamps carrying a TimestampList.
    :raises ValueError: if the index is empty, not a DatetimeIndex, or not strictly increasing.
    """
    pd = _require_pandas()

    if not isinstance(index, pd.DatetimeIndex):
        raise ValueError(
            f"DataFrame must be indexed by a pandas DatetimeIndex to become a time axis, got {type(index).__name__}"
        )
    if len(index) == 0:
        raise ValueError("DataFrame must have at least one row to become a DataFrame with a time axis")

    # tz-naive input is ambiguous: it could be UTC or local.  Require the caller to say, as to_timestamp() does.
    if index.tz is None:
        raise ValueError(
            "DataFrame index must be timezone-aware so its instants are unambiguous; localize it first, "
            "e.g. df.index = df.index.tz_localize('UTC')"
        )

    # NaT has an integer representation (the int64 minimum), so a raw integer view would serialize it as a real
    # instant.  Reject it before converting: an absent timestamp is not a time axis position.
    if index.isna().any():
        missing = [int(position) for position in index.isna().nonzero()[0][:5]]
        raise ValueError(
            f"DataFrame index contains NaT at row position(s) {missing}; every row must have a real timestamp to "
            f"become a time axis.  Drop those rows, or supply the timestamps they should carry."
        )

    # Timestamp.value is always nanoseconds regardless of the index's own storage unit.  A raw int64 view is NOT:
    # pandas 2+ keeps second, millisecond, and microsecond resolutions, and viewing a datetime64[us] index as
    # int64 yields microseconds -- silently placing every instant 1000x too early.
    epoch_nanos = [entry.value for entry in index]

    timestamps = common_pb2.DataTimestamps()
    previous = None
    for position, nanos in enumerate(epoch_nanos):
        if previous is not None and nanos <= previous:
            raise ValueError(
                f"DataFrame index must be strictly increasing to become a time axis; entry {position} "
                f"({nanos} ns) does not follow entry {position - 1} ({previous} ns)"
            )
        previous = nanos
        timestamp = timestamps.timestampList.timestamps.add()
        timestamp.epochSeconds, timestamp.nanoseconds = divmod(nanos, 1_000_000_000)
    return timestamps


def _timestamp_from_nanos(epoch_nanos: int) -> common_pb2.Timestamp:
    """
    Builds a common.Timestamp from integer epoch nanoseconds -- the inverse of to_epoch_nanos().

    :param epoch_nanos: Epoch nanoseconds.
    :return: The equivalent common.Timestamp.
    """
    timestamp = common_pb2.Timestamp()
    timestamp.epochSeconds, timestamp.nanoseconds = divmod(epoch_nanos, 1_000_000_000)
    return timestamp


def column_metadata_from_dict(summary: dict[str, Any] | None) -> common_pb2.ColumnMetadata | None:
    """
    Rebuilds a ColumnMetadata from the dict column_metadata_dict() produced -- the inverse of that function.

    This is what lets a frame survive a pandas round trip with its provenance intact: data_frame_to_pandas() parks
    each column's metadata in df.attrs["column_metadata"], and data_frame_from_pandas() feeds it back through
    here.  Without it the attrs were carried and then silently dropped, losing exactly the record of where the
    numbers came from that makes calculations worth storing.

    :param summary: A dict as returned by column_metadata_dict(), or None.
    :return: The equivalent ColumnMetadata, or None when the summary is absent or carries nothing.
    """
    if not summary:
        return None

    tags = summary.get("tags") or []
    attributes = summary.get("attributes") or {}
    provenance_summary = summary.get("provenance")
    if not tags and not attributes and not provenance_summary:
        return None

    metadata = common_pb2.ColumnMetadata()
    if tags:
        metadata.tags[:] = list(tags)
    for name, value in attributes.items():
        attribute = metadata.attributes.add()
        attribute.name = name
        attribute.value = value

    if provenance_summary:
        provenance = metadata.provenance
        if provenance_summary.get("source"):
            provenance.source = provenance_summary["source"]
        if provenance_summary.get("process"):
            provenance.process = provenance_summary["process"]
        for entry in provenance_summary.get("derived_from") or []:
            source = provenance.derivedFrom.add()
            if "pv_name" in entry:
                source.pvName = entry["pv_name"]
            elif "calculations_column" in entry:
                column = entry["calculations_column"]
                source.calculationsColumn.calculationsId = column["calculations_id"]
                source.calculationsColumn.frameName = column["frame_name"]
                source.calculationsColumn.columnName = column["column_name"]
            time_range = entry.get("time_range")
            if time_range is not None:
                begin_nanos, end_nanos = time_range
                source.timeRange.beginTime.CopyFrom(_timestamp_from_nanos(begin_nanos))
                source.timeRange.endTime.CopyFrom(_timestamp_from_nanos(end_nanos))

    return metadata


def _column_from_series(
    name: str, series: Any, metadata: common_pb2.ColumnMetadata | None = None, enum_id: str | None = None
) -> Any:
    """
    Builds the typed column matching a pandas Series' dtype.

    Mapping: float64 -> DoubleColumn, float32 -> FloatColumn, int64 -> Int64Column, int32 -> Int32Column,
    bool -> BoolColumn, and object/string -> StringColumn.  Anything else raises rather than guessing.

    An enum_id routes an integer column to EnumColumn instead, since an enum's values are indistinguishable from
    plain int32 codes by dtype alone -- the id is what makes it an enumeration.

    A missing value anywhere is a fail-loud error: the typed columns are dense repeated fields with no way to mark
    an absent entry, so a NaN would have to be either invented as a real value or silently dropped.

    :param name: The column's name.
    :param series: The pandas Series holding its values.
    :param metadata: Optional ColumnMetadata to attach (see column_metadata_from_dict()).
    :param enum_id: When set, build an EnumColumn carrying this enumeration id rather than an integer column.
    :return: The matching typed column message.
    :raises ValueError: if the dtype has no mapping, or the series contains a missing value.
    """
    from dp_python_lib.client import data_frame as builders

    if series.isna().any():
        missing_positions = [int(position) for position in series.isna().to_numpy().nonzero()[0][:5]]
        raise ValueError(
            f"column '{name}' contains missing values (first at row position(s) {missing_positions}), which a "
            f"dense typed column cannot represent.  Either give the sparse column its own frame over only the "
            f"timestamps where it has values, or build it with data_frame.data_column(), whose DataValues can be "
            f"left unset."
        )

    dtype = series.dtype
    dtype_name = str(dtype)

    if enum_id is not None:
        # Checked before EVERY dtype branch, not just the integer ones: an EnumColumn's values are int32 codes, so
        # dtype alone cannot distinguish it from a plain Int32Column, and a non-integer dtype paired with an enum
        # id is a caller error worth naming rather than quietly building the wrong column kind.
        if dtype_name not in ("int64", "Int64", "int32", "Int32"):
            raise ValueError(
                f"column '{name}' carries an enum id ({enum_id!r}) but has dtype {dtype_name}; an EnumColumn's "
                f"values must be integer codes"
            )
        return builders.enum_column(name, [int(v) for v in series], enum_id, metadata=metadata)

    if dtype_name == "float64":
        return builders.double_column(name, [float(v) for v in series], metadata=metadata)
    if dtype_name == "float32":
        return builders.float_column(name, [float(v) for v in series], metadata=metadata)
    if dtype_name in ("int64", "Int64"):
        return builders.int64_column(name, [int(v) for v in series], metadata=metadata)
    if dtype_name in ("int32", "Int32"):
        return builders.int32_column(name, [int(v) for v in series], metadata=metadata)
    if dtype_name in ("bool", "boolean"):
        return builders.bool_column(name, [bool(v) for v in series], metadata=metadata)
    if dtype_name in ("object", "string", "str") or dtype_name.startswith("string"):
        values = list(series)
        if not all(isinstance(value, str) for value in values):
            offending = next(
                (type(value).__name__ for value in values if not isinstance(value, str)),
                "unknown",
            )
            raise ValueError(
                f"column '{name}' has dtype {dtype_name} but holds a non-string value (type {offending}); "
                f"object columns are mapped to StringColumn, so convert the values first or build the column "
                f"explicitly with a data_frame builder"
            )
        return builders.string_column(name, values, metadata=metadata)

    raise ValueError(
        f"column '{name}' has dtype {dtype_name}, which has no typed-column mapping.  Supported dtypes are "
        f"float64, float32, int64, int32, bool, and object/string; build other kinds explicitly with the "
        f"data_frame builders and pass them to data_frame()."
    )


def data_frame_from_pandas(df: Any) -> common_pb2.DataFrame:
    """
    Converts a pandas DataFrame into a common.DataFrame, mapping each column's dtype to a typed column.

    Requires the optional [analysis] extra.

    The index becomes an explicit TimestampList; see _timestamps_from_index() for why a SamplingClock is never
    inferred.  Missing values are rejected fail-loud, since a dense typed column cannot express a gap.

    Per-column metadata is read back from df.attrs["column_metadata"] when present, so a frame that went out
    through data_frame_to_pandas() returns with its tags, attributes, and provenance intact.  Enum columns are
    rebuilt as enum columns via df.attrs["enum_ids"]; without that id an integer column is just an integer column.

    Converting back with data_frame_to_pandas() preserves every value, dtype, and timestamp, but can return the
    columns grouped by type rather than in their original order -- see that function's docstring.

    :param df: The pandas DataFrame to convert.  Must have a tz-aware, strictly increasing DatetimeIndex.
    :return: A common.DataFrame.
    :raises ImportError: if the [analysis] extra is not installed.
    :raises ValueError: if the index is unusable, the frame has no columns, a dtype has no mapping, or any column
        contains a missing value.
    """
    from dp_python_lib.client import data_frame as builders

    _require_pandas()

    if len(df.columns) == 0:
        raise ValueError("DataFrame must have at least one column")

    # Duplicate labels make df[name] a DataFrame rather than a Series, which would otherwise surface far downstream
    # as pandas' "truth value of a Series is ambiguous".  Column names must be unique within a frame anyway, so
    # reject it here with a message naming the offender -- concat() and merge() produce this easily by accident.
    names = [str(name) for name in df.columns]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(
            f"DataFrame has more than one column named {', '.join(repr(name) for name in duplicates)}; column "
            f"names must be unique within a frame.  Rename or drop the duplicates first (e.g. after a concat or "
            f"a merge with overlapping names)."
        )

    # data_frame_to_pandas() parks each column's ColumnMetadata here; carrying it back is what keeps provenance
    # alive across a round trip.  A frame built by hand simply has no attrs, and every column gets None.
    metadata_by_column = df.attrs.get("column_metadata") or {}
    enum_ids = df.attrs.get("enum_ids") or {}

    data_timestamps = _timestamps_from_index(df.index)
    columns = [
        _column_from_series(
            str(name),
            df[name],
            metadata=column_metadata_from_dict(metadata_by_column.get(str(name))),
            enum_id=enum_ids.get(str(name)),
        )
        for name in df.columns
    ]
    return builders.data_frame(data_timestamps, columns)


def calculations_from_dataframes(frames: dict[str, Any]) -> annotation_pb2.Calculations:
    """
    Converts a mapping of frame name -> pandas DataFrame into a Calculations payload.

    Requires the optional [analysis] extra.

    :param frames: Mapping of frame name to the pandas DataFrame holding that frame's columns.
    :return: An annotation.Calculations.
    :raises ImportError: if the [analysis] extra is not installed.
    :raises ValueError: if frames is empty, a frame name is empty, or any frame cannot be converted.
    """
    from dp_python_lib.client.annotations_client import calculations as build_calculations

    _require_pandas()

    if not frames:
        raise ValueError("calculations_from_dataframes() requires at least one frame")

    return build_calculations({name: data_frame_from_pandas(df) for name, df in frames.items()})


def calculations_to_dataframes(
    calculations: annotation_pb2.Calculations | None,
    exclude_column_metadata: bool = False,
) -> dict[str, Any]:
    """
    Converts every frame of a Calculations into a pandas DataFrame, keyed by frame name.

    Requires the optional [analysis] extra.

    :param calculations: The Calculations to convert, or None.
    :param exclude_column_metadata: When True, skip populating each frame's df.attrs["column_metadata"].
    :return: A dict mapping frame name to its pandas DataFrame; empty when calculations is None or has no frames.
    :raises ImportError: if the [analysis] extra is not installed.
    :raises ValueError: if two frames share a name, or a frame cannot be converted.
    """
    _require_pandas()

    if calculations is None:
        return {}

    frames: dict[str, Any] = {}
    for entry in calculations.calculationDataFrames:
        if entry.name in frames:
            raise ValueError(f"Calculations carries more than one frame named '{entry.name}'; frame names are unique")
        frames[entry.name] = data_frame_to_pandas(entry.frame, exclude_column_metadata=exclude_column_metadata)
    return frames
