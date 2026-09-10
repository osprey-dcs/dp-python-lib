"""
Builders for common.DataFrame -- the shared time-series payload shape (issue #6, Phase 2).

A DataFrame is one time axis plus a set of columns sampled on it.  It is the payload of an annotation's
Calculations, and it is also ingestion's `ingestionDataFrame`: the same message, so this module is the substrate
issue #17 extends rather than a parallel one to fork.

Design decisions (see plan/tickets/6/plan.md, D6):
  - The time axis builders sampling_clock() / timestamp_list() live here now that a second caller exists; they are
    re-exported from sample_status_client so existing imports keep working.
  - The axis carries its own sample count and every column is validated against it.  That restates a number the
    caller already has (sampling_clock(t0, period, count=len(values))), which is deliberate: deriving the count
    from the columns would fork the axis API by caller, since sample_status_client genuinely knows its count
    independently of any column.  SampleStatusFrame set the same precedent -- explicit axis, per-column validation.
  - Validation mirrors the server's SHAPE rules only (non-blank names, non-empty values, count match, column-name
    uniqueness across types), so an error names the offending frame or column instead of bouncing the whole batch.
    The server's resource caps -- 256-char strings, 10M-element arrays, 50 MB images, 1 MB structs -- stay
    server-side: they are deployment policy, and duplicating numbers that can change is how clients drift.
  - Array, image, struct, and serialized column builders are deliberately absent.  Their ergonomics (dims, image
    descriptors, schema ids) are #17's to design once with ingestion data in hand.  Meanwhile they are reachable by
    building the proto directly and passing it to data_frame(), which accepts pre-built column messages alongside
    the ones its builders return.
"""

from numbers import Integral, Real
from typing import Any

from dp_python_lib.client.time_conversions import TimestampInput, to_timestamp
from dp_python_lib.grpc import common_pb2

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


def timestamp_list(values: list[TimestampInput]) -> common_pb2.DataTimestamps:
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
    :raises ValueError: if name, values, or enum_id is empty.
    """
    if not enum_id:
        raise ValueError("enum_column() requires a non-empty enum_id")
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
    :raises ValueError: if the column is of an unsupported type, unnamed, empty, duplicate-named, or
        count-mismatched.
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

    if isinstance(column, common_pb2.SerializedDataColumn):
        # Serialized payloads are opaque; the server checks their names only.
        return

    if isinstance(column, _ARRAY_COLUMN_TYPES):
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


def data_frame(
    data_timestamps: common_pb2.DataTimestamps,
    columns: list[Any],
) -> common_pb2.DataFrame:
    """
    Assembles a common.DataFrame from a time axis and a list of columns, routing each column into the repeated
    field for its type.

    Accepts the columns this module builds and pre-built column messages alike, so the kinds without builders yet
    (arrays, images, structs, serialized payloads -- issue #17) can be passed straight through.

    Validates the server's shape rules client-side, so a mistake names the offending column instead of bouncing the
    whole save: at least one column, a non-blank name and non-empty values for each, a value count matching the
    axis (for arrays, values / prod(dims)), and column names unique across ALL types within the frame.  The
    server's size caps are not duplicated -- see the module docstring.

    :param data_timestamps: The frame's time axis (see sampling_clock() / timestamp_list()).
    :param columns: The frame's columns, in any mix of supported types.
    :return: A common.DataFrame.
    :raises ValueError: if the axis is empty or malformed, if columns is empty, or if any column violates a shape
        rule.
    """
    if not columns:
        raise ValueError("data_frame() requires at least one column")

    expected_count = timestamp_count(data_timestamps)

    seen_names: set[str] = set()
    for index, column in enumerate(columns):
        _check_column(column, index, expected_count, seen_names)

    frame = common_pb2.DataFrame()
    frame.dataTimestamps.CopyFrom(data_timestamps)
    for column in columns:
        getattr(frame, _COLUMN_FIELD_BY_TYPE[type(column)]).append(column)
    return frame
