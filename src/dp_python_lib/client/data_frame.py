"""
Builders for common.DataFrame -- the shared time-series payload shape (issue #6, Phase 2; extended by issue #17).

A DataFrame is one time axis plus a set of columns sampled on it.  It is the payload of an annotation's
Calculations, and it is also ingestion's `ingestionDataFrame`: the same message, so this module is the substrate
issue #17 extended rather than a parallel one to fork.

Design decisions (see plan/tickets/6/plan.md, D6, and plan/tickets/17/plan.md, D1/D7/D8):
  - The time axis builders sampling_clock() / timestamp_list() live here now that a second caller exists; they are
    re-exported from sample_status_client so existing imports keep working.
  - The axis carries its own sample count and every column is validated against it.  That restates a number the
    caller already has (sampling_clock(t0, period, count=len(values))), which is deliberate: deriving the count
    from the columns would fork the axis API by caller, since sample_status_client genuinely knows its count
    independently of any column.  SampleStatusFrame set the same precedent -- explicit axis, per-column validation.
  - Validation mirrors the server's SHAPE rules only (non-blank names, non-empty values, count match, column-name
    uniqueness across types, and each column kind's structural fields: an enum's enumId, an array's 1-3 dims, an
    image's descriptor, a struct's schemaId, a serialized column's encoding), so an error names the offending frame
    or column instead of bouncing the whole batch.  The structural rules live in _check_column(), not only in the
    builders, because a hand-built column bypasses every builder; validate_data_frame() applies the same checks to
    a whole frame, however it was made.  The server's resource caps -- 256-char strings, 10M-element arrays,
    50 MB images, 1 MB structs -- stay server-side: they are deployment policy, and duplicating numbers that can
    change is how clients drift.
  - Two rules are stricter than the server's, deliberately: blank (whitespace-only) names and duplicate timestamps
    are rejected, though ingestion accepts both.  Anything data_frame() accepts must be readable back, the read
    path rejects duplicate timestamps, and sample status matching assumes one sample per instant.
  - split_data_frame() is the exception to "no deployment caps here": its whole purpose is to fit the server's
    inbound message limit and bucket span cap, which a large frame hits at once (a 4 MB message holds roughly 500k
    doubles; a day of one 10 kHz PV is 864M).  So the caller names each limit explicitly, and
    SERVER_DEFAULT_MAX_MESSAGE_BYTES is exported as a documented reference value, not applied as a default.
"""

from collections.abc import Iterator, Sequence
from numbers import Integral, Real
from typing import Any

from dp_python_lib.client.time_conversions import TimestampInput, from_epoch_nanos, to_epoch_nanos, to_timestamp
from dp_python_lib.grpc import common_pb2, ingestion_pb2

# dp-service's default inbound gRPC message limit (common/server/GrpcServerBase.java, configurable as
# GrpcServer.incomingMessageSizeLimitBytes).  A documented reference for split_data_frame(max_bytes=...), not a
# default: a deployment can change it, and a chunker that silently assumed it would drift with it.
SERVER_DEFAULT_MAX_MESSAGE_BYTES = 4_096_000

# The longest providerId / clientRequestId split_data_frame() budgets for.  The chunker never sees the ids its
# chunks will be sent with, so it reserves room for two ids of this many characters at the UTF-8 worst case of
# 4 bytes each; IngestDataRequestParams rejects anything longer, so no request it builds can exceed the budget.
MAX_BUDGETED_ID_CHARS = 256

# Array columns carry 1-3 dimensions (the server rejects any other count).
_MAX_ARRAY_DIMS = 3

# Each typed column message, paired with the DataFrame field it belongs in.  data_frame() routes by exact type, so
# a pre-built column of any supported kind lands in the right repeated field without the caller naming it.
_COLUMN_FIELD_BY_TYPE = {
    common_pb2.DoubleColumn: "doubleColumns",
    common_pb2.FloatColumn: "floatColumns",
    common_pb2.Int64Column: "int64Columns",
    common_pb2.Int32Column: "int32Columns",
    common_pb2.BoolColumn: "boolColumns",
    common_pb2.StringColumn: "stringColumns",
    common_pb2.EnumColumn: "enumColumns",
    common_pb2.ImageColumn: "imageColumns",
    common_pb2.StructColumn: "structColumns",
    common_pb2.DoubleArrayColumn: "doubleArrayColumns",
    common_pb2.FloatArrayColumn: "floatArrayColumns",
    common_pb2.Int32ArrayColumn: "int32ArrayColumns",
    common_pb2.Int64ArrayColumn: "int64ArrayColumns",
    common_pb2.BoolArrayColumn: "boolArrayColumns",
    common_pb2.DataColumn: "dataColumns",
    common_pb2.SerializedDataColumn: "serializedDataColumns",
}

# Columns whose per-sample count is len(values).  The array columns are excluded: their values are flat, so the
# sample count is len(values) / prod(dims).  Serialized columns carry no countable values at all.
_SCALAR_COLUMN_TYPES = (
    common_pb2.DoubleColumn,
    common_pb2.FloatColumn,
    common_pb2.Int64Column,
    common_pb2.Int32Column,
    common_pb2.BoolColumn,
    common_pb2.StringColumn,
    common_pb2.EnumColumn,
    common_pb2.StructColumn,
)

_ARRAY_COLUMN_TYPES = (
    common_pb2.DoubleArrayColumn,
    common_pb2.FloatArrayColumn,
    common_pb2.Int32ArrayColumn,
    common_pb2.Int64ArrayColumn,
    common_pb2.BoolArrayColumn,
)


# ----------------------------------------------------------------------
# time axis
# ----------------------------------------------------------------------


def sampling_clock(
    start_time: TimestampInput,
    period_nanos: int,
    count: int,
) -> common_pb2.DataTimestamps:
    """
    Builds a DataTimestamps with a SamplingClock time axis, the compact form for regularly-sampled data.

    When labeling existing archived data (sample status), the clock must match that data's clock exactly --
    matching is by exact timestamp at nanosecond precision, so an off-by-one-nanosecond period misses every sample
    after the first.  When describing derived values (calculations), the clock defines the axis, and count must
    equal the length of every column on it.

    Note the server rejects a calculations axis whose startTime is epoch zero, so build fixtures from a real time
    rather than datetime(1970, 1, 1).

    :param start_time: Time of the first sample (tz-aware datetime, epoch seconds, or common.Timestamp).
    :param period_nanos: Period between samples, in nanoseconds.  Must be > 0.
    :param count: Number of samples in the interval.  Must be >= 1.
    :return: A DataTimestamps carrying a SamplingClock.
    :raises ValueError: if period_nanos is not positive or count is less than 1.
    """
    if period_nanos <= 0:
        raise ValueError(f"sampling_clock() requires period_nanos > 0, got {period_nanos}")
    if count < 1:
        raise ValueError(f"sampling_clock() requires count >= 1, got {count}")

    timestamps = common_pb2.DataTimestamps()
    timestamps.samplingClock.startTime.CopyFrom(to_timestamp(start_time))
    timestamps.samplingClock.periodNanos = period_nanos
    timestamps.samplingClock.count = count
    return timestamps


def timestamp_list(values: Sequence[TimestampInput]) -> common_pb2.DataTimestamps:
    """
    Builds a DataTimestamps with an explicit TimestampList time axis, the form for irregularly-spaced samples --
    and, for sample status, for sparse labeling that names only the samples being labeled.

    Timestamps must be strictly increasing, which is validated here rather than deferred to the server.  When
    labeling existing data, supply timestamps taken from query results (or exact SamplingClock arithmetic); a
    recomputed or rounded timestamp will silently fail to match its sample.

    :param values: The timestamps of the samples (tz-aware datetimes, epoch seconds, or common.Timestamp objects).
    :return: A DataTimestamps carrying a TimestampList.
    :raises ValueError: if values is empty or the timestamps are not strictly increasing.
    """
    if not values:
        raise ValueError("timestamp_list() requires a non-empty values list")

    converted = [to_timestamp(value) for value in values]

    previous = converted[0]
    for index, current in enumerate(converted[1:], start=1):
        if (current.epochSeconds, current.nanoseconds) <= (previous.epochSeconds, previous.nanoseconds):
            raise ValueError(
                f"timestamp_list() requires strictly increasing timestamps; entry {index} "
                f"({current.epochSeconds}.{current.nanoseconds:09d}) does not follow entry {index - 1} "
                f"({previous.epochSeconds}.{previous.nanoseconds:09d})"
            )
        previous = current

    timestamps = common_pb2.DataTimestamps()
    timestamps.timestampList.timestamps.extend(converted)
    return timestamps


def timestamp_count(timestamps: common_pb2.DataTimestamps) -> int:
    """
    Returns the number of timestamps a DataTimestamps describes, for validating parallel-array lengths.

    An empty or malformed axis is rejected here rather than allowed to surface later as a confusing column-length
    mismatch.  The axis builders already make this unreachable -- sampling_clock() requires count >= 1 and a
    positive period, and timestamp_list() requires a non-empty list -- but a hand-built DataTimestamps can still
    carry a zero-count or zero-period SamplingClock, or an empty or out-of-order TimestampList, and all report
    their oneof arm as set.  Rejecting them keeps this in step with the read path -- expand_data_timestamps() for
    the clock rules, data_frame_from_pandas() for the ordering one; anything data_frame() accepts must be readable
    back.

    :param timestamps: The time axis to measure.
    :return: The number of timestamps on the axis.
    :raises ValueError: if neither axis form is set, if the axis describes no timestamps, if a SamplingClock's
        periodNanos is not positive, or if a TimestampList is not strictly increasing.
    """
    axis = timestamps.WhichOneof("value")
    if axis == "samplingClock":
        count = timestamps.samplingClock.count
        if count < 1:
            raise ValueError(f"DataTimestamps samplingClock requires count >= 1, got {count}")
        period = timestamps.samplingClock.periodNanos
        if period <= 0:
            # Checked here as well as in sampling_clock(), which a hand-built axis bypasses.  The read path
            # (expand_data_timestamps) rejects a non-positive period, so accepting one here would build a frame
            # the library cannot read back -- and every sample after the first would share the first's timestamp.
            raise ValueError(f"DataTimestamps samplingClock requires periodNanos > 0, got {period}")
        return count
    if axis == "timestampList":
        entries = timestamps.timestampList.timestamps
        count = len(entries)
        if count < 1:
            raise ValueError("DataTimestamps timestampList requires at least one timestamp, got an empty list")
        # Strict ordering, as timestamp_list() enforces -- a hand-built axis bypasses that builder entirely.
        # Duplicate or decreasing timestamps mean two samples claim one instant, which the identity model cannot
        # express, and data_frame_from_pandas() rejects the index they produce, so accepting them here would build
        # a frame the library cannot round-trip.
        previous = entries[0]
        for index, current in enumerate(entries[1:], start=1):
            if (current.epochSeconds, current.nanoseconds) <= (previous.epochSeconds, previous.nanoseconds):
                raise ValueError(
                    f"DataTimestamps timestampList requires strictly increasing timestamps; entry {index} "
                    f"({current.epochSeconds}.{current.nanoseconds:09d}) does not follow entry {index - 1} "
                    f"({previous.epochSeconds}.{previous.nanoseconds:09d})"
                )
            previous = current
        return count
    raise ValueError("DataTimestamps must specify either a samplingClock or a timestampList")


# ----------------------------------------------------------------------
# provenance
# ----------------------------------------------------------------------


def _time_range(bounds: tuple[TimestampInput, TimestampInput]) -> common_pb2.TimeRange:
    """
    Builds a common.TimeRange from a (begin, end) pair.

    :param bounds: The (begin, end) pair, each a tz-aware datetime, epoch seconds, or common.Timestamp.
    :return: The equivalent common.TimeRange.
    :raises ValueError: if begin is not strictly before end.
    """
    begin, end = (to_timestamp(bound) for bound in bounds)
    if (begin.epochSeconds, begin.nanoseconds) >= (end.epochSeconds, end.nanoseconds):
        raise ValueError(
            f"time_range requires begin strictly before end; got begin "
            f"{begin.epochSeconds}.{begin.nanoseconds:09d} and end {end.epochSeconds}.{end.nanoseconds:09d}"
        )
    time_range = common_pb2.TimeRange()
    time_range.beginTime.CopyFrom(begin)
    time_range.endTime.CopyFrom(end)
    return time_range


def pv_source(
    pv_name: str,
    time_range: tuple[TimestampInput, TimestampInput] | None = None,
) -> common_pb2.ColumnProvenance.ColumnSource:
    """
    Builds a ColumnSource naming an archived PV a calculated column was derived from.

    :param pv_name: Name of the source PV.
    :param time_range: Optional (begin, end) pair narrowing which part of the PV's history was used.
    :return: A ColumnProvenance.ColumnSource with its pvName origin set.
    :raises ValueError: if pv_name is empty, or if the time range's begin is not before its end.
    """
    if not pv_name:
        raise ValueError("pv_source() requires a non-empty pv_name")

    source = common_pb2.ColumnProvenance.ColumnSource()
    source.pvName = pv_name
    if time_range is not None:
        source.timeRange.CopyFrom(_time_range(time_range))
    return source


def calculations_source(
    calculations_id: str,
    frame_name: str,
    column_name: str,
    time_range: tuple[TimestampInput, TimestampInput] | None = None,
) -> common_pb2.ColumnProvenance.ColumnSource:
    """
    Builds a ColumnSource naming another calculations column this one was derived from.

    These are soft references: deleting the annotation that owns the referenced calculations leaves this link
    dangling, which readers are expected to tolerate rather than resolve.

    :param calculations_id: Id of the calculations object holding the source column.
    :param frame_name: Name of the frame within those calculations.
    :param column_name: Name of the column within that frame.
    :param time_range: Optional (begin, end) pair narrowing which part of the source column was used.
    :return: A ColumnProvenance.ColumnSource with its calculationsColumn origin set.
    :raises ValueError: if any of the three identifiers is empty, or if the time range is reversed.
    """
    if not calculations_id:
        raise ValueError("calculations_source() requires a non-empty calculations_id")
    if not frame_name:
        raise ValueError("calculations_source() requires a non-empty frame_name")
    if not column_name:
        raise ValueError("calculations_source() requires a non-empty column_name")

    source = common_pb2.ColumnProvenance.ColumnSource()
    source.calculationsColumn.calculationsId = calculations_id
    source.calculationsColumn.frameName = frame_name
    source.calculationsColumn.columnName = column_name
    if time_range is not None:
        source.timeRange.CopyFrom(_time_range(time_range))
    return source


def provenance(
    source: str | None = None,
    process: str | None = None,
    derived_from: list[common_pb2.ColumnProvenance.ColumnSource] | None = None,
) -> common_pb2.ColumnProvenance:
    """
    Builds a ColumnProvenance recording where a column's values came from and how they were produced.

    :param source: Free-text name of the producing system or person.
    :param process: Free-text description of the computation (e.g. "1 Hz RMS").
    :param derived_from: The inputs this column was computed from (see pv_source() / calculations_source()).
    :return: A common.ColumnProvenance.
    """
    result = common_pb2.ColumnProvenance()
    if source:
        result.source = source
    if process:
        result.process = process
    if derived_from:
        result.derivedFrom.extend(derived_from)
    return result


def column_metadata(
    tags: list[str] | None = None,
    attributes: dict[str, str] | None = None,
    provenance: common_pb2.ColumnProvenance | None = None,
) -> common_pb2.ColumnMetadata:
    """
    Builds a ColumnMetadata: per-column tags, attributes, and provenance.

    :param tags: Tags (keywords) describing the column.
    :param attributes: Map of key/value attributes describing the column.
    :param provenance: Where the column's values came from (see provenance()).
    :return: A common.ColumnMetadata.
    """
    metadata = common_pb2.ColumnMetadata()
    if provenance is not None:
        metadata.provenance.CopyFrom(provenance)
    if tags:
        metadata.tags[:] = tags
    if attributes:
        for name, value in attributes.items():
            attribute = metadata.attributes.add()
            attribute.name = name
            attribute.value = value
    return metadata


# ----------------------------------------------------------------------
# typed scalar columns
# ----------------------------------------------------------------------


def _build_scalar_column(
    column_class: Any,
    builder_name: str,
    name: str,
    values: list,
    metadata: common_pb2.ColumnMetadata | None,
) -> Any:
    """
    Builds one typed scalar column, applying the shape rules every column type shares.

    :param column_class: The typed column message class to construct.
    :param builder_name: The public builder's name, used in error messages.
    :param name: The column's name.
    :param values: The column's values, one per sample.
    :param metadata: Optional per-column metadata.
    :return: The constructed typed column message.
    :raises ValueError: if name is empty or values is empty.
    """
    if not name or not name.strip():
        raise ValueError(f"{builder_name}() requires a non-blank name, got {name!r}")
    if not values:
        raise ValueError(f"{builder_name}() requires a non-empty values list for column '{name}'")

    column = column_class()
    column.name = name
    column.values[:] = values
    if metadata is not None:
        column.metadata.CopyFrom(metadata)
    return column


def double_column(
    name: str, values: list[float], metadata: common_pb2.ColumnMetadata | None = None
) -> common_pb2.DoubleColumn:
    """
    Builds a DoubleColumn (float64) with one value per sample.

    :param name: The column's name, unique within its frame.
    :param values: One float per sample on the frame's time axis.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.DoubleColumn.
    :raises ValueError: if name or values is empty.
    """
    return _build_scalar_column(common_pb2.DoubleColumn, "double_column", name, values, metadata)


def float_column(
    name: str, values: list[float], metadata: common_pb2.ColumnMetadata | None = None
) -> common_pb2.FloatColumn:
    """
    Builds a FloatColumn (float32) with one value per sample.

    :param name: The column's name, unique within its frame.
    :param values: One float per sample on the frame's time axis.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.FloatColumn.
    :raises ValueError: if name or values is empty.
    """
    return _build_scalar_column(common_pb2.FloatColumn, "float_column", name, values, metadata)


def int64_column(
    name: str, values: list[int], metadata: common_pb2.ColumnMetadata | None = None
) -> common_pb2.Int64Column:
    """
    Builds an Int64Column with one value per sample.

    :param name: The column's name, unique within its frame.
    :param values: One int per sample on the frame's time axis.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.Int64Column.
    :raises ValueError: if name or values is empty.
    """
    return _build_scalar_column(common_pb2.Int64Column, "int64_column", name, values, metadata)


def int32_column(
    name: str, values: list[int], metadata: common_pb2.ColumnMetadata | None = None
) -> common_pb2.Int32Column:
    """
    Builds an Int32Column with one value per sample.

    :param name: The column's name, unique within its frame.
    :param values: One int per sample on the frame's time axis.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.Int32Column.
    :raises ValueError: if name or values is empty.
    """
    return _build_scalar_column(common_pb2.Int32Column, "int32_column", name, values, metadata)


def bool_column(
    name: str, values: list[bool], metadata: common_pb2.ColumnMetadata | None = None
) -> common_pb2.BoolColumn:
    """
    Builds a BoolColumn with one value per sample.

    :param name: The column's name, unique within its frame.
    :param values: One bool per sample on the frame's time axis.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.BoolColumn.
    :raises ValueError: if name or values is empty.
    """
    return _build_scalar_column(common_pb2.BoolColumn, "bool_column", name, values, metadata)


def string_column(
    name: str, values: list[str], metadata: common_pb2.ColumnMetadata | None = None
) -> common_pb2.StringColumn:
    """
    Builds a StringColumn with one value per sample.

    The server caps individual string values (256 characters at the time of writing); that limit is deployment
    policy and is deliberately not duplicated here.

    :param name: The column's name, unique within its frame.
    :param values: One string per sample on the frame's time axis.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.StringColumn.
    :raises ValueError: if name or values is empty.
    """
    return _build_scalar_column(common_pb2.StringColumn, "string_column", name, values, metadata)


def enum_column(
    name: str, values: list[int], enum_id: str, metadata: common_pb2.ColumnMetadata | None = None
) -> common_pb2.EnumColumn:
    """
    Builds an EnumColumn: integer codes plus the id of the enumeration that gives them meaning.

    :param name: The column's name, unique within its frame.
    :param values: One integer code per sample on the frame's time axis.
    :param enum_id: Id of the enumeration defining what the codes mean.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.EnumColumn.
    :raises ValueError: if name or enum_id is blank, or values is empty.
    """
    if not enum_id or not enum_id.strip():
        raise ValueError(f"enum_column() requires a non-blank enum_id, got {enum_id!r}")
    column = _build_scalar_column(common_pb2.EnumColumn, "enum_column", name, values, metadata)
    column.enumId = enum_id
    return column


def _is_numpy_bool(value: Any) -> bool:
    """
    True if a value is a NumPy boolean scalar.

    NumPy's boolean scalar is a subclass of neither bool nor numbers.Integral, so nothing else in data_column()'s
    type mapping catches it -- while np.float64 IS a subclass of float and np.int64 IS numbers.Integral.  Left
    unhandled it would be the one NumPy scalar the mapping rejected.

    Identified by its type's module and name rather than by importing NumPy, which keeps this module free of the
    optional [analysis] dependency.  Both spellings are matched: the type is named `bool_` under NumPy 1.x and
    `bool` under 2.x, and the module check is what keeps the latter from matching an unrelated class.

    :param value: The value to test.
    :return: True if the value is a numpy.bool_ / numpy.bool scalar.
    """
    value_type = type(value)
    return value_type.__module__ == "numpy" and value_type.__name__ in ("bool_", "bool")


def data_column(
    name: str, values: list[Any], metadata: common_pb2.ColumnMetadata | None = None
) -> common_pb2.DataColumn:
    """
    Builds a legacy DataColumn of per-sample DataValues -- the escape hatch for a column that needs gaps.

    The typed columns above are dense: every sample has a value, because the repeated field carries no notion of a
    missing entry.  A DataColumn's values are DataValue messages, whose oneof can be left unset, so a None entry
    here becomes a genuinely absent value.  That makes this the only way to express a gap on a shared time axis
    short of giving the sparse column its own frame.

    Values are mapped to DataValue arms by Python type: bool -> booleanValue (checked before int, since bool is a
    subclass of int), int -> longValue, float -> doubleValue, str -> stringValue, bytes -> byteArrayValue, and
    None -> an unset oneof.  Anything else raises: silently coercing an unexpected type would store something the
    caller did not mean.  Pass a pre-built DataValue to use an arm this mapping does not cover.

    The integer and float arms are matched by numbers.Integral / numbers.Real, and np.bool_ by name, so NumPy
    scalars work too.  np.int64 is not a subclass of int and np.bool_ is not a subclass of bool (while np.float64
    IS a subclass of float), so mapping by exact Python type would accept some NumPy scalars and reject others --
    an arbitrary distinction for a caller coming from pandas or NumPy.

    :param name: The column's name, unique within its frame.
    :param values: One value per sample, where None means "no value for this sample".
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.DataColumn.
    :raises ValueError: if name or values is empty, or if a value's type has no DataValue mapping.
    """
    if not name or not name.strip():
        raise ValueError(f"data_column() requires a non-blank name, got {name!r}")
    if not values:
        raise ValueError(f"data_column() requires a non-empty values list for column '{name}'")

    column = common_pb2.DataColumn()
    column.name = name
    for index, value in enumerate(values):
        data_value = column.dataValues.add()
        if value is None:
            continue
        if isinstance(value, common_pb2.DataValue):
            data_value.CopyFrom(value)
        elif isinstance(value, bool) or _is_numpy_bool(value):
            # Checked before the integer branch: bool is a subclass of int, so that branch would swallow it.
            data_value.booleanValue = bool(value)
        elif isinstance(value, Integral):
            # Integral rather than int, so a NumPy integer scalar maps like a Python one.
            data_value.longValue = int(value)
        elif isinstance(value, Real):
            # Real rather than float, for symmetry with the integer branch above.
            data_value.doubleValue = float(value)
        elif isinstance(value, str):
            data_value.stringValue = value
        elif isinstance(value, bytes):
            data_value.byteArrayValue = value
        else:
            raise ValueError(
                f"data_column() cannot map value at index {index} of column '{name}' "
                f"(type {type(value).__name__}) to a DataValue; pass a pre-built common.DataValue instead"
            )

    if metadata is not None:
        column.metadata.CopyFrom(metadata)
    return column


# ----------------------------------------------------------------------
# array, image, struct, and serialized columns
# ----------------------------------------------------------------------


def _require_non_blank(value: str, builder_name: str, what: str) -> None:
    """
    Raises unless a string argument has content; whitespace-only counts as blank, as it does for column names.

    :param value: The argument to check.
    :param builder_name: The public builder's name, used in the message.
    :param what: The argument's name, used in the message.
    :raises ValueError: if value is empty or whitespace-only.
    """
    if not value or not value.strip():
        raise ValueError(f"{builder_name}() requires a non-blank {what}, got {value!r}")


def _sample_shape_and_values(sample: Any) -> tuple[tuple[int, ...], list]:
    """
    Returns one array sample's shape and its values flattened in row-major (C) order.

    A sample is either anything with `.shape` and `.ravel()` -- a NumPy array, duck-typed so NumPy stays an
    optional dependency -- or a nested sequence, whose shape is inferred from the nesting and must be rectangular.
    Row-major is the order data_frame_conversions assumes when it slices a column back into samples.

    :param sample: One sample of an array column.
    :return: (shape, flat values); a scalar sample has the empty shape ().
    :raises ValueError: if a nested sequence is ragged.
    """
    if hasattr(sample, "shape") and hasattr(sample, "ravel"):
        # tolist() hands back Python scalars, which every repeated proto field accepts; NumPy scalars are not
        # uniformly accepted (np.bool_ in a bool field, for one).
        return tuple(int(extent) for extent in sample.shape), sample.ravel().tolist()
    if isinstance(sample, (str, bytes)) or not isinstance(sample, Sequence):
        return (), [sample]
    if len(sample) == 0:
        return (0,), []
    shapes_and_values = [_sample_shape_and_values(element) for element in sample]
    inner_shape = shapes_and_values[0][0]
    flat: list = []
    for position, (shape, values) in enumerate(shapes_and_values):
        if shape != inner_shape:
            raise ValueError(
                f"element {position} has shape {list(shape)} but element 0 has shape {list(inner_shape)}; "
                f"an array sample must be rectangular"
            )
        flat.extend(values)
    return (len(sample), *inner_shape), flat


def _build_array_column(
    column_class: Any,
    builder_name: str,
    name: str,
    samples: Sequence[Any],
    dims: Sequence[int] | None,
    metadata: common_pb2.ColumnMetadata | None,
) -> Any:
    """
    Builds one array column: every sample the same shape, stored flat as samples x prod(dims) values.

    :param column_class: The array column message class to construct.
    :param builder_name: The public builder's name, used in error messages.
    :param name: The column's name.
    :param samples: One array per sample (nested sequences or NumPy arrays).
    :param dims: Explicit per-sample dimensions, or None to use the samples' own shape.
    :param metadata: Optional per-column metadata.
    :return: The constructed array column message.
    :raises ValueError: if a shape rule is violated; the message names the column and, where there is one, the
        offending sample.
    """
    _require_non_blank(name, builder_name, "name")
    # len(), not truthiness: a NumPy array of samples has no single truth value.
    if len(samples) == 0:
        raise ValueError(f"{builder_name}() requires at least one sample for column '{name}'")

    flat: list = []
    first_shape: tuple[int, ...] | None = None
    for index, sample in enumerate(samples):
        try:
            shape, values = _sample_shape_and_values(sample)
        except ValueError as e:
            raise ValueError(f"{builder_name}() column '{name}' sample {index}: {e}") from e
        if first_shape is None:
            first_shape = shape
        elif shape != first_shape:
            raise ValueError(
                f"{builder_name}() column '{name}' sample {index} has shape {list(shape)} but sample 0 has shape "
                f"{list(first_shape)}; every sample in an array column must have the same shape"
            )
        flat.extend(values)
    assert first_shape is not None  # samples is non-empty

    if len(first_shape) == 0:
        raise ValueError(
            f"{builder_name}() column '{name}' has scalar samples; each sample must be an array (use the scalar "
            f"column builders for one value per sample)"
        )

    if dims is None:
        resolved = list(first_shape)
    else:
        resolved = [int(dim) for dim in dims]
        per_sample = 1
        for dim in resolved:
            per_sample *= dim
        sample_size = len(flat) // len(samples)
        # Explicit dims either restate the samples' own shape or give a flat sample its multidimensional shape.
        # Anything else would silently reinterpret the data's layout (a 2x3 sample read as 3x2).
        if not (list(first_shape) == resolved or (len(first_shape) == 1 and sample_size == per_sample)):
            raise ValueError(
                f"{builder_name}() column '{name}' samples have shape {list(first_shape)}, which dims {resolved} "
                f"does not describe; pass dims only to restate that shape or to shape flat samples of "
                f"{per_sample} values"
            )

    if not 1 <= len(resolved) <= _MAX_ARRAY_DIMS:
        raise ValueError(
            f"{builder_name}() column '{name}' has {len(resolved)} dimensions {resolved}; array columns support "
            f"1 to {_MAX_ARRAY_DIMS}"
        )
    if any(dim <= 0 for dim in resolved):
        raise ValueError(f"{builder_name}() column '{name}' has dimensions {resolved}; every dimension must be > 0")

    column = column_class()
    column.name = name
    column.dimensions.dims.extend(resolved)
    try:
        column.values.extend(flat)
    except (TypeError, ValueError) as e:
        # protobuf raises TypeError for a wrong type and ValueError for an out-of-range integer; either way the
        # message should name the column.
        raise ValueError(f"{builder_name}() column '{name}' has a value its type cannot hold: {e}") from e
    if metadata is not None:
        column.metadata.CopyFrom(metadata)
    return column


def double_array_column(
    name: str,
    samples: Sequence[Any],
    dims: Sequence[int] | None = None,
    metadata: common_pb2.ColumnMetadata | None = None,
) -> common_pb2.DoubleArrayColumn:
    """
    Builds a DoubleArrayColumn (float64): one fixed-shape array per sample, such as a waveform or a 2-D map.

    Each sample is a nested sequence (its shape inferred, and required to be rectangular) or a NumPy array; a 2-D
    NumPy array works as `samples` directly, one row per sample.  Every sample must have the same shape, with 1 to
    3 dimensions.  Values are stored flat in row-major order.  Pass `dims` when the samples are already flat but
    mean a multidimensional shape -- [64, 64] for 4096-value samples -- or to restate their shape; dims that would
    reinterpret a multidimensional sample's layout are rejected.

    :param name: The column's name, unique within its frame.
    :param samples: One array per sample on the frame's time axis.
    :param dims: Optional per-sample dimensions (see above).
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.DoubleArrayColumn.
    :raises ValueError: if name is blank, samples is empty, a sample is scalar or ragged, samples differ in shape,
        dims do not describe the samples, or there are not 1 to 3 dimensions each > 0.
    """
    return _build_array_column(common_pb2.DoubleArrayColumn, "double_array_column", name, samples, dims, metadata)


def float_array_column(
    name: str,
    samples: Sequence[Any],
    dims: Sequence[int] | None = None,
    metadata: common_pb2.ColumnMetadata | None = None,
) -> common_pb2.FloatArrayColumn:
    """
    Builds a FloatArrayColumn (float32).  Sample and dims rules are those of double_array_column().

    :param name: The column's name, unique within its frame.
    :param samples: One array per sample on the frame's time axis.
    :param dims: Optional per-sample dimensions.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.FloatArrayColumn.
    :raises ValueError: as for double_array_column().
    """
    return _build_array_column(common_pb2.FloatArrayColumn, "float_array_column", name, samples, dims, metadata)


def int32_array_column(
    name: str,
    samples: Sequence[Any],
    dims: Sequence[int] | None = None,
    metadata: common_pb2.ColumnMetadata | None = None,
) -> common_pb2.Int32ArrayColumn:
    """
    Builds an Int32ArrayColumn.  Sample and dims rules are those of double_array_column().

    :param name: The column's name, unique within its frame.
    :param samples: One array per sample on the frame's time axis.
    :param dims: Optional per-sample dimensions.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.Int32ArrayColumn.
    :raises ValueError: as for double_array_column(), or if a value is not an integer in range.
    """
    return _build_array_column(common_pb2.Int32ArrayColumn, "int32_array_column", name, samples, dims, metadata)


def int64_array_column(
    name: str,
    samples: Sequence[Any],
    dims: Sequence[int] | None = None,
    metadata: common_pb2.ColumnMetadata | None = None,
) -> common_pb2.Int64ArrayColumn:
    """
    Builds an Int64ArrayColumn.  Sample and dims rules are those of double_array_column().

    :param name: The column's name, unique within its frame.
    :param samples: One array per sample on the frame's time axis.
    :param dims: Optional per-sample dimensions.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.Int64ArrayColumn.
    :raises ValueError: as for double_array_column(), or if a value is not an integer in range.
    """
    return _build_array_column(common_pb2.Int64ArrayColumn, "int64_array_column", name, samples, dims, metadata)


def bool_array_column(
    name: str,
    samples: Sequence[Any],
    dims: Sequence[int] | None = None,
    metadata: common_pb2.ColumnMetadata | None = None,
) -> common_pb2.BoolArrayColumn:
    """
    Builds a BoolArrayColumn.  Sample and dims rules are those of double_array_column().

    :param name: The column's name, unique within its frame.
    :param samples: One array per sample on the frame's time axis.
    :param dims: Optional per-sample dimensions.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.BoolArrayColumn.
    :raises ValueError: as for double_array_column().
    """
    return _build_array_column(common_pb2.BoolArrayColumn, "bool_array_column", name, samples, dims, metadata)


def image_column(
    name: str,
    images: Sequence[bytes],
    width: int,
    height: int,
    channels: int,
    encoding: str,
    metadata: common_pb2.ColumnMetadata | None = None,
) -> common_pb2.ImageColumn:
    """
    Builds an ImageColumn: one encoded image per sample, all described by a single ImageDescriptor.

    The descriptor is per column, as the proto has it, so every image in the column shares its width, height,
    channel count, and encoding.  The encoding is a producer/consumer contract ("png", "raw-u16le", ...) that
    MLDP stores without interpreting.

    :param name: The column's name, unique within its frame.
    :param images: One encoded image per sample on the frame's time axis.
    :param width: Image width in pixels.  Must be > 0.
    :param height: Image height in pixels.  Must be > 0.
    :param channels: Channels per pixel, e.g. 1 (mono) or 3 (RGB).  Must be > 0.
    :param encoding: How each image's bytes are encoded.  Must be non-blank.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.ImageColumn.
    :raises ValueError: if name or encoding is blank, images is empty, or a descriptor dimension is not positive.
    """
    _require_non_blank(name, "image_column", "name")
    if len(images) == 0:
        raise ValueError(f"image_column() requires at least one image for column '{name}'")
    for label, value in (("width", width), ("height", height), ("channels", channels)):
        if value <= 0:
            raise ValueError(f"image_column() requires {label} > 0 for column '{name}', got {value}")
    _require_non_blank(encoding, "image_column", "encoding")

    column = common_pb2.ImageColumn()
    column.name = name
    column.imageDescriptor.width = width
    column.imageDescriptor.height = height
    column.imageDescriptor.channels = channels
    column.imageDescriptor.encoding = encoding
    column.images.extend(images)
    if metadata is not None:
        column.metadata.CopyFrom(metadata)
    return column


def struct_column(
    name: str,
    values: Sequence[bytes],
    schema_id: str,
    metadata: common_pb2.ColumnMetadata | None = None,
) -> common_pb2.StructColumn:
    """
    Builds a StructColumn: one serialized structure per sample, all following one schema.

    :param name: The column's name, unique within its frame.
    :param values: One serialized structure per sample on the frame's time axis.
    :param schema_id: Id of the schema the structures follow, e.g. "beam_position:v3".  Must be non-blank.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.StructColumn.
    :raises ValueError: if name or schema_id is blank, or values is empty.
    """
    _require_non_blank(schema_id, "struct_column", "schema_id")
    column = _build_scalar_column(common_pb2.StructColumn, "struct_column", name, list(values), metadata)
    column.schemaId = schema_id
    return column


def serialized_column(
    name: str,
    payload: bytes,
    encoding: str,
    metadata: common_pb2.ColumnMetadata | None = None,
) -> common_pb2.SerializedDataColumn:
    """
    Builds a SerializedDataColumn: a whole column's values as one opaque byte string.

    The payload is opaque to MLDP and to this library, so it carries no sample count and is not checked against
    the time axis -- the server checks only its name and encoding.  For the same reason split_data_frame() cannot
    divide a frame containing one.

    :param name: The column's name, unique within its frame.
    :param payload: The encoded column.
    :param encoding: How the payload is encoded, e.g. "proto:Image".  Must be non-blank.
    :param metadata: Optional per-column tags, attributes, and provenance (see column_metadata()).
    :return: A common.SerializedDataColumn.
    :raises ValueError: if name or encoding is blank.
    """
    _require_non_blank(name, "serialized_column", "name")
    _require_non_blank(encoding, "serialized_column", "encoding")
    column = common_pb2.SerializedDataColumn()
    column.name = name
    column.encoding = encoding
    column.payload = payload
    if metadata is not None:
        column.metadata.CopyFrom(metadata)
    return column


# ----------------------------------------------------------------------
# frame assembly
# ----------------------------------------------------------------------


def _array_sample_size(column: Any) -> int | None:
    """
    Returns an array column's per-sample size, prod(dims), or None when it cannot be derived.

    Absent dims are underivable rather than a product of 1 -- an empty product would silently treat every element
    as its own sample.  A zero or negative dim is equally unusable, so both report as None for the caller to
    reject with a message naming the column.

    :param column: An array column (DoubleArrayColumn, Int32ArrayColumn, ...).
    :return: The number of values each sample occupies, or None if dims are missing or non-positive.
    """
    dims = list(column.dimensions.dims)
    if not dims:
        return None
    product = 1
    for dim in dims:
        product *= dim
    if product <= 0:
        return None
    return product


def _column_sample_count(column: Any) -> int | None:
    """
    Returns the number of samples a column carries, or None when the column has no countable per-sample values.

    For an array column the count is len(values) / prod(dims); a value count that is not a whole multiple of
    prod(dims) has no sample count at all and reports as None, so _check_column() rejects it rather than letting
    floor division round it into a passing count.  The read path (data_frame_conversions._reshape_array_values)
    applies the same rule, and a frame this accepted but that could not be read back would be the worst outcome.

    An ImageColumn counts its `images`, and a DataColumn its `dataValues`; only the scalar and array columns keep
    their per-sample entries in `values`.

    :param column: A typed column, a legacy DataColumn, an ImageColumn, or a SerializedDataColumn.
    :return: The sample count, or None for a SerializedDataColumn (whose payload is opaque) and for an array
        column whose dims are missing, non-positive, or do not evenly divide its values.
    """
    if isinstance(column, common_pb2.SerializedDataColumn):
        return None
    if isinstance(column, common_pb2.DataColumn):
        return len(column.dataValues)
    if isinstance(column, common_pb2.ImageColumn):
        # ImageColumn holds one encoded payload per sample in `images`; it has no `values` field at all, so the
        # generic path below would raise AttributeError on a column this function claims to support.
        return len(column.images)
    if isinstance(column, _ARRAY_COLUMN_TYPES):
        product = _array_sample_size(column)
        if product is None:
            return None
        if len(column.values) % product != 0:
            return None
        return len(column.values) // product
    return len(column.values)


def _check_column(column: Any, index: int, expected_count: int, seen_names: set[str]) -> None:
    """
    Applies the server's per-column shape rules to one column, with a message naming the column.

    :param column: The column to check.
    :param index: The column's position in the caller's list, for messages about an unnamed column.
    :param expected_count: The frame's sample count, which every counted column must match.
    :param seen_names: Names already used in this frame; mutated to record this column's name.
    :raises ValueError: if the column is of an unsupported type, unnamed, empty, duplicate-named, count-mismatched,
        or missing a structural field its kind requires (enumId, 1-3 array dims, an image descriptor, schemaId,
        or a serialized column's encoding).
    """
    if type(column) not in _COLUMN_FIELD_BY_TYPE:
        raise ValueError(
            f"data_frame() received an unsupported column type at index {index}: {type(column).__name__}. "
            f"Use one of the column builders, or pass a pre-built typed column message."
        )

    name = column.name
    if not name or not name.strip():
        # Blank, not merely empty: the rule is a non-blank name, and a whitespace-only one is not a name.
        raise ValueError(
            f"data_frame() requires a non-blank name for every column; column at index {index} has {name!r}"
        )
    if name in seen_names:
        raise ValueError(
            f"data_frame() requires unique column names within a frame; '{name}' appears more than once "
            f"(names must be unique across ALL column types, not just within one type)"
        )
    seen_names.add(name)

    # Each kind's structural fields.  The builders enforce these too, but a hand-built column bypasses every
    # builder, and the server rejects all of them -- so this is the check that holds for any column (#17, T7).
    if isinstance(column, common_pb2.SerializedDataColumn):
        # Serialized payloads are opaque; the server checks only the name and the encoding, never a count.
        if not column.encoding.strip():
            raise ValueError(f"data_frame() serialized column '{name}' requires a non-blank encoding")
        return
    if isinstance(column, common_pb2.EnumColumn) and not column.enumId.strip():
        raise ValueError(f"data_frame() enum column '{name}' requires a non-blank enumId")
    if isinstance(column, common_pb2.StructColumn) and not column.schemaId.strip():
        raise ValueError(f"data_frame() struct column '{name}' requires a non-blank schemaId")
    if isinstance(column, common_pb2.ImageColumn):
        if not column.HasField("imageDescriptor"):
            raise ValueError(f"data_frame() image column '{name}' requires an imageDescriptor")
        descriptor = column.imageDescriptor
        for label, value in (
            ("width", descriptor.width),
            ("height", descriptor.height),
            ("channels", descriptor.channels),
        ):
            if value <= 0:
                raise ValueError(f"data_frame() image column '{name}' requires imageDescriptor.{label} > 0")
        if not descriptor.encoding.strip():
            raise ValueError(f"data_frame() image column '{name}' requires a non-blank imageDescriptor.encoding")

    if isinstance(column, _ARRAY_COLUMN_TYPES):
        dims = list(column.dimensions.dims)
        if len(dims) > _MAX_ARRAY_DIMS:
            raise ValueError(
                f"data_frame() array column '{name}' has {len(dims)} dimensions {dims}; array columns support "
                f"1 to {_MAX_ARRAY_DIMS}"
            )
        product = _array_sample_size(column)
        if product is None:
            raise ValueError(
                f"data_frame() cannot determine the sample count of array column '{name}': its dimensions are "
                f"missing or zero.  Set ArrayDimensions.dims so that values is samples x prod(dims)."
            )
        if len(column.values) % product != 0:
            raise ValueError(
                f"data_frame() array column '{name}' has {len(column.values)} values, which is not a whole "
                f"multiple of its per-sample size {product} (from dims {list(column.dimensions.dims)}); "
                f"values must be samples x prod(dims)"
            )

    count = _column_sample_count(column)
    if count == 0:
        raise ValueError(f"data_frame() requires a non-empty values list for column '{name}'")
    if count != expected_count:
        raise ValueError(
            f"data_frame() column '{name}' has {count} values but the time axis has {expected_count} timestamps; "
            f"every column must carry exactly one value per sample"
        )


def _check_columns(columns: Sequence[Any], expected_count: int) -> None:
    """
    Applies _check_column() to every column of one frame, sharing a single set of names across them all.

    :param columns: The frame's columns, in any mix of supported types.
    :param expected_count: The frame's sample count.
    :raises ValueError: as for _check_column(), or if there are no columns.
    """
    if not columns:
        raise ValueError("data_frame() requires at least one column")
    seen_names: set[str] = set()
    for index, column in enumerate(columns):
        _check_column(column, index, expected_count, seen_names)


def _frame_columns(frame: common_pb2.DataFrame) -> list[Any]:
    """
    Returns every column of a frame, across all of its repeated fields, serialized columns included.

    :param frame: The frame to walk.
    :return: The frame's column messages, grouped by type in _COLUMN_FIELD_BY_TYPE order.
    """
    return [column for field in _COLUMN_FIELD_BY_TYPE.values() for column in getattr(frame, field)]


def validate_data_frame(frame: common_pb2.DataFrame) -> None:
    """
    Applies data_frame()'s checks to an already-assembled frame, however it was made.

    data_frame() validates as it assembles, but a frame can also come from data_frame_from_pandas(),
    split_data_frame(), or a hand-built message, and ingestion re-validates whatever it is given here so every
    path fails with the same client-side messages.  A column's "index" in an error counts across the frame's
    repeated fields in type order, since an assembled frame no longer has the caller's original ordering.

    :param frame: The frame to check.
    :raises ValueError: if the axis is empty or malformed, if the frame has no columns, or if any column violates a
        shape rule (see data_frame()).
    """
    expected_count = timestamp_count(frame.dataTimestamps)
    _check_columns(_frame_columns(frame), expected_count)


def data_frame(
    data_timestamps: common_pb2.DataTimestamps,
    columns: list[Any],
) -> common_pb2.DataFrame:
    """
    Assembles a common.DataFrame from a time axis and a list of columns, routing each column into the repeated
    field for its type.

    Accepts the columns this module builds and pre-built column messages alike.

    Validates the server's shape rules client-side, so a mistake names the offending column instead of bouncing the
    whole save: at least one column, a non-blank name and non-empty values for each, a value count matching the
    axis (for arrays, values / prod(dims)), column names unique across ALL types within the frame, and each kind's
    structural fields -- an enum's enumId, an array's 1 to 3 dims, an image's descriptor (positive width, height,
    and channels, and an encoding), a struct's schemaId, and a serialized column's encoding.  The server's size
    caps are not duplicated -- see the module docstring.  validate_data_frame() applies the same checks to a frame
    built any other way.

    :param data_timestamps: The frame's time axis (see sampling_clock() / timestamp_list()).
    :param columns: The frame's columns, in any mix of supported types.
    :return: A common.DataFrame.
    :raises ValueError: if the axis is empty or malformed, if columns is empty, or if any column violates a shape
        rule.
    """
    if not columns:
        raise ValueError("data_frame() requires at least one column")

    # Checked here, against the caller's list, rather than by validate_data_frame() after assembly: an unsupported
    # type cannot be routed at all, and the caller's own index is the useful one in a message.
    _check_columns(columns, timestamp_count(data_timestamps))

    frame = common_pb2.DataFrame()
    frame.dataTimestamps.CopyFrom(data_timestamps)
    for column in columns:
        getattr(frame, _COLUMN_FIELD_BY_TYPE[type(column)]).append(column)
    return frame


# ----------------------------------------------------------------------
# chunking
# ----------------------------------------------------------------------


def _varint_size(value: int) -> int:
    """
    Returns the number of bytes protobuf's varint encoding uses for a non-negative integer.

    :param value: The integer to encode.
    :return: Its encoded length, 1 to 10 bytes.
    """
    return max(1, (value.bit_length() + 6) // 7)


# The serialized size of an IngestDataRequest's two id fields at their budgeted worst case: MAX_BUDGETED_ID_CHARS
# characters that each take the maximum 4 UTF-8 bytes.  Measured from the message rather than hard-coded, so it
# stays right if the field numbers or wire format ever change.
_WORST_CASE_ID = "\U0010ffff" * MAX_BUDGETED_ID_CHARS
_BUDGETED_ID_FIELDS_BYTES = ingestion_pb2.IngestDataRequest(
    providerId=_WORST_CASE_ID, clientRequestId=_WORST_CASE_ID
).ByteSize()

# The ingestionDataFrame field's tag.  Field 3, length-delimited, encodes in one byte.
_FRAME_FIELD_TAG_BYTES = 1


def _budgeted_request_bytes(frame_bytes: int) -> int:
    """
    Returns the size of an IngestDataRequest carrying a frame of the given size and two worst-case ids.

    This is what split_data_frame() compares against max_bytes: the whole message the server's inbound limit
    applies to, not the frame alone.  The envelope is small but not fixed -- the ids are the caller's strings, and
    the frame's length prefix grows with the frame -- so it is computed rather than guessed.

    :param frame_bytes: The frame's serialized size (DataFrame.ByteSize()).
    :return: The request's serialized size, in bytes.
    """
    return _BUDGETED_ID_FIELDS_BYTES + _FRAME_FIELD_TAG_BYTES + _varint_size(frame_bytes) + frame_bytes


def _sample_field(column: Any) -> str:
    """
    Returns the name of the repeated field holding a column's per-sample values.

    :param column: Any countable column (not a SerializedDataColumn).
    :return: "dataValues" for a DataColumn, "images" for an ImageColumn, otherwise "values".
    """
    if isinstance(column, common_pb2.DataColumn):
        return "dataValues"
    if isinstance(column, common_pb2.ImageColumn):
        return "images"
    return "values"


def _fill_column_slice(target: Any, column: Any, start: int, end: int) -> None:
    """
    Fills an empty column with rows [start, end) of another column of the same type.

    Every field other than the per-sample values -- name, metadata, enumId, schemaId, dimensions, image
    descriptor -- is copied whole, generically, so a field added to a column message later is carried into every
    chunk without a change here.  Array values are sliced in prod(dims)-sized blocks, one block per row.

    :param target: A new, empty column message of the same type as column.
    :param column: The column to slice.
    :param start: First row, inclusive.
    :param end: Last row, exclusive.
    """
    sample_field = _sample_field(column)
    for field, value in column.ListFields():
        if field.name == sample_field:
            continue
        if field.message_type is not None and not field.is_repeated:
            getattr(target, field.name).CopyFrom(value)
        elif field.is_repeated:
            getattr(target, field.name).extend(value)
        else:
            setattr(target, field.name, value)

    block = 1
    if isinstance(column, _ARRAY_COLUMN_TYPES):
        # Validated before chunking starts, so the dims are present and positive.
        block = _array_sample_size(column) or 1
    getattr(target, sample_field).extend(getattr(column, sample_field)[start * block : end * block])


def _slice_frame(frame: common_pb2.DataFrame, start: int, end: int) -> common_pb2.DataFrame:
    """
    Returns rows [start, end) of a frame as a new frame carrying every column.

    A SamplingClock chunk keeps the period and gets start time startTime + start * periodNanos, computed in integer
    nanoseconds: present-day epoch nanoseconds need about 61 bits, and a float would move the instant.  A
    TimestampList is sliced.

    :param frame: A validated frame with no serialized columns.
    :param start: First row, inclusive.
    :param end: Last row, exclusive.
    :return: The chunk.
    """
    chunk = common_pb2.DataFrame()
    axis = frame.dataTimestamps
    if axis.WhichOneof("value") == "samplingClock":
        clock = chunk.dataTimestamps.samplingClock
        first_nanos = to_epoch_nanos(axis.samplingClock.startTime) + start * axis.samplingClock.periodNanos
        clock.startTime.CopyFrom(from_epoch_nanos(first_nanos))
        clock.periodNanos = axis.samplingClock.periodNanos
        clock.count = end - start
    else:
        chunk.dataTimestamps.timestampList.timestamps.extend(axis.timestampList.timestamps[start:end])

    for field in _COLUMN_FIELD_BY_TYPE.values():
        for column in getattr(frame, field):
            _fill_column_slice(getattr(chunk, field).add(), column, start, end)
    return chunk


def split_data_frame(
    frame: common_pb2.DataFrame,
    *,
    max_rows: int | None = None,
    max_bytes: int | None = None,
    max_span_nanos: int | None = None,
) -> Iterator[common_pb2.DataFrame]:
    """
    Splits a frame along its time axis into chunks that each fit the given limits, lazily.

    A large frame hits the server's limits at once: its inbound message cap (about 4 MB by default -- roughly 500k
    doubles) and its bucket span cap (1 day by default).  Over the message cap the call fails with a gRPC status,
    and on a stream it kills every request after it.  Each chunk carries every column, with its metadata, over a
    contiguous run of rows; concatenating the chunks' rows reproduces the frame.

    - max_rows caps the rows per chunk.
    - max_bytes caps the size of the whole IngestDataRequest a chunk will travel in, not just the frame, with room
      reserved for a providerId and clientRequestId of up to MAX_BUDGETED_ID_CHARS characters each.  So a chunk
      within the budget fits the message; pass SERVER_DEFAULT_MAX_MESSAGE_BYTES to target a default server.
      Chunks are sized from the rows already measured, so they fit but are not guaranteed to be the largest that
      would.
    - max_span_nanos caps the time from a chunk's first timestamp to its last, as the server measures the bucket
      span: a chunk may span exactly the limit.  Pass 86_400 * NANOS_PER_SECOND to target a default server.

    The server's limits are deployment settings, so none is assumed: at least one must be given.

    The frame is validated (see validate_data_frame()) before the first chunk is produced, and the call raises
    immediately rather than on first iteration.  Chunks are built as they are consumed, so feeding them to a
    streaming ingest never holds every chunk in memory at once.

    :param frame: The frame to split.
    :param max_rows: Maximum rows per chunk.  Must be > 0.
    :param max_bytes: Maximum size, in bytes, of the IngestDataRequest carrying each chunk.  Must be > 0.
    :param max_span_nanos: Maximum nanoseconds from a chunk's first timestamp to its last.  Must be >= 0.
    :return: An iterator over the chunks, in time order.
    :raises ValueError: if no limit is given or a limit is out of range, if the frame is invalid, if it contains a
        SerializedDataColumn (whose opaque payload cannot be divided), or -- during iteration -- if a single row
        exceeds max_bytes on its own.
    """
    if max_rows is None and max_bytes is None and max_span_nanos is None:
        raise ValueError("split_data_frame() requires at least one of max_rows, max_bytes, or max_span_nanos")
    if max_rows is not None and max_rows <= 0:
        raise ValueError(f"split_data_frame() requires max_rows > 0, got {max_rows}")
    if max_bytes is not None and max_bytes <= 0:
        raise ValueError(f"split_data_frame() requires max_bytes > 0, got {max_bytes}")
    if max_span_nanos is not None and max_span_nanos < 0:
        raise ValueError(f"split_data_frame() requires max_span_nanos >= 0, got {max_span_nanos}")

    validate_data_frame(frame)
    if len(frame.serializedDataColumns) > 0:
        names = [column.name for column in frame.serializedDataColumns]
        raise ValueError(
            f"split_data_frame() cannot split a frame with serialized columns {names}: a serialized payload is "
            f"opaque and cannot be divided by row"
        )

    return _iter_chunks(frame, max_rows, max_bytes, max_span_nanos)


def _iter_chunks(
    frame: common_pb2.DataFrame,
    max_rows: int | None,
    max_bytes: int | None,
    max_span_nanos: int | None,
) -> Iterator[common_pb2.DataFrame]:
    """
    The generator behind split_data_frame(), which validates its arguments eagerly and then returns this.

    :param frame: A validated frame with no serialized columns.
    :param max_rows: Maximum rows per chunk, or None.
    :param max_bytes: Maximum request bytes per chunk, or None.
    :param max_span_nanos: Maximum first-to-last nanoseconds per chunk, or None.
    :return: An iterator over the chunks.
    :raises ValueError: if a single row exceeds max_bytes.
    """
    row_count = timestamp_count(frame.dataTimestamps)
    axis = frame.dataTimestamps
    clock_period = axis.samplingClock.periodNanos if axis.WhichOneof("value") == "samplingClock" else None
    list_nanos = (
        None if clock_period is not None else [to_epoch_nanos(entry) for entry in axis.timestampList.timestamps]
    )

    # Bytes per row, first estimated from the whole frame and then from each chunk as it is measured, so the next
    # chunk's first guess tracks the data as it changes.
    row_bytes_estimate = max(1, frame.ByteSize() // row_count) if max_bytes is not None else 1

    start = 0
    while start < row_count:
        end = row_count
        if max_rows is not None:
            end = min(end, start + max_rows)
        if max_span_nanos is not None:
            if clock_period is not None:
                end = min(end, start + max_span_nanos // clock_period + 1)
            else:
                assert list_nanos is not None
                limit = list_nanos[start] + max_span_nanos
                span_end = start + 1
                while span_end < end and list_nanos[span_end] <= limit:
                    span_end += 1
                end = span_end

        if max_bytes is None:
            yield _slice_frame(frame, start, end)
            start = end
            continue

        end = min(end, start + max(1, max_bytes // row_bytes_estimate))
        while True:
            chunk = _slice_frame(frame, start, end)
            size = _budgeted_request_bytes(chunk.ByteSize())
            if size <= max_bytes:
                break
            if end - start == 1:
                raise ValueError(
                    f"split_data_frame() row {start} alone makes a {size}-byte request, over max_bytes {max_bytes} "
                    f"(which includes room for {MAX_BUDGETED_ID_CHARS}-character ids); no split can fit it"
                )
            # Shrink in proportion to the overshoot; the loop repeats if the rows are uneven.
            end = min(end - 1, start + max(1, (end - start) * max_bytes // size))
        row_bytes_estimate = max(1, size // (end - start))
        yield chunk
        start = end
