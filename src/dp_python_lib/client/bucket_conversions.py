"""
Pythonic conversions for bucket query results (issue #16; plan/tickets/16/plan.md).

A bucket query (QueryClient.query_buckets() and friends) returns the archive's stored units whole: each DataBucket
holds ONE PV's column, in its stored typed form, over that bucket's own time axis.  This module reads them.

The pure-Python half has no third-party dependencies.  It views a bucket as a one-column common.DataFrame, so every
data_frame_conversions reader applies unchanged, and trims a bucket to [begin, end) exactly at the proto level.
pandas is imported lazily inside the entry points that need it ([analysis] extra), so importing this module never
requires the extra.

Design decisions (see the plan's D4-D8):
  - Buckets come back UNTRIMMED: the server selects every bucket overlapping [begin, end) and returns it whole.
    Trimming is opt-in (trim_bucket(), time_range=), exact, and half-open.  It cannot remove samples in a gap
    between configuration intervals; use querySamples() for that.
  - Assembly groups by pvName and orders each PV's buckets by first timestamp itself, rather than relying on the
    server's (undocumented) arrival order.  Overlapping or duplicate timestamps across buckets are kept.
  - Read-side checks only.  A bucket is server data, and ingestion allows a non-decreasing TimestampList with
    repeated timestamps, which this library's write path (validate_data_frame(), timestamp_count()) would refuse.
    Nothing here calls those.
  - A SerializedDataColumn bucket is passed through (bucket_column(), bucket_to_data_frame()) but refused wherever
    its values would have to be interpreted, since its payload is opaque and the encoding contract is the caller's.
"""

from bisect import bisect_left
from collections.abc import Iterable
from typing import Any

from dp_python_lib.client import data_frame_conversions as dfc

# _slice_frame() is private to data_frame.py, which is the shared, kind-neutral home of the DataFrame builders rather
# than a feature module.  Reusing it keeps one implementation of the integer-nanosecond SamplingClock shift.
from dp_python_lib.client.data_frame import _COLUMN_FIELD_BY_TYPE, _slice_frame
from dp_python_lib.client.sample_status_conversions import expand_data_timestamps
from dp_python_lib.client.time_conversions import TimestampInput, to_epoch_nanos, to_timestamp
from dp_python_lib.grpc import common_pb2


def _require_pandas():
    """Imports and returns pandas, or raises an actionable error if the optional [analysis] extra is missing."""
    try:
        import pandas
    except ImportError as e:
        raise ImportError(
            "pandas is required for bucket DataFrame conversions.  Install the optional analysis extra: "
            'pip install "dp-python-lib[analysis]"'
        ) from e
    return pandas


# ----------------------------------------------------------------------
# one bucket
# ----------------------------------------------------------------------


def bucket_column(bucket: common_pb2.DataBucket) -> Any:
    """
    Returns a bucket's column message, whichever kind it is -- a SerializedDataColumn included.

    This is the way to reach a serialized bucket's payload and encoding, which the value readers refuse.

    :param bucket: The DataBucket.
    :return: The column message set in bucket.dataValues (DoubleColumn, ImageColumn, SerializedDataColumn, ...).
    :raises ValueError: if the bucket carries no column.
    """
    arm = bucket.dataValues.WhichOneof("values") if bucket.HasField("dataValues") else None
    if arm is None:
        raise ValueError(f"DataBucket for PV '{bucket.pvName}' carries no column (dataValues is unset)")
    return getattr(bucket.dataValues, arm)


def _require_axis(bucket: common_pb2.DataBucket) -> None:
    """Rejects a bucket whose time axis is unset, naming its PV."""
    if bucket.dataTimestamps.WhichOneof("value") is None:
        raise ValueError(f"DataBucket for PV '{bucket.pvName}' has no dataTimestamps; every bucket carries a time axis")


def bucket_to_data_frame(bucket: common_pb2.DataBucket) -> common_pb2.DataFrame:
    """
    Views a bucket as a one-column common.DataFrame over the bucket's own time axis.

    Every data_frame_conversions reader then applies unchanged: data_frame_columns(), data_frame_to_pandas(), and
    the companion accessors for array dims, image descriptors, and struct schema ids.  A serialized column lands in
    serializedDataColumns, where those readers skip it by design.

    The frame is not validated with validate_data_frame(): that is the write path's check, and it rejects the
    repeated timestamps a stored bucket may legitimately carry.

    :param bucket: The DataBucket.
    :return: A DataFrame carrying the bucket's axis and its single column.
    :raises ValueError: if the bucket has no time axis or no column.
    """
    _require_axis(bucket)
    column = bucket_column(bucket)

    frame = common_pb2.DataFrame()
    frame.dataTimestamps.CopyFrom(bucket.dataTimestamps)
    getattr(frame, _COLUMN_FIELD_BY_TYPE[type(column)]).add().CopyFrom(column)
    return frame


def bucket_timestamps(bucket: common_pb2.DataBucket) -> list[int]:
    """
    Expands a bucket's time axis into one integer epoch-nanosecond value per sample.

    A SamplingClock is expanded in integer nanoseconds, so the result is exact at present-day epochs.

    :param bucket: The DataBucket.
    :return: Epoch nanoseconds for each sample, in axis order.
    :raises ValueError: if the axis is unset, empty, or malformed.
    """
    _require_axis(bucket)
    epoch_nanos = expand_data_timestamps(bucket.dataTimestamps)
    if not epoch_nanos:
        raise ValueError(f"DataBucket for PV '{bucket.pvName}' has a time axis describing no timestamps")
    return epoch_nanos


def _refuse_serialized(bucket: common_pb2.DataBucket, column: Any, what: str) -> None:
    """Raises if column is a SerializedDataColumn, naming the PV, its encoding, and the way to reach it."""
    if isinstance(column, common_pb2.SerializedDataColumn):
        raise ValueError(
            f"DataBucket for PV '{bucket.pvName}' holds a SerializedDataColumn (encoding {column.encoding!r}), "
            f"whose payload is opaque, so it cannot be {what}; read it with bucket_column() and decode it per its "
            f"encoding"
        )


def _aligned_values(bucket: common_pb2.DataBucket, column: Any, n_rows: int) -> list:
    """
    Converts a bucket's column and checks it holds exactly one sample per timestamp.

    This is the read path's count rule (data_frame_conversions.column_values()), so an array column whose dims are
    missing or do not divide its values raises here rather than being sliced in the wrong-sized blocks.

    :param bucket: The DataBucket, named in the error.
    :param column: Its column, already known not to be serialized.
    :param n_rows: The number of timestamps on its axis.
    :return: One value per sample.
    :raises ValueError: if the column cannot be converted or its sample count differs from n_rows.
    """
    values = dfc.column_values(column)
    if len(values) != n_rows:
        raise ValueError(
            f"DataBucket for PV '{bucket.pvName}' has {len(values)} samples but its time axis has {n_rows} "
            f"timestamps; a bucket's column must be index-aligned with its axis"
        )
    return values


def bucket_values(bucket: common_pb2.DataBucket) -> list:
    """
    Extracts one Python value per sample from a bucket's column, in axis order.

    The mapping is data_frame_conversions.column_values()'s: native scalars, integer codes for an enum (its enumId is
    on bucket_column()), one list per sample for an array (dims via data_frame_conversions.column_dimensions()), one
    bytes payload per sample for an image, and None for a gap in a legacy DataColumn.

    :param bucket: The DataBucket.
    :return: One value per sample, parallel to bucket_timestamps().
    :raises ValueError: if the bucket is malformed or not aligned with its axis, or holds a SerializedDataColumn.
    """
    column = bucket_column(bucket)
    _refuse_serialized(bucket, column, "converted to values")
    return _aligned_values(bucket, column, len(bucket_timestamps(bucket)))


def _range_nanos(begin: TimestampInput, end: TimestampInput, caller: str) -> tuple[int, int]:
    """
    Converts a (begin, end) pair to epoch nanoseconds, requiring begin strictly before end.

    A reversed or empty range is rejected rather than trimming to nothing, which would be indistinguishable from a
    valid range that simply holds no samples.  The rule and the message's shape are data_frame._time_range()'s, but
    the message names the caller's own parameter (trim_bucket()'s begin/end, or time_range=).

    :param caller: How the range was passed, e.g. "trim_bucket()" or "time_range"; starts the error message.
    :raises ValueError: if begin is not strictly before end, or either bound is not a supported time input.
    """
    begin_nanos, end_nanos = to_epoch_nanos(to_timestamp(begin)), to_epoch_nanos(to_timestamp(end))
    if begin_nanos >= end_nanos:
        raise ValueError(
            f"{caller} requires begin strictly before end; got begin {begin_nanos} ns and end {end_nanos} ns"
        )
    return begin_nanos, end_nanos


def _first_out_of_order(epoch_nanos: list[int]) -> int | None:
    """Returns the first position whose timestamp is earlier than its predecessor's, or None if non-decreasing."""
    for position in range(1, len(epoch_nanos)):
        if epoch_nanos[position] < epoch_nanos[position - 1]:
            return position
    return None


def _has_samples_in(bucket: common_pb2.DataBucket, begin_nanos: int, end_nanos: int) -> bool:
    """Whether any of a bucket's timestamps falls in [begin, end).  Reads only the axis, so any column kind works."""
    return any(begin_nanos <= nanos < end_nanos for nanos in bucket_timestamps(bucket))


def _trim_nanos(bucket: common_pb2.DataBucket, begin_nanos: int, end_nanos: int) -> common_pb2.DataBucket | None:
    """trim_bucket() over an already-validated range in epoch nanoseconds."""
    column = bucket_column(bucket)
    _refuse_serialized(bucket, column, "trimmed")

    epoch_nanos = bucket_timestamps(bucket)
    # The read-side alignment check.  _slice_frame() assumes a validated frame, and on an array column with no dims
    # it would fall back to one value per row and slice silently wrong.
    _aligned_values(bucket, column, len(epoch_nanos))

    # A stored axis is non-decreasing (ingestion enforces it, repeats allowed), so the rows in [begin, end) are one
    # contiguous span and bisect_left finds both of its ends -- with repeats too: every copy of a timestamp equal to
    # begin is kept, and every copy equal to end is dropped.  A decreasing axis has no such span.
    out_of_order = _first_out_of_order(epoch_nanos)
    if out_of_order is not None:
        raise ValueError(
            f"DataBucket for PV '{bucket.pvName}' has a decreasing time axis at position {out_of_order} "
            f"({epoch_nanos[out_of_order]} ns follows {epoch_nanos[out_of_order - 1]} ns), so it cannot be trimmed "
            f"to a contiguous span"
        )

    start = bisect_left(epoch_nanos, begin_nanos)
    end = bisect_left(epoch_nanos, end_nanos)
    if start >= end:
        return None

    sliced = _slice_frame(bucket_to_data_frame(bucket), start, end)
    trimmed = common_pb2.DataBucket()
    trimmed.CopyFrom(bucket)
    trimmed.dataTimestamps.CopyFrom(sliced.dataTimestamps)
    # The copy has the same column arm set, so bucket_column() returns it in place, ready to overwrite.
    bucket_column(trimmed).CopyFrom(getattr(sliced, _COLUMN_FIELD_BY_TYPE[type(column)])[0])
    return trimmed


def trim_bucket(
    bucket: common_pb2.DataBucket, begin: TimestampInput, end: TimestampInput
) -> common_pb2.DataBucket | None:
    """
    Returns a copy of a bucket holding only its samples in the half-open range [begin, end).

    Exact: timestamps are compared as integer epoch nanoseconds, so a sample exactly at begin is kept and one exactly
    at end is dropped, as on the querySamples() path.  A SamplingClock bucket stays a SamplingClock with its start
    shifted in integer nanoseconds.  The PV, provider, and column metadata are carried over unchanged.

    This trims to [begin, end) only.  With a configuration selector the server can return a bucket spanning a gap
    between activation intervals, and trimming does not remove the samples in that gap.

    :param bucket: The DataBucket to trim.  It is not modified.
    :param begin: Inclusive start (tz-aware datetime, epoch seconds, or common.Timestamp).
    :param end: Exclusive end, strictly after begin.
    :return: The trimmed copy, or None when no sample falls in [begin, end).
    :raises ValueError: if begin is not strictly before end; if the bucket is malformed, holds a SerializedDataColumn
        (an opaque payload cannot be sliced), is not aligned with its axis, or has a decreasing time axis.
    """
    begin_nanos, end_nanos = _range_nanos(begin, end, "trim_bucket()")
    return _trim_nanos(bucket, begin_nanos, end_nanos)


# ----------------------------------------------------------------------
# many buckets
# ----------------------------------------------------------------------


def _first_nanos(bucket: common_pb2.DataBucket) -> int:
    """A bucket's first timestamp, read without expanding the axis."""
    _require_axis(bucket)
    axis = bucket.dataTimestamps
    if axis.WhichOneof("value") == "samplingClock":
        return to_epoch_nanos(axis.samplingClock.startTime)
    if not axis.timestampList.timestamps:
        raise ValueError(f"DataBucket for PV '{bucket.pvName}' has a time axis describing no timestamps")
    return to_epoch_nanos(axis.timestampList.timestamps[0])


def buckets_by_pv(buckets: Iterable[common_pb2.DataBucket]) -> dict[str, list[common_pb2.DataBucket]]:
    """
    Groups buckets by PV name, each PV's buckets ordered by first timestamp.

    PVs keep the order they are first seen in.  The sort is stable, so buckets with the same first timestamp keep
    their input order.  Today's server already returns buckets sorted by (pvName, first time), but does not promise
    to, so the grouping is done here; it also reassembles a PV whose buckets were split across pages or stream
    messages, once those are combined.

    Overlapping or duplicate timestamps across a PV's buckets are kept, not deduplicated: choosing between two
    ingests is not the client's call.

    :param buckets: The buckets, in any order (e.g. the data_buckets of every page).
    :return: A dict of PV name to that PV's buckets.
    :raises ValueError: if a bucket has no usable time axis.
    """
    groups: dict[str, list[common_pb2.DataBucket]] = {}
    for bucket in buckets:
        groups.setdefault(bucket.pvName, []).append(bucket)
    return {pv_name: sorted(group, key=_first_nanos) for pv_name, group in groups.items()}


def _structure(column: Any) -> tuple:
    """
    The fields of a column, other than its values, without which its values cannot be interpreted: its kind, plus
    an enum's enumId, an array's dims, an image's descriptor, or a struct's schemaId.
    """
    return (
        type(column).__name__,
        column.enumId if isinstance(column, common_pb2.EnumColumn) else None,
        tuple(dims) if (dims := dfc.column_dimensions(column)) is not None else None,
        tuple(sorted(descriptor.items())) if (descriptor := dfc.image_descriptor_dict(column)) is not None else None,
        dfc.column_schema_id(column),
    )


def _describe_structure(structure: tuple) -> str:
    """Renders a _structure() tuple for an error message, e.g. "EnumColumn (enumId 'mode:v1')"."""
    kind, enum_id, dims, descriptor, schema_id = structure
    details = []
    if enum_id is not None:
        details.append(f"enumId {enum_id!r}")
    if dims is not None:
        details.append(f"dims {list(dims)}")
    if descriptor is not None:
        details.append("image " + ", ".join(f"{key}={value!r}" for key, value in descriptor))
    if schema_id is not None:
        details.append(f"schemaId {schema_id!r}")
    return f"{kind} ({'; '.join(details)})" if details else kind


def _check_consistent(pv_name: str, buckets: list[common_pb2.DataBucket]) -> None:
    """
    Rejects a PV whose buckets differ in column kind or structure, which cannot be concatenated into one column.
    A legacy DataColumn carries no column-level type, so its buckets always pass (see buckets_to_dataframes()).

    Concatenating anyway would silently widen dtypes (double then int32) or mix payloads that mean different things
    (two enumerations, two array shapes).

    :raises ValueError: naming the PV and the first timestamps of the two buckets that differ.
    """
    first = buckets[0]
    expected = _structure(bucket_column(first))
    for bucket in buckets[1:]:
        actual = _structure(bucket_column(bucket))
        if actual != expected:
            raise ValueError(
                f"PV '{pv_name}' has buckets with different column kinds or structure (the bucket starting at "
                f"{_first_nanos(first)} ns has {_describe_structure(expected)}, the one starting at "
                f"{_first_nanos(bucket)} ns has {_describe_structure(actual)}), so they cannot form one column; "
                f"read them separately with bucket_to_data_frame() or bucket_values()"
            )


def _bucket_descriptor(bucket: common_pb2.DataBucket, epoch_nanos: list[int], exclude_column_metadata: bool) -> dict:
    """The per-bucket entry of df.attrs["buckets"]."""
    column = bucket_column(bucket)
    descriptor: dict[str, Any] = {
        "first_nanos": epoch_nanos[0],
        "last_nanos": epoch_nanos[-1],
        "sample_count": len(epoch_nanos),
        "provider_id": bucket.providerId,
        "provider_name": bucket.providerName,
        # The frame's column is named for the PV; this is the name the column was actually stored under.
        "column_name": column.name,
    }
    if not exclude_column_metadata:
        # None, not column_metadata_dict()'s empty containers, when the bucket carries no metadata at all -- as
        # when the query itself set excludeColumnMetadata, which a page's to_dataframes() cannot know.  An empty
        # summary would read as metadata the server returned.
        descriptor["column_metadata"] = dfc.column_metadata_dict(column) if column.HasField("metadata") else None
    return descriptor


def _pv_dataframe(pd: Any, pv_name: str, buckets: list[common_pb2.DataBucket], exclude_column_metadata: bool) -> Any:
    """Builds one PV's pandas DataFrame from its (already trimmed, ordered, consistent) buckets."""
    parts = []
    descriptors = []
    for bucket in buckets:
        frame = bucket_to_data_frame(bucket)
        part = dfc.data_frame_to_pandas(frame, exclude_column_metadata=True)
        # Ingestion names a bucket's column for its PV, but the stored name is not guaranteed to match; name it for
        # the PV regardless, so every part concatenates into the one column.  The stored name stays in
        # attrs["buckets"][i]["column_name"].
        part.columns = [pv_name]
        parts.append(part)
        descriptors.append(_bucket_descriptor(bucket, dfc.data_frame_timestamps(frame), exclude_column_metadata))

    df = pd.concat(parts, axis=0) if len(parts) > 1 else parts[0]

    # Rebuilt from scratch rather than inherited: how pd.concat propagates attrs has varied across pandas versions.
    column = bucket_column(buckets[0])
    attrs: dict[str, Any] = {"buckets": descriptors}
    if isinstance(column, common_pb2.EnumColumn):
        # Structural, not metadata: carried even under exclude_column_metadata, as data_frame_to_pandas() does.
        attrs["enum_ids"] = {pv_name: column.enumId}
    dims = dfc.column_dimensions(column)
    if dims is not None:
        attrs["dimensions"] = {pv_name: dims}
    descriptor = dfc.image_descriptor_dict(column)
    if descriptor is not None:
        attrs["image_descriptors"] = {pv_name: descriptor}
    schema_id = dfc.column_schema_id(column)
    if schema_id is not None:
        attrs["schema_ids"] = {pv_name: schema_id}
    if not exclude_column_metadata:
        # Each ingest carries its own metadata, so a PV's buckets can legitimately differ.  The per-PV summary is set
        # only when they agree; the per-bucket copies in attrs["buckets"] are always there.
        first_metadata = descriptors[0]["column_metadata"]
        if first_metadata is not None and all(entry["column_metadata"] == first_metadata for entry in descriptors[1:]):
            attrs["column_metadata"] = {pv_name: first_metadata}
    df.attrs = attrs
    return df


def buckets_to_dataframes(
    buckets: Iterable[common_pb2.DataBucket],
    *,
    time_range: tuple[TimestampInput, TimestampInput] | None = None,
    exclude_column_metadata: bool = False,
) -> dict[str, Any]:
    """
    Assembles buckets into one pandas DataFrame per PV.  Requires the optional [analysis] extra.

    Each frame has a UTC DatetimeIndex built from int64 epoch nanoseconds and one column, named for the PV, with the
    dtype its column type implies (float32 for a FloatColumn, int32 for an Int32Column or EnumColumn, ...).  Array,
    image, and struct samples are object cells, as in data_frame_conversions.data_frame_to_pandas().  A PV's buckets
    are concatenated in first-timestamp order (see buckets_by_pv()); overlapping buckets are kept, so the index can
    be non-monotonic or carry repeated instants.

    df.attrs carries:
      - "buckets": one dict per bucket in the frame, with 'first_nanos', 'last_nanos', 'sample_count',
        'provider_id', 'provider_name', 'column_name' (the name the column was stored under; the frame's column is
        always named for the PV), and (unless exclude_column_metadata) 'column_metadata': a dict, or None when the
        bucket carries no metadata -- e.g. because the query set exclude_column_metadata.
      - "column_metadata": {pv: dict}, only when every bucket carries metadata, all identical, and it is not
        excluded.
      - "enum_ids", "dimensions", "image_descriptors", "schema_ids": {pv: value} for the column kinds that carry
        them.  These are structural -- the values cannot be interpreted without them -- so they are present even
        under exclude_column_metadata.

    A legacy DataColumn is the exception to the consistency check: it has no column-level type, so its buckets are
    always treated as one structure even when their DataValues use different arms.  Their parts then concatenate
    with pandas' own widening -- integers with gaps, or ints in one bucket and doubles in another, become float64,
    and a mix with strings becomes object -- rather than raising.  Read such buckets with bucket_values() when the
    per-sample arm matters.

    :param buckets: The buckets, in any order (e.g. the data_buckets of every page or stream message).
    :param time_range: Optional (begin, end) to trim each bucket to [begin, end) exactly (see trim_bucket()); None
        (the default) keeps every bucket whole, as the server returned it.  A PV with no sample left in the range is
        absent from the result.
    :param exclude_column_metadata: When True, leave column metadata out of attrs.
    :return: A dict of PV name to pandas.DataFrame, in first-seen PV order.
    :raises ImportError: if the [analysis] extra is not installed.
    :raises ValueError: if time_range's begin is not before its end; if a PV's buckets differ in column kind or
        structure; or if a bucket is malformed, holds a SerializedDataColumn, or (when trimming) has a decreasing
        time axis.  With time_range, only buckets holding a sample in the range are checked: one lying wholly
        outside it is dropped, a serialized one included.
    """
    pd = _require_pandas()

    range_nanos = _range_nanos(*time_range, "time_range") if time_range is not None else None

    frames: dict[str, Any] = {}
    for pv_name, group in buckets_by_pv(buckets).items():
        if range_nanos is not None:
            # A serialized bucket with no sample in the range would be dropped by the trim like any other, so it is
            # refused only when it overlaps.  Refusing it up front made one serialized PV anywhere in a result --
            # even entirely outside the range -- block the conversion of every PV.
            group = [bucket for bucket in group if _has_samples_in(bucket, *range_nanos)]
            if not group:
                continue
        for bucket in group:
            _refuse_serialized(bucket, bucket_column(bucket), "converted to a pandas DataFrame")
        if range_nanos is not None:
            group = [trimmed for bucket in group if (trimmed := _trim_nanos(bucket, *range_nanos)) is not None]
        _check_consistent(pv_name, group)
        frames[pv_name] = _pv_dataframe(pd, pv_name, group, exclude_column_metadata)
    return frames


def query_buckets_to_dataframes(
    query_client: Any, params: Any, *, trim: bool = False, max_buckets: int | None = None
) -> dict[str, Any]:
    """
    Runs a unary bucket query, pages through the whole result with iter_query_buckets(), and assembles one pandas
    DataFrame per PV (see buckets_to_dataframes()).  Requires the optional [analysis] extra.

    There is deliberately no streaming counterpart: a PV can span stream messages, so a frame per message would be
    a PV fragment.  Stream callers collect the buckets of every message and call buckets_to_dataframes() once.

    :param query_client: A QueryClient.
    :param params: The QueryParams describing the query (must not carry a sample_status_filter).
    :param trim: When True, trim every bucket to the query's own [begin, end); by default buckets stay whole.
    :param max_buckets: Optional cap on the total number of buckets across all pages; raise once it would be
        exceeded (an unbounded "give me everything" is an out-of-memory foot-gun on large ranges).
    :return: A dict of PV name to pandas.DataFrame.
    :raises ValueError: if the result exceeds max_buckets, or the buckets cannot be assembled.
    :raises RuntimeError: if any page returns an error (propagated from iter_query_buckets()).
    """
    _require_pandas()

    buckets: list[common_pb2.DataBucket] = []
    for page in query_client.iter_query_buckets(params):
        buckets.extend(page.data_buckets)
        if max_buckets is not None and len(buckets) > max_buckets:
            raise ValueError(
                f"query result exceeds max_buckets={max_buckets} (at least {len(buckets)} buckets); narrow the "
                f"range or raise max_buckets"
            )

    time_range = (params.begin_timestamp, params.end_timestamp) if trim else None
    return buckets_to_dataframes(buckets, time_range=time_range, exclude_column_metadata=params.exclude_column_metadata)
