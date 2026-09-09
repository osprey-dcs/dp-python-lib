import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from assignment_spy import watch_assignments

from dp_python_lib.client.sample_status_client import (
    QuerySampleStatusesRequestParams,
    SampleStatusClient,
    SampleStatusColumn,
    SampleStatusFrame,
    SaveSampleStatusesRequestParams,
    sampling_clock,
    timestamp_list,
)
from dp_python_lib.grpc import annotation_pb2

BEGIN = datetime(2024, 1, 1, tzinfo=timezone.utc)
END = datetime(2024, 1, 2, tzinfo=timezone.utc)
BEGIN_EPOCH = int(BEGIN.timestamp())
END_EPOCH = int(END.timestamp())


def _client_with_mock_stub():
    """Build a SampleStatusClient whose gRPC stub is a Mock, avoiding any real network call."""
    client = SampleStatusClient(Mock(spec=grpc.Channel))
    client._stub = Mock()
    return client


def _save_response(saved_count=0):
    response = annotation_pb2.SaveSampleStatusesResponse()
    response.saveSampleStatusesResult.savedCount = saved_count
    return response


def _query_response(next_page_token="", pv_names=()):
    """Build a QuerySampleStatusesResponse carrying one bucket per supplied PV name."""
    response = annotation_pb2.QuerySampleStatusesResponse()
    result = response.querySampleStatusesResult
    result.nextPageToken = next_page_token
    for pv_name in pv_names:
        bucket = result.sampleStatusBuckets.add()
        bucket.statusColumn.pvName = pv_name
    return response


def _delete_response(deleted_count=0):
    response = annotation_pb2.DeleteSampleStatusesResponse()
    response.deleteSampleStatusesResult.deletedCount = deleted_count
    return response


def _exceptional(response_class, message):
    response = response_class()
    response.exceptionalResult.message = message
    return response


def _unrecognized(response_class):
    """A response with neither oneof arm set, exercising the 'unexpected response format' branch."""
    return response_class()


def _rpc_error(details="boom"):
    error = grpc.RpcError()
    error.details = Mock(return_value=details)
    return error


# ----------------------------------------------------------------------
# Time axis builders
# ----------------------------------------------------------------------


class TestSamplingClock(unittest.TestCase):
    def test_builds_axis(self):
        axis = sampling_clock(BEGIN, 1_000_000, 3)
        self.assertEqual(axis.WhichOneof("value"), "samplingClock")
        self.assertEqual(axis.samplingClock.startTime.epochSeconds, BEGIN_EPOCH)
        self.assertEqual(axis.samplingClock.periodNanos, 1_000_000)
        self.assertEqual(axis.samplingClock.count, 3)

    def test_rejects_non_positive_period(self):
        for period in (0, -1):
            with self.assertRaises(ValueError):
                sampling_clock(BEGIN, period, 3)

    def test_rejects_count_below_one(self):
        with self.assertRaises(ValueError):
            sampling_clock(BEGIN, 1_000_000, 0)


class TestTimestampList(unittest.TestCase):
    def test_builds_axis(self):
        axis = timestamp_list([100, 200, 300])
        self.assertEqual(axis.WhichOneof("value"), "timestampList")
        self.assertEqual([t.epochSeconds for t in axis.timestampList.timestamps], [100, 200, 300])

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            timestamp_list([])

    def test_rejects_non_increasing(self):
        with self.assertRaises(ValueError):
            timestamp_list([100, 300, 200])

    def test_rejects_duplicates(self):
        # Equal timestamps are not strictly increasing, and would be two statuses for one identity key.
        with self.assertRaises(ValueError):
            timestamp_list([100, 100])

    def test_rejects_sub_second_non_increasing(self):
        # Ordering must compare nanoseconds too, not just whole seconds.
        with self.assertRaises(ValueError):
            timestamp_list([100.000_002, 100.000_001])

    def test_accepts_sub_second_increasing(self):
        axis = timestamp_list([100.000_001, 100.000_002])
        nanos = [t.nanoseconds for t in axis.timestampList.timestamps]
        self.assertEqual(nanos, [1000, 2000])

    def test_rejects_naive_datetime(self):
        with self.assertRaises(ValueError):
            timestamp_list([datetime(2024, 1, 1)])  # noqa: DTZ001 -- naive input is the condition under test


# ----------------------------------------------------------------------
# SampleStatusColumn / SampleStatusFrame construction and validation
# ----------------------------------------------------------------------


class TestSampleStatusColumn(unittest.TestCase):
    def test_requires_pv_name(self):
        with self.assertRaises(ValueError):
            SampleStatusColumn("", [1])

    def test_requires_status_codes(self):
        with self.assertRaises(ValueError):
            SampleStatusColumn("ABC:1", [])

    def test_to_proto_carries_codes_and_confidence(self):
        column = SampleStatusColumn("ABC:1", [1, 2], confidence=[0.5, 0.25]).to_proto()
        self.assertEqual(column.pvName, "ABC:1")
        self.assertEqual(list(column.statusCodes), [1, 2])
        self.assertAlmostEqual(column.confidence[0], 0.5)
        self.assertAlmostEqual(column.confidence[1], 0.25)

    def test_all_empty_reasons_are_omitted(self):
        # The API directs clients to omit an all-empty reasons list rather than send empty strings.
        column = SampleStatusColumn("ABC:1", [1, 2], reasons=["", ""]).to_proto()
        self.assertEqual(list(column.reasons), [])

    def test_partially_populated_reasons_are_kept_whole(self):
        # A real reason alongside a blank must survive, including the blank that positions it.
        column = SampleStatusColumn("ABC:1", [1, 2], reasons=["", "spike"]).to_proto()
        self.assertEqual(list(column.reasons), ["", "spike"])


class TestSampleStatusFrame(unittest.TestCase):
    def _axis(self, count=2):
        return sampling_clock(BEGIN, 1_000_000, count)

    def test_requires_domain_and_layer(self):
        column = SampleStatusColumn("ABC:1", [1, 2])
        with self.assertRaises(ValueError):
            SampleStatusFrame("", "op", self._axis(), [column])
        with self.assertRaises(ValueError):
            SampleStatusFrame("dq", "", self._axis(), [column])

    def test_requires_columns(self):
        with self.assertRaises(ValueError):
            SampleStatusFrame("dq", "op", self._axis(), [])

    def test_rejects_duplicate_pv(self):
        columns = [SampleStatusColumn("ABC:1", [1, 2]), SampleStatusColumn("ABC:1", [3, 4])]
        with self.assertRaises(ValueError) as ctx:
            SampleStatusFrame("dq", "op", self._axis(), columns)
        self.assertIn("ABC:1", str(ctx.exception))

    def test_rejects_status_codes_length_mismatch(self):
        with self.assertRaises(ValueError) as ctx:
            SampleStatusFrame("dq", "op", self._axis(count=3), [SampleStatusColumn("ABC:1", [1, 2])])
        self.assertIn("ABC:1", str(ctx.exception))

    def test_rejects_confidence_length_mismatch(self):
        column = SampleStatusColumn("ABC:1", [1, 2], confidence=[0.5])
        with self.assertRaises(ValueError):
            SampleStatusFrame("dq", "op", self._axis(), [column])

    def test_rejects_reasons_length_mismatch(self):
        column = SampleStatusColumn("ABC:1", [1, 2], reasons=["a"])
        with self.assertRaises(ValueError):
            SampleStatusFrame("dq", "op", self._axis(), [column])

    def test_validates_against_timestamp_list_length(self):
        axis = timestamp_list([100, 200, 300])
        with self.assertRaises(ValueError):
            SampleStatusFrame("dq", "op", axis, [SampleStatusColumn("ABC:1", [1, 2])])

    def test_rejects_unset_time_axis(self):
        from dp_python_lib.grpc import common_pb2

        with self.assertRaises(ValueError):
            SampleStatusFrame("dq", "op", common_pb2.DataTimestamps(), [SampleStatusColumn("ABC:1", [1])])

    def test_rejects_zero_count_sampling_clock(self):
        # sampling_clock() makes this unreachable, but a hand-built DataTimestamps can carry count=0 -- and the
        # oneof still reports samplingClock as set, so it is not caught by the unset-axis check above.  The error
        # must name the empty axis rather than surfacing as a column-length mismatch.
        from dp_python_lib.grpc import common_pb2

        axis = common_pb2.DataTimestamps()
        axis.samplingClock.startTime.epochSeconds = BEGIN_EPOCH
        axis.samplingClock.periodNanos = 1_000_000
        axis.samplingClock.count = 0

        with self.assertRaises(ValueError) as ctx:
            SampleStatusFrame("dq", "op", axis, [SampleStatusColumn("ABC:1", [1])])
        self.assertIn("count", str(ctx.exception))

    def test_rejects_empty_timestamp_list(self):
        # As above for the other axis arm: an empty repeated field still reports timestampList as the set oneof.
        from dp_python_lib.grpc import common_pb2

        axis = common_pb2.DataTimestamps()
        axis.timestampList.SetInParent()

        with self.assertRaises(ValueError) as ctx:
            SampleStatusFrame("dq", "op", axis, [SampleStatusColumn("ABC:1", [1])])
        self.assertIn("timestamp", str(ctx.exception).lower())

    def test_to_proto(self):
        frame = SampleStatusFrame(
            "data_quality", "operator_override", self._axis(), [SampleStatusColumn("ABC:1", [1, 2])]
        ).to_proto()
        self.assertEqual(frame.domain, "data_quality")
        self.assertEqual(frame.layer, "operator_override")
        self.assertEqual(len(frame.statusColumns), 1)
        self.assertEqual(frame.statusColumns[0].pvName, "ABC:1")


# ----------------------------------------------------------------------
# Request params validation
# ----------------------------------------------------------------------


class TestSaveSampleStatusesRequestParams(unittest.TestCase):
    def test_requires_frames(self):
        with self.assertRaises(ValueError):
            SaveSampleStatusesRequestParams([])


class TestQuerySampleStatusesRequestParams(unittest.TestCase):
    def test_rejects_begin_not_before_end(self):
        with self.assertRaises(ValueError):
            QuerySampleStatusesRequestParams(END, BEGIN)
        with self.assertRaises(ValueError):
            QuerySampleStatusesRequestParams(BEGIN, BEGIN)

    def test_rejects_negative_limit(self):
        with self.assertRaises(ValueError):
            QuerySampleStatusesRequestParams(BEGIN, END, limit=-1)

    def test_allows_zero_limit(self):
        params = QuerySampleStatusesRequestParams(BEGIN, END, limit=0)
        self.assertEqual(params.limit, 0)

    def test_rejects_naive_datetime(self):
        with self.assertRaises(ValueError):
            QuerySampleStatusesRequestParams(datetime(2024, 1, 1), END)  # noqa: DTZ001 -- naive is under test


# ----------------------------------------------------------------------
# saveSampleStatuses
# ----------------------------------------------------------------------


class TestSaveSampleStatuses(unittest.TestCase):
    def setUp(self):
        self.client = _client_with_mock_stub()
        self.frame = SampleStatusFrame(
            "data_quality",
            "operator_override",
            sampling_clock(BEGIN, 1_000_000, 2),
            [SampleStatusColumn("ABC:1", [1, 2])],
        )

    def test_build_request(self):
        params = SaveSampleStatusesRequestParams([self.frame], source="detector", modified_by="ml")
        request = self.client._build_save_sample_statuses_request(params)
        self.assertEqual(len(request.frames), 1)
        self.assertEqual(request.frames[0].domain, "data_quality")
        self.assertEqual(request.source, "detector")
        self.assertEqual(request.modifiedBy, "ml")

    def test_build_request_omits_optional_provenance(self):
        request = self.client._build_save_sample_statuses_request(SaveSampleStatusesRequestParams([self.frame]))
        self.assertEqual(request.source, "")
        self.assertEqual(request.modifiedBy, "")

    def test_success(self):
        self.client._stub.saveSampleStatuses.return_value = _save_response(saved_count=7)
        result = self.client.save_sample_statuses(SaveSampleStatusesRequestParams([self.frame]))
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.saved_count, 7)
        self.client._stub.saveSampleStatuses.assert_called_once()

    def test_business_error(self):
        self.client._stub.saveSampleStatuses.return_value = _exceptional(
            annotation_pb2.SaveSampleStatusesResponse, "rejected"
        )
        result = self.client.save_sample_statuses(SaveSampleStatusesRequestParams([self.frame]))
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "rejected")
        self.assertIsNone(result.saved_count)

    def test_grpc_error(self):
        self.client._stub.saveSampleStatuses.side_effect = _rpc_error("unavailable")
        result = self.client.save_sample_statuses(SaveSampleStatusesRequestParams([self.frame]))
        self.assertTrue(result.result_status.is_error)
        self.assertIn("unavailable", result.result_status.message)

    def test_unexpected_error(self):
        self.client._stub.saveSampleStatuses.side_effect = RuntimeError("kaboom")
        result = self.client.save_sample_statuses(SaveSampleStatusesRequestParams([self.frame]))
        self.assertTrue(result.result_status.is_error)
        self.assertIn("kaboom", result.result_status.message)

    def test_unrecognized_response(self):
        self.client._stub.saveSampleStatuses.return_value = _unrecognized(annotation_pb2.SaveSampleStatusesResponse)
        result = self.client.save_sample_statuses(SaveSampleStatusesRequestParams([self.frame]))
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)


# ----------------------------------------------------------------------
# querySampleStatuses (unary + paging)
# ----------------------------------------------------------------------


class TestQuerySampleStatuses(unittest.TestCase):
    def setUp(self):
        self.client = _client_with_mock_stub()
        self.params = QuerySampleStatusesRequestParams(
            BEGIN, END, pv_names=["ABC:1"], domains=["dq"], layers=["op"], limit=50
        )

    def test_build_request(self):
        request = self.client._build_query_sample_statuses_request(self.params)
        self.assertEqual(request.timeRange.beginTime.epochSeconds, BEGIN_EPOCH)
        self.assertEqual(request.timeRange.endTime.epochSeconds, END_EPOCH)
        self.assertEqual(list(request.pvNames), ["ABC:1"])
        self.assertEqual(list(request.domains), ["dq"])
        self.assertEqual(list(request.layers), ["op"])
        self.assertEqual(request.limit, 50)
        self.assertEqual(request.pageToken, "")

    def test_build_request_limit_zero_is_set(self):
        # limit=0 is meaningful ("server picks a default"); a truthiness guard would drop it (cf. issue #13).
        # A proto3 scalar reads 0 whether or not it was assigned, so watch the assignment itself.
        params = QuerySampleStatusesRequestParams(BEGIN, END, limit=0)
        with watch_assignments(annotation_pb2, "QuerySampleStatusesRequest", "limit") as assigned:
            self.client._build_query_sample_statuses_request(params)
        self.assertEqual(assigned, [0], "limit=0 must be assigned, not dropped by a truthiness guard")

    def test_build_request_omits_empty_filters(self):
        params = QuerySampleStatusesRequestParams(BEGIN, END)
        request = self.client._build_query_sample_statuses_request(params)
        self.assertEqual(list(request.pvNames), [])
        self.assertEqual(list(request.domains), [])
        self.assertEqual(list(request.layers), [])

    def test_build_request_with_page_token(self):
        request = self.client._build_query_sample_statuses_request(self.params, page_token="tok")
        self.assertEqual(request.pageToken, "tok")

    def test_success(self):
        self.client._stub.querySampleStatuses.return_value = _query_response(pv_names=["ABC:1", "ABC:2"])
        result = self.client.query_sample_statuses(self.params)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(len(result.sample_status_buckets), 2)
        self.assertEqual(result.sample_status_buckets[0].statusColumn.pvName, "ABC:1")

    def test_business_error_accessors_are_empty(self):
        self.client._stub.querySampleStatuses.return_value = _exceptional(
            annotation_pb2.QuerySampleStatusesResponse, "bad range"
        )
        result = self.client.query_sample_statuses(self.params)
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.sample_status_buckets, [])
        self.assertEqual(result.next_page_token, "")

    def test_grpc_error(self):
        self.client._stub.querySampleStatuses.side_effect = _rpc_error("down")
        result = self.client.query_sample_statuses(self.params)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("down", result.result_status.message)

    def test_unexpected_error(self):
        self.client._stub.querySampleStatuses.side_effect = RuntimeError("kaboom")
        result = self.client.query_sample_statuses(self.params)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("kaboom", result.result_status.message)

    def test_unrecognized_response(self):
        self.client._stub.querySampleStatuses.return_value = _unrecognized(annotation_pb2.QuerySampleStatusesResponse)
        result = self.client.query_sample_statuses(self.params)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_iter_pages_until_token_exhausted(self):
        self.client._stub.querySampleStatuses.side_effect = [
            _query_response(next_page_token="t1", pv_names=["ABC:1"]),
            _query_response(next_page_token="", pv_names=["ABC:2"]),
        ]
        pages = list(self.client.iter_sample_statuses(self.params))
        self.assertEqual(len(pages), 2)
        # The second call must carry the token the first page returned.
        second_request = self.client._stub.querySampleStatuses.call_args_list[1][0][0]
        self.assertEqual(second_request.pageToken, "t1")

    def test_iter_raises_on_page_error(self):
        self.client._stub.querySampleStatuses.side_effect = [
            _query_response(next_page_token="t1", pv_names=["ABC:1"]),
            _exceptional(annotation_pb2.QuerySampleStatusesResponse, "page blew up"),
        ]
        iterator = self.client.iter_sample_statuses(self.params)
        next(iterator)
        with self.assertRaises(RuntimeError) as ctx:
            next(iterator)
        self.assertIn("page blew up", str(ctx.exception))

    def test_iter_single_empty_page_is_not_an_error(self):
        self.client._stub.querySampleStatuses.return_value = _query_response()
        pages = list(self.client.iter_sample_statuses(self.params))
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].sample_status_buckets, [])


# ----------------------------------------------------------------------
# querySampleStatusesStream
# ----------------------------------------------------------------------


class TestQuerySampleStatusesStream(unittest.TestCase):
    def setUp(self):
        self.client = _client_with_mock_stub()
        self.params = QuerySampleStatusesRequestParams(BEGIN, END)

    def test_never_sends_page_token(self):
        self.client._stub.querySampleStatusesStream.return_value = iter([_query_response(pv_names=["ABC:1"])])
        list(self.client.iter_sample_statuses_stream(self.params))
        request = self.client._stub.querySampleStatusesStream.call_args[0][0]
        self.assertEqual(request.pageToken, "")

    def test_yields_each_message(self):
        self.client._stub.querySampleStatusesStream.return_value = iter(
            [_query_response(pv_names=["ABC:1"]), _query_response(pv_names=["ABC:2"])]
        )
        messages = list(self.client.iter_sample_statuses_stream(self.params))
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[1].sample_status_buckets[0].statusColumn.pvName, "ABC:2")

    def test_is_lazy(self):
        # A generator that would raise on the second pull proves nothing is consumed before it is requested.
        def responses():
            yield _query_response(pv_names=["ABC:1"])
            raise AssertionError("stream consumed eagerly")

        self.client._stub.querySampleStatusesStream.return_value = responses()
        iterator = self.client.iter_sample_statuses_stream(self.params)
        first = next(iterator)
        self.assertEqual(first.sample_status_buckets[0].statusColumn.pvName, "ABC:1")

    def test_raises_on_mid_stream_business_error(self):
        self.client._stub.querySampleStatusesStream.return_value = iter(
            [
                _query_response(pv_names=["ABC:1"]),
                _exceptional(annotation_pb2.QuerySampleStatusesResponse, "stream failed"),
            ]
        )
        iterator = self.client.iter_sample_statuses_stream(self.params)
        next(iterator)
        with self.assertRaises(RuntimeError) as ctx:
            next(iterator)
        self.assertIn("stream failed", str(ctx.exception))

    def test_raises_on_grpc_error(self):
        self.client._stub.querySampleStatusesStream.side_effect = _rpc_error("stream down")
        with self.assertRaises(RuntimeError) as ctx:
            list(self.client.iter_sample_statuses_stream(self.params))
        self.assertIn("stream down", str(ctx.exception))

    def test_raises_on_unrecognized_message(self):
        self.client._stub.querySampleStatusesStream.return_value = iter(
            [_unrecognized(annotation_pb2.QuerySampleStatusesResponse)]
        )
        with self.assertRaises(RuntimeError) as ctx:
            list(self.client.iter_sample_statuses_stream(self.params))
        self.assertIn("Unexpected response format", str(ctx.exception))


# ----------------------------------------------------------------------
# deleteSampleStatuses
# ----------------------------------------------------------------------


class TestDeleteSampleStatuses(unittest.TestCase):
    def setUp(self):
        self.client = _client_with_mock_stub()

    def test_build_request_with_pv_names(self):
        request = self.client._build_delete_sample_statuses_request(BEGIN, END, "dq", "op", pv_names=["ABC:1", "ABC:2"])
        self.assertEqual(request.domain, "dq")
        self.assertEqual(request.layer, "op")
        self.assertEqual(list(request.pvNames), ["ABC:1", "ABC:2"])
        self.assertEqual(request.timeRange.beginTime.epochSeconds, BEGIN_EPOCH)

    def test_build_request_all_pvs_sends_empty_wildcard(self):
        request = self.client._build_delete_sample_statuses_request(BEGIN, END, "dq", "op", all_pvs=True)
        self.assertEqual(list(request.pvNames), [])

    def test_requires_explicit_pv_scope(self):
        # Omitting both must raise rather than silently becoming the delete-everything wildcard.
        with self.assertRaises(ValueError) as ctx:
            self.client.delete_sample_statuses(BEGIN, END, "dq", "op")
        self.assertIn("all_pvs=True", str(ctx.exception))
        self.client._stub.deleteSampleStatuses.assert_not_called()

    def test_rejects_both_pv_names_and_all_pvs(self):
        with self.assertRaises(ValueError):
            self.client.delete_sample_statuses(BEGIN, END, "dq", "op", pv_names=["ABC:1"], all_pvs=True)

    def test_requires_domain_and_layer(self):
        with self.assertRaises(ValueError):
            self.client.delete_sample_statuses(BEGIN, END, "", "op", pv_names=["ABC:1"])
        with self.assertRaises(ValueError):
            self.client.delete_sample_statuses(BEGIN, END, "dq", "", pv_names=["ABC:1"])

    def test_rejects_begin_not_before_end(self):
        with self.assertRaises(ValueError):
            self.client.delete_sample_statuses(END, BEGIN, "dq", "op", pv_names=["ABC:1"])

    def test_success(self):
        self.client._stub.deleteSampleStatuses.return_value = _delete_response(deleted_count=4)
        result = self.client.delete_sample_statuses(BEGIN, END, "dq", "op", pv_names=["ABC:1"])
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.deleted_count, 4)

    def test_zero_deleted_is_success_not_error(self):
        self.client._stub.deleteSampleStatuses.return_value = _delete_response(deleted_count=0)
        result = self.client.delete_sample_statuses(BEGIN, END, "dq", "op", pv_names=["ABC:1"])
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.deleted_count, 0)

    def test_business_error(self):
        self.client._stub.deleteSampleStatuses.return_value = _exceptional(
            annotation_pb2.DeleteSampleStatusesResponse, "nope"
        )
        result = self.client.delete_sample_statuses(BEGIN, END, "dq", "op", pv_names=["ABC:1"])
        self.assertTrue(result.result_status.is_error)
        self.assertIsNone(result.deleted_count)

    def test_grpc_error(self):
        self.client._stub.deleteSampleStatuses.side_effect = _rpc_error("gone")
        result = self.client.delete_sample_statuses(BEGIN, END, "dq", "op", pv_names=["ABC:1"])
        self.assertTrue(result.result_status.is_error)
        self.assertIn("gone", result.result_status.message)

    def test_unexpected_error(self):
        self.client._stub.deleteSampleStatuses.side_effect = RuntimeError("kaboom")
        result = self.client.delete_sample_statuses(BEGIN, END, "dq", "op", pv_names=["ABC:1"])
        self.assertTrue(result.result_status.is_error)
        self.assertIn("kaboom", result.result_status.message)

    def test_unrecognized_response(self):
        self.client._stub.deleteSampleStatuses.return_value = _unrecognized(annotation_pb2.DeleteSampleStatusesResponse)
        result = self.client.delete_sample_statuses(BEGIN, END, "dq", "op", pv_names=["ABC:1"])
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)


# ----------------------------------------------------------------------
# Client construction
# ----------------------------------------------------------------------


class TestSampleStatusClientConstruction(unittest.TestCase):
    def test_creates_stub_once(self):
        channel = Mock(spec=grpc.Channel)
        client = SampleStatusClient(channel)
        self.assertIsNotNone(client._stub)
        self.assertIs(client._channel, channel)


if __name__ == "__main__":
    unittest.main()
