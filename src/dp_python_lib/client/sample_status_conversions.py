"""
Pythonic conversions for sample status query results (issue #8).

Turns the raw protobuf SampleStatusBucket objects returned by SampleStatusClient into flat, per-sample Python rows.
A bucket is one PV's statuses over one time axis in one (domain, layer); this module expands that compact form into
the per-sample view that most consumers actually want -- one row per labeled sample.

Design decisions:
  - SamplingClock axis expansion is computed in INTEGER NANOSECONDS (startTime + i * periodNanos), never in float
    seconds.  Status-to-sample matching is by exact timestamp at nanosecond precision, so a float round-trip would
    produce timestamps that no longer match the samples they label.  A double carries 53 bits of mantissa; epoch
    nanoseconds today need ~61 bits, so float seconds cannot represent them exactly.
  - Absent optional fields surface as None, not as fabricated zeros or empty strings.  Absence means "no assertion"
    throughout the sample status model, and 0.0 is a legitimate confidence value -- conflating the two would invent
    an assertion the producer never made.
  - Parallel-array lengths are validated fail-loud, mirroring query_conversions._check_column_alignment().

This module intentionally has no third-party dependencies: it returns plain Python objects and does not require the
optional [analysis] extra.  A pandas view is deferred to a follow-up.
"""

from collections.abc import Iterator

from dp_python_lib.client.time_conversions import NANOS_PER_SECOND, to_epoch_nanos
from dp_python_lib.grpc import common_pb2

# The Timestamp -> epoch-nanoseconds conversion is shared (see time_conversions.to_epoch_nanos); this private
# alias is the name this module has always used internally.
_timestamp_to_nanos = to_epoch_nanos


def _nanos_to_timestamp(epoch_nanos: int) -> common_pb2.Timestamp:
    """
    Converts integer epoch nanoseconds back into a common.Timestamp.
    :param epoch_nanos: Epoch nanoseconds.
    :return: The equivalent common.Timestamp.
    """
    timestamp = common_pb2.Timestamp()
    timestamp.epochSeconds, timestamp.nanoseconds = divmod(epoch_nanos, NANOS_PER_SECOND)
    return timestamp


def expand_data_timestamps(data_timestamps: common_pb2.DataTimestamps) -> list[int]:
    """
    Expands a DataTimestamps time axis into an explicit list of epoch-nanosecond values, one per sample.

    A SamplingClock is expanded arithmetically as startTime + i * periodNanos for i in [0, count), computed in
    integer nanoseconds so the result reproduces the producer's timestamps exactly.  Computing in float seconds
    would defeat the API's exact-match contract: matching is by exact timestamp at nanosecond precision, and a
    float64 cannot represent present-day epoch nanoseconds without loss.

    A TimestampList is returned as-is, converted to nanoseconds.

    :param data_timestamps: The time axis to expand (either oneof arm).
    :return: Epoch nanoseconds for each sample on the axis, in axis order.
    :raises ValueError: if neither axis arm is set, or if a SamplingClock has a non-positive period or count.
    """
    axis = data_timestamps.WhichOneof("value")

    if axis == "samplingClock":
        clock = data_timestamps.samplingClock
        if clock.periodNanos <= 0:
            raise ValueError(f"SamplingClock requires periodNanos > 0, got {clock.periodNanos}")
        if clock.count < 1:
            raise ValueError(f"SamplingClock requires count >= 1, got {clock.count}")
        start_nanos = _timestamp_to_nanos(clock.startTime)
        period = clock.periodNanos
        return [start_nanos + i * period for i in range(clock.count)]

    if axis == "timestampList":
        return [_timestamp_to_nanos(t) for t in data_timestamps.timestampList.timestamps]

    raise ValueError("DataTimestamps must specify either a samplingClock or a timestampList")


class SampleStatusRow:
    """
    One sample's status: the flat, per-sample view of a SampleStatusBucket.

    confidence and reason are None when the producer did not supply them.  That is a deliberate distinction rather
    than a convenience: absence means "no assertion" in this API, and 0.0 is a legitimate confidence value, so
    defaulting absent confidence to 0.0 (or an absent reason to "") would fabricate an assertion.
    """

    __slots__ = ("confidence", "domain", "epoch_nanos", "layer", "pv_name", "reason", "status_code")

    def __init__(
        self,
        pv_name: str,
        epoch_nanos: int,
        status_code: int,
        domain: str,
        layer: str,
        confidence: float | None = None,
        reason: str | None = None,
    ) -> None:
        """
        :param pv_name: Name of the PV this status applies to.
        :param epoch_nanos: Timestamp of the labeled sample, as integer epoch nanoseconds.
        :param status_code: The int32 status code, whose meaning is defined by the domain.
        :param domain: The domain naming the status-code semantics contract.
        :param layer: The layer naming the producer stream that assigned the status.
        :param confidence: The producer's confidence for this sample, or None if not supplied.
        :param reason: The producer's reason for this sample, or None if not supplied.
        """
        self.pv_name = pv_name
        self.epoch_nanos = epoch_nanos
        self.status_code = status_code
        self.domain = domain
        self.layer = layer
        self.confidence = confidence
        self.reason = reason

    @property
    def timestamp(self) -> common_pb2.Timestamp:
        """This row's timestamp as a common.Timestamp, for round-tripping into a new save or delete request."""
        return _nanos_to_timestamp(self.epoch_nanos)

    def __repr__(self) -> str:
        return (
            f"SampleStatusRow(pv_name={self.pv_name!r}, epoch_nanos={self.epoch_nanos}, "
            f"status_code={self.status_code}, domain={self.domain!r}, layer={self.layer!r}, "
            f"confidence={self.confidence!r}, reason={self.reason!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SampleStatusRow):
            return NotImplemented
        return (
            self.pv_name == other.pv_name
            and self.epoch_nanos == other.epoch_nanos
            and self.status_code == other.status_code
            and self.domain == other.domain
            and self.layer == other.layer
            and self.confidence == other.confidence
            and self.reason == other.reason
        )


def _check_alignment(bucket: common_pb2.SampleStatusBucket, field_name: str, actual: int, expected: int) -> None:
    """
    Raises ValueError if a bucket's parallel array is populated but not one-entry-per-timestamp.
    :param bucket: The bucket being converted, used to name the offending PV in the error.
    :param field_name: Name of the parallel array being checked.
    :param actual: The array's actual length.
    :param expected: The number of timestamps on the bucket's time axis.
    """
    if actual != expected:
        raise ValueError(
            f"SampleStatusBucket for PV {bucket.statusColumn.pvName!r} (domain={bucket.domain!r}, "
            f"layer={bucket.layer!r}) has {actual} {field_name} but its time axis has {expected} timestamps; "
            f"one entry per timestamp is required"
        )


def bucket_to_rows(bucket: common_pb2.SampleStatusBucket) -> list[SampleStatusRow]:
    """
    Expands a single SampleStatusBucket into one SampleStatusRow per labeled sample.

    The bucket's compact form -- a time axis plus parallel arrays -- is expanded so each row carries its own exact
    timestamp, status code, and the bucket's domain and layer.  Timestamps are computed in integer nanoseconds (see
    expand_data_timestamps()), so a row's timestamp matches the sample it labels exactly.

    An omitted confidence or reasons array yields None for every row, and an empty reason string within a populated
    reasons array also yields None -- both mean "no reason for this sample".

    :param bucket: The SampleStatusBucket to expand.
    :return: One SampleStatusRow per sample, in time-axis order.
    :raises ValueError: if the time axis is unset or invalid, or if any populated parallel array does not supply
        exactly one entry per timestamp.
    """
    epoch_nanos_list = expand_data_timestamps(bucket.dataTimestamps)
    n_timestamps = len(epoch_nanos_list)

    column = bucket.statusColumn
    _check_alignment(bucket, "statusCodes", len(column.statusCodes), n_timestamps)

    # An omitted optional array is the normal case, not an error; a *populated* one must align.
    confidence = list(column.confidence) if len(column.confidence) else None
    if confidence is not None:
        _check_alignment(bucket, "confidence values", len(confidence), n_timestamps)

    reasons = list(column.reasons) if len(column.reasons) else None
    if reasons is not None:
        _check_alignment(bucket, "reasons", len(reasons), n_timestamps)

    rows = []
    for i in range(n_timestamps):
        # An empty reason string inside a populated array means "no reason for this sample", so it maps to None
        # rather than to "" -- the same absence-means-no-assertion rule the omitted-array case follows.
        reason = reasons[i] if reasons is not None and reasons[i] else None
        rows.append(
            SampleStatusRow(
                pv_name=column.pvName,
                epoch_nanos=epoch_nanos_list[i],
                status_code=column.statusCodes[i],
                domain=bucket.domain,
                layer=bucket.layer,
                confidence=confidence[i] if confidence is not None else None,
                reason=reason,
            )
        )
    return rows


def buckets_to_rows(buckets: list[common_pb2.SampleStatusBucket]) -> list[SampleStatusRow]:
    """
    Expands a list of SampleStatusBucket objects (e.g. one query page's sample_status_buckets) into a flat list of
    per-sample rows, preserving bucket order and within-bucket time-axis order.

    Rows from different buckets are not merged, sorted, or deduplicated: two buckets may legitimately carry statuses
    for the same PV and timestamp in different (domain, layer) pairs, which are distinct statuses under the API's
    (pvName, timestamp, domain, layer) identity key.

    :param buckets: The buckets to expand.
    :return: The concatenated per-sample rows.
    :raises ValueError: if any bucket is misaligned or carries an unset time axis.
    """
    rows: list[SampleStatusRow] = []
    for bucket in buckets:
        rows.extend(bucket_to_rows(bucket))
    return rows


def iter_rows(results: Iterator) -> Iterator[SampleStatusRow]:
    """
    Lazily flattens an iterator of QuerySampleStatusesApiResult pages (from SampleStatusClient.iter_sample_statuses()
    or iter_sample_statuses_stream()) into a single stream of per-sample rows.

    Laziness is preserved end to end -- pages are expanded as they arrive, so a large query never materializes every
    row at once.  Page errors are not swallowed here: both iter_* methods raise RuntimeError on an error page, and
    that exception propagates through this generator.

    :param results: An iterator of QuerySampleStatusesApiResult objects.
    :return: A lazy iterator over the per-sample rows of every page.
    """
    for result in results:
        yield from buckets_to_rows(result.sample_status_buckets)
