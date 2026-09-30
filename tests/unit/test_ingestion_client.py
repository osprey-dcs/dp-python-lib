import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client import data_frame as dfb
from dp_python_lib.client.ingestion_client import (
    REQUEST_STATUS_CLOCK_SKEW,
    IngestDataRequestParams,
    IngestionClient,
    IngestionRequestStatus,
    RegisterProviderApiResult,
    RegisterProviderRequestParams,
    chunked_request_params,
)
from dp_python_lib.client.ingestion_client import RequestStatusQuery as RS
from dp_python_lib.client.time_conversions import to_epoch_nanos
from dp_python_lib.grpc import common_pb2, ingestion_pb2


class TestIngestionClient(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures before each test method."""
        self.mock_channel = Mock()
        self.client = IngestionClient(self.mock_channel)

    def test_build_register_provider_request_all_fields(self):
        """Test _build_register_provider_request with all fields populated."""
        params = RegisterProviderRequestParams(
            name="test_provider",
            description="Test provider description",
            tag_list=["tag1", "tag2", "tag3"],
            attribute_map={"key1": "value1", "key2": "value2"},
        )

        request = self.client._build_register_provider_request(params)

        # Verify request type
        self.assertIsInstance(request, ingestion_pb2.RegisterProviderRequest)

        # Verify required field
        self.assertEqual(request.providerName, "test_provider")

        # Verify optional description
        self.assertEqual(request.description, "Test provider description")

        # Verify tags
        self.assertEqual(list(request.tags), ["tag1", "tag2", "tag3"])

        # Verify attributes
        self.assertEqual(len(request.attributes), 2)

        # Check attribute contents (order may vary)
        attr_dict = {attr.name: attr.value for attr in request.attributes}
        self.assertEqual(attr_dict, {"key1": "value1", "key2": "value2"})

    def test_build_register_provider_request_name_only(self):
        """Test _build_register_provider_request with only required name field."""
        params = RegisterProviderRequestParams(
            name="minimal_provider", description=None, tag_list=None, attribute_map=None
        )

        request = self.client._build_register_provider_request(params)

        # Verify request type and required field
        self.assertIsInstance(request, ingestion_pb2.RegisterProviderRequest)
        self.assertEqual(request.providerName, "minimal_provider")

        # Verify optional fields are not set or empty
        self.assertEqual(request.description, "")
        self.assertEqual(len(request.tags), 0)
        self.assertEqual(len(request.attributes), 0)

    def test_build_register_provider_request_empty_description(self):
        """Test _build_register_provider_request with empty description."""
        params = RegisterProviderRequestParams(name="test_provider", description="", tag_list=None, attribute_map=None)

        request = self.client._build_register_provider_request(params)

        self.assertEqual(request.providerName, "test_provider")
        # Empty string should not be set as description
        self.assertEqual(request.description, "")

    def test_build_register_provider_request_empty_collections(self):
        """Test _build_register_provider_request with empty tag_list and attribute_map."""
        params = RegisterProviderRequestParams(
            name="test_provider", description="Test description", tag_list=[], attribute_map={}
        )

        request = self.client._build_register_provider_request(params)

        self.assertEqual(request.providerName, "test_provider")
        self.assertEqual(request.description, "Test description")
        self.assertEqual(len(request.tags), 0)
        self.assertEqual(len(request.attributes), 0)

    def test_build_register_provider_request_single_tag(self):
        """Test _build_register_provider_request with single tag."""
        params = RegisterProviderRequestParams(
            name="test_provider", description=None, tag_list=["single_tag"], attribute_map=None
        )

        request = self.client._build_register_provider_request(params)

        self.assertEqual(request.providerName, "test_provider")
        self.assertEqual(list(request.tags), ["single_tag"])
        self.assertEqual(len(request.attributes), 0)

    def test_build_register_provider_request_single_attribute(self):
        """Test _build_register_provider_request with single attribute."""
        params = RegisterProviderRequestParams(
            name="test_provider",
            description=None,
            tag_list=None,
            attribute_map={"single_key": "single_value"},
        )

        request = self.client._build_register_provider_request(params)

        self.assertEqual(request.providerName, "test_provider")
        self.assertEqual(len(request.tags), 0)
        self.assertEqual(len(request.attributes), 1)

        attr = request.attributes[0]
        self.assertIsInstance(attr, common_pb2.Attribute)
        self.assertEqual(attr.name, "single_key")
        self.assertEqual(attr.value, "single_value")

    def test_build_register_provider_request_multiple_attributes(self):
        """Test _build_register_provider_request with multiple attributes."""
        params = RegisterProviderRequestParams(
            name="test_provider",
            description=None,
            tag_list=None,
            attribute_map={"attr1": "val1", "attr2": "val2", "attr3": "val3"},
        )

        request = self.client._build_register_provider_request(params)

        self.assertEqual(len(request.attributes), 3)

        # Verify all attributes are present
        attr_dict = {attr.name: attr.value for attr in request.attributes}
        expected = {"attr1": "val1", "attr2": "val2", "attr3": "val3"}
        self.assertEqual(attr_dict, expected)

        # Verify all are Attribute objects
        for attr in request.attributes:
            self.assertIsInstance(attr, common_pb2.Attribute)

    def test_send_register_provider_success(self):
        """Test _send_register_provider with successful response."""
        # Create mock response with registrationResult
        mock_response = Mock()
        mock_response.HasField = Mock()
        mock_response.HasField.side_effect = lambda field: field == "registrationResult"

        # Setup mock stub on the client (stub is created once in __init__ and reused)
        mock_stub = Mock()
        mock_stub.registerProvider.return_value = mock_response
        self.client._stub = mock_stub

        # Create test request
        request = ingestion_pb2.RegisterProviderRequest()
        request.providerName = "test_provider"

        # Call method
        result = self.client._send_register_provider(request)

        # Verify results
        self.assertIsInstance(result, RegisterProviderApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "")
        self.assertEqual(result.response, mock_response)

        # Verify stub was called correctly
        mock_stub.registerProvider.assert_called_once_with(request)

    def test_send_register_provider_exceptional_result(self):
        """Test _send_register_provider with exceptionalResult (business error)."""
        # Create mock response with exceptionalResult
        mock_response = Mock()
        mock_response.HasField = Mock()
        mock_response.HasField.side_effect = lambda field: field == "exceptionalResult"
        mock_response.exceptionalResult.message = "Provider name already exists"

        # Setup mock stub on the client (stub is created once in __init__ and reused)
        mock_stub = Mock()
        mock_stub.registerProvider.return_value = mock_response
        self.client._stub = mock_stub

        # Create test request
        request = ingestion_pb2.RegisterProviderRequest()
        request.providerName = "duplicate_provider"

        # Call method
        result = self.client._send_register_provider(request)

        # Verify results
        self.assertIsInstance(result, RegisterProviderApiResult)
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "Provider name already exists")
        self.assertIsNone(result.response)

    def test_send_register_provider_unexpected_response(self):
        """Test _send_register_provider with unexpected response format."""
        # Create mock response with neither field
        mock_response = Mock()
        mock_response.HasField = Mock(return_value=False)

        # Setup mock stub on the client (stub is created once in __init__ and reused)
        mock_stub = Mock()
        mock_stub.registerProvider.return_value = mock_response
        self.client._stub = mock_stub

        # Create test request
        request = ingestion_pb2.RegisterProviderRequest()
        request.providerName = "test_provider"

        # Call method
        result = self.client._send_register_provider(request)

        # Verify results
        self.assertIsInstance(result, RegisterProviderApiResult)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)
        self.assertIsNone(result.response)

    def test_send_register_provider_grpc_error(self):
        """Test _send_register_provider with gRPC RpcError."""
        # Setup mock stub to raise gRPC error
        mock_stub = Mock()
        mock_grpc_error = grpc.RpcError()
        mock_grpc_error.details = Mock(return_value="Connection timeout")
        mock_stub.registerProvider.side_effect = mock_grpc_error
        self.client._stub = mock_stub

        # Create test request
        request = ingestion_pb2.RegisterProviderRequest()
        request.providerName = "test_provider"

        # Call method
        result = self.client._send_register_provider(request)

        # Verify results
        self.assertIsInstance(result, RegisterProviderApiResult)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: Connection timeout", result.result_status.message)
        self.assertIsNone(result.response)

    def test_send_register_provider_general_exception(self):
        """Test _send_register_provider with general exception."""
        # Setup mock stub to raise general exception
        mock_stub = Mock()
        mock_stub.registerProvider.side_effect = ValueError("Invalid parameter")
        self.client._stub = mock_stub

        # Create test request
        request = ingestion_pb2.RegisterProviderRequest()
        request.providerName = "test_provider"

        # Call method
        result = self.client._send_register_provider(request)

        # Verify results
        self.assertIsInstance(result, RegisterProviderApiResult)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: Invalid parameter", result.result_status.message)
        self.assertIsNone(result.response)


# ----------------------------------------------------------------------
# #17: data ingestion and request status
# ----------------------------------------------------------------------

PROVIDER = "provider-1"
SINCE = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
_RequestStatus = ingestion_pb2.QueryRequestStatusResponse.RequestStatusResult.RequestStatus


def _frame(count=2):
    return dfb.data_frame(
        dfb.sampling_clock(1_770_000_000, 1_000, count), [dfb.double_column("pv", [float(i) for i in range(count)])]
    )


def _params(request_id="req-1"):
    return IngestDataRequestParams(PROVIDER, _frame(), client_request_id=request_id)


class _FakeRpcError(grpc.RpcError):
    """An RpcError with a resolvable code(), unlike the bare grpc.RpcError() the older tests raise."""

    def __init__(self, code, details):
        super().__init__()
        self._code, self._details = code, details

    def code(self):
        return self._code

    def details(self):
        return self._details


def _ack(request_id, rows=2, columns=1):
    response = ingestion_pb2.IngestDataResponse(providerId=PROVIDER, clientRequestId=request_id)
    response.ackResult.numRows, response.ackResult.numColumns = rows, columns
    return response


def _reject(request_id, message="bad request"):
    response = ingestion_pb2.IngestDataResponse(providerId=PROVIDER, clientRequestId=request_id)
    response.exceptionalResult.message = message
    return response


def _status_response(*documents):
    response = ingestion_pb2.QueryRequestStatusResponse()
    response.requestStatusResult.requestStatus.extend(documents)
    return response


def _document(request_id, status=IngestionRequestStatus.SUCCESS):
    return _RequestStatus(providerId=PROVIDER, requestId=request_id, ingestionRequestStatus=int(status))


class _ClientTestCase(unittest.TestCase):
    def setUp(self):
        self.client = IngestionClient(Mock())
        self.stub = Mock()
        self.client._stub = self.stub


class TestRegisterProviderResultProperties(unittest.TestCase):
    def test_success_exposes_id_and_newness(self):
        response = ingestion_pb2.RegisterProviderResponse()
        response.registrationResult.providerId = "p-1"
        response.registrationResult.isNewProvider = True
        result = RegisterProviderApiResult(is_error=False, message="", response=response)
        self.assertEqual((result.provider_id, result.is_new_provider), ("p-1", True))

    def test_error_has_neither(self):
        result = RegisterProviderApiResult(is_error=True, message="nope")
        self.assertIsNone(result.provider_id)
        self.assertIsNone(result.is_new_provider)


class TestIngestDataRequestParams(unittest.TestCase):
    def test_generates_a_uuid_request_id_by_default(self):
        first, second = IngestDataRequestParams(PROVIDER, _frame()), IngestDataRequestParams(PROVIDER, _frame())
        self.assertEqual(len(first.client_request_id), 36)
        self.assertNotEqual(first.client_request_id, second.client_request_id)

    def test_keeps_an_explicit_request_id(self):
        self.assertEqual(_params("mine").client_request_id, "mine")

    def test_rejects_blank_ids(self):
        for kwargs in (
            {"provider_id": " "},
            {"provider_id": ""},
            {"client_request_id": "\t"},
            {"client_request_id": ""},
        ):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                IngestDataRequestParams(**{"provider_id": PROVIDER, "frame": _frame(), **kwargs})

    def test_id_length_is_capped_at_the_chunking_budget(self):
        # split_data_frame() reserves room for ids of MAX_BUDGETED_ID_CHARS; a longer one could overrun max_bytes.
        IngestDataRequestParams(PROVIDER, _frame(), client_request_id="x" * dfb.MAX_BUDGETED_ID_CHARS)
        for kwargs in ({"client_request_id": "x" * 257}, {"provider_id": "p" * 257}):
            with self.subTest(field=next(iter(kwargs))), self.assertRaises(ValueError) as ctx:
                IngestDataRequestParams(**{"provider_id": PROVIDER, "frame": _frame(), **kwargs})
            self.assertIn("at most 256", str(ctx.exception))

    def test_frame_is_revalidated(self):
        frame = common_pb2.DataFrame()
        frame.dataTimestamps.CopyFrom(dfb.sampling_clock(1_770_000_000, 1_000, 1))
        frame.enumColumns.add(name="state").values[:] = [0]  # hand-built, no enumId
        with self.assertRaises(ValueError) as ctx:
            IngestDataRequestParams(PROVIDER, frame)
        self.assertIn("enumId", str(ctx.exception))


class TestChunkedRequestParams(unittest.TestCase):
    def test_numbers_the_chunks_under_one_base(self):
        chunks = dfb.split_data_frame(_frame(5), max_rows=2)
        params = list(chunked_request_params(PROVIDER, chunks, base_request_id="load-7"))
        self.assertEqual([p.client_request_id for p in params], ["load-7-0", "load-7-1", "load-7-2"])
        self.assertTrue(all(p.provider_id == PROVIDER for p in params))

    def test_default_base_is_generated_and_shared(self):
        params = list(chunked_request_params(PROVIDER, [_frame(), _frame()]))
        bases = {p.client_request_id.rsplit("-", 1)[0] for p in params}
        self.assertEqual(len(bases), 1)

    def test_is_lazy(self):
        def frames():
            yield _frame()
            raise AssertionError("consumed too far")

        self.assertEqual(next(chunked_request_params(PROVIDER, frames())).client_request_id[-2:], "-0")


class TestIngestData(_ClientTestCase):
    def test_builds_the_request(self):
        params = _params("r-1")
        request = self.client._build_ingest_data_request(params)
        self.assertEqual((request.providerId, request.clientRequestId), (PROVIDER, "r-1"))
        self.assertEqual(request.ingestionDataFrame, params.frame)

    def test_ack(self):
        self.stub.ingestData.return_value = _ack("r-1")
        result = self.client.ingest_data(_params("r-1"))
        self.assertFalse(result.result_status.is_error)
        self.assertEqual((result.provider_id, result.client_request_id), (PROVIDER, "r-1"))
        self.assertEqual((result.num_rows, result.num_columns), (2, 1))
        sent = self.stub.ingestData.call_args[0][0]
        self.assertEqual(sent.clientRequestId, "r-1")

    def test_reject_still_names_the_request(self):
        self.stub.ingestData.return_value = _reject("r-1", "bad frame")
        result = self.client.ingest_data(_params("r-1"))
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "bad frame")
        self.assertEqual((result.provider_id, result.client_request_id), (PROVIDER, "r-1"))
        self.assertIsNone(result.num_rows)

    def test_unrecognized_response_is_an_error(self):
        self.stub.ingestData.return_value = ingestion_pb2.IngestDataResponse(clientRequestId="r-1")
        result = self.client.ingest_data(_params("r-1"))
        self.assertIn("neither exceptionalResult nor ackResult", result.result_status.message)

    def test_grpc_error(self):
        self.stub.ingestData.side_effect = _FakeRpcError(grpc.StatusCode.UNAVAILABLE, "connection refused")
        result = self.client.ingest_data(_params())
        self.assertEqual(result.result_status.message, "gRPC error: connection refused")

    def test_unexpected_exception(self):
        self.stub.ingestData.side_effect = RuntimeError("kaboom")
        result = self.client.ingest_data(_params())
        self.assertIn("Unexpected error: kaboom", result.result_status.message)

    def test_oversized_request_points_at_split_data_frame(self):
        self.stub.ingestData.side_effect = _FakeRpcError(
            grpc.StatusCode.RESOURCE_EXHAUSTED, "gRPC message exceeds maximum size 4096000: 5000000"
        )
        message = self.client.ingest_data(_params()).result_status.message
        self.assertTrue(message.startswith("gRPC error: gRPC message exceeds maximum size"))
        self.assertIn("split_data_frame", message)

    def test_other_resource_exhaustion_gets_no_hint(self):
        for code, details in (
            (grpc.StatusCode.RESOURCE_EXHAUSTED, "quota exceeded"),
            (grpc.StatusCode.UNKNOWN, "gRPC message exceeds maximum size"),
        ):
            with self.subTest(code=code):
                self.stub.ingestData.side_effect = _FakeRpcError(code, details)
                self.assertEqual(self.client.ingest_data(_params()).result_status.message, f"gRPC error: {details}")


def _consume_then(response):
    """A stub side effect that drains the request iterator, as grpcio does, before returning response."""
    sent = []

    def call(requests):
        sent.extend(requests)
        return response

    return call, sent


class TestIngestDataStream(_ClientTestCase):
    def test_all_accepted(self):
        response = ingestion_pb2.IngestDataStreamResponse(clientRequestIds=["a", "b"])
        response.ingestDataStreamResult.numRequests = 2
        self.stub.ingestDataStream.side_effect, sent = _consume_then(response)
        result = self.client.ingest_data_stream([_params("a"), _params("b")])
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.num_requests, 2)
        self.assertEqual([r.clientRequestId for r in sent], ["a", "b"])

    def test_partial_reject_keeps_the_response(self):
        response = ingestion_pb2.IngestDataStreamResponse(clientRequestIds=["a", "b"], rejectedRequestIds=["b"])
        response.exceptionalResult.message = "one or more requests were rejected"
        self.stub.ingestDataStream.side_effect, _ = _consume_then(response)
        result = self.client.ingest_data_stream([_params("a"), _params("b")])
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.rejected_request_ids, ["b"])
        self.assertEqual(result.client_request_ids, ["a", "b"])
        self.assertIsNone(result.num_requests)

    def test_grpc_error_has_no_response(self):
        self.stub.ingestDataStream.side_effect = _FakeRpcError(grpc.StatusCode.UNAVAILABLE, "down")
        result = self.client.ingest_data_stream([_params()])
        self.assertEqual(result.result_status.message, "gRPC error: down")
        self.assertIsNone(result.client_request_ids)
        self.assertIsNone(result.rejected_request_ids)
        self.assertIsNone(result.num_requests)

    def test_oversized_request_points_at_split_data_frame(self):
        self.stub.ingestDataStream.side_effect = _FakeRpcError(
            grpc.StatusCode.RESOURCE_EXHAUSTED, "gRPC message exceeds maximum size 4096000: 9"
        )
        self.assertIn("split_data_frame", self.client.ingest_data_stream([_params()]).result_status.message)

    def test_unrecognized_response_is_an_error(self):
        self.stub.ingestDataStream.side_effect, _ = _consume_then(ingestion_pb2.IngestDataStreamResponse())
        result = self.client.ingest_data_stream([_params()])
        self.assertIn("neither exceptionalResult nor ingestDataStreamResult", result.result_status.message)


class TestIngestDataBidiStream(_ClientTestCase):
    def test_yields_rejects_without_raising(self):
        self.stub.ingestDataBidiStream.return_value = iter([_ack("a"), _reject("b", "nope"), _ack("c")])
        results = list(self.client.iter_ingest_data_bidi_stream([_params("a"), _params("b"), _params("c")]))
        self.assertEqual([r.client_request_id for r in results], ["a", "b", "c"])
        self.assertEqual([r.result_status.is_error for r in results], [False, True, False])
        self.assertEqual(results[1].result_status.message, "nope")

    def test_unrecognized_response_is_yielded_as_that_requests_error(self):
        self.stub.ingestDataBidiStream.return_value = iter([ingestion_pb2.IngestDataResponse(clientRequestId="a")])
        (result,) = list(self.client.iter_ingest_data_bidi_stream([_params("a")]))
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.client_request_id, "a")

    def test_transport_error_mid_stream_raises_after_earlier_results(self):
        def responses():
            yield _ack("a")
            raise _FakeRpcError(grpc.StatusCode.UNAVAILABLE, "stream reset")

        self.stub.ingestDataBidiStream.return_value = responses()
        seen = []
        with self.assertRaises(RuntimeError) as ctx:
            for result in self.client.iter_ingest_data_bidi_stream([_params("a"), _params("b")]):
                seen.append(result.client_request_id)
        self.assertEqual(seen, ["a"])
        self.assertIn("gRPC error: stream reset", str(ctx.exception))


class _CancellableResponses:
    """A response iterator with the cancel() a grpcio stream-stream call has."""

    def __init__(self, responses):
        self._responses = iter(responses)
        self.cancel = Mock()

    def __iter__(self):
        return self._responses


class TestIngestDataBidiStreamCancellation(_ClientTestCase):
    def test_stopping_early_cancels_the_call(self):
        # Otherwise grpcio keeps sending requests from its own thread after the caller has walked away.
        responses = _CancellableResponses([_ack("a"), _ack("b")])
        self.stub.ingestDataBidiStream.return_value = responses
        results = self.client.iter_ingest_data_bidi_stream([_params("a"), _params("b")])
        next(results)
        results.close()
        responses.cancel.assert_called_once()

    def test_a_completed_call_is_cancelled_harmlessly(self):
        responses = _CancellableResponses([_ack("a")])
        self.stub.ingestDataBidiStream.return_value = responses
        list(self.client.iter_ingest_data_bidi_stream([_params("a")]))
        responses.cancel.assert_called_once()


class TestRequestStatusQuery(unittest.TestCase):
    def test_enum_mirrors_the_proto(self):
        self.assertEqual(
            {member.name: member.value for member in IngestionRequestStatus},
            {
                name.replace("INGESTION_REQUEST_STATUS_", ""): value
                for name, value in ingestion_pb2.IngestionRequestStatus.items()
            },
        )

    def test_id_and_name_criteria(self):
        self.assertEqual(RS.provider_id("p").providerIdCriterion.providerId, "p")
        self.assertEqual(RS.provider_name("n").providerNameCriterion.providerName, "n")
        self.assertEqual(RS.request_id("r").requestIdCriterion.requestId, "r")

    def test_blank_inputs_are_rejected(self):
        for helper in (RS.provider_id, RS.provider_name, RS.request_id):
            with self.subTest(helper=helper.__name__), self.assertRaises(ValueError):
                helper("  ")

    def test_status_accepts_members_and_ints(self):
        criterion = RS.status([IngestionRequestStatus.ERROR, 1])
        self.assertEqual(list(criterion.statusCriterion.status), [2, 1])

    def test_status_rejects_empty_and_unknown(self):
        with self.assertRaises(ValueError):
            RS.status([])
        with self.assertRaises(ValueError) as ctx:
            RS.status([7])
        self.assertIn("unknown status", str(ctx.exception))

    def test_time_range_with_and_without_end(self):
        open_ended = RS.time_range(SINCE)
        self.assertTrue(open_ended.timeRangeCriterion.HasField("beginTime"))
        self.assertFalse(open_ended.timeRangeCriterion.HasField("endTime"))
        closed = RS.time_range(SINCE, SINCE)  # inclusive at both ends, so equal bounds are a valid instant
        self.assertTrue(closed.timeRangeCriterion.HasField("endTime"))

    def test_time_range_rejections(self):
        with self.assertRaises(ValueError):
            RS.time_range(0)  # the server requires begin >= 1 s
        with self.assertRaises(ValueError):
            RS.time_range(SINCE, datetime(2026, 1, 1, tzinfo=timezone.utc))


class TestQueryRequestStatus(_ClientTestCase):
    def test_requires_a_criterion(self):
        with self.assertRaises(ValueError):
            self.client.query_request_status([])

    def test_returns_the_documents(self):
        self.stub.queryRequestStatus.return_value = _status_response(_document("a"), _document("b"))
        result = self.client.query_request_status([RS.provider_id(PROVIDER)])
        self.assertEqual([d.requestId for d in result.request_statuses], ["a", "b"])
        sent = self.stub.queryRequestStatus.call_args[0][0]
        self.assertEqual(sent.criteria[0].providerIdCriterion.providerId, PROVIDER)

    def test_error_has_no_documents(self):
        response = ingestion_pb2.QueryRequestStatusResponse()
        response.exceptionalResult.message = "criteria list must not be empty"
        self.stub.queryRequestStatus.return_value = response
        result = self.client.query_request_status([RS.provider_id(PROVIDER)])
        self.assertTrue(result.result_status.is_error)
        self.assertIsNone(result.request_statuses)

    def test_receive_limit_points_at_a_narrower_query(self):
        self.stub.queryRequestStatus.side_effect = _FakeRpcError(
            grpc.StatusCode.RESOURCE_EXHAUSTED, "Received message larger than max (5000000 vs. 4194304)"
        )
        message = self.client.query_request_status([RS.provider_id(PROVIDER)]).result_status.message
        self.assertTrue(message.startswith("gRPC error: Received message larger than max"))
        self.assertIn("narrow the query", message)


class _FakeClock:
    """Stands in for time.monotonic / time.sleep, so polling tests take no real time."""

    def __init__(self):
        self.now = 1_000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class TestAwaitRequestStatuses(_ClientTestCase):
    def setUp(self):
        super().setUp()
        self.clock = _FakeClock()
        patcher = patch.multiple(
            "dp_python_lib.client.ingestion_client.time", monotonic=self.clock.monotonic, sleep=self.clock.sleep
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _await(self, ids, **kwargs):
        return self.client.await_request_statuses(PROVIDER, ids, since=SINCE, **kwargs)

    def test_returns_each_ids_documents_after_one_poll(self):
        self.stub.queryRequestStatus.return_value = _status_response(
            _document("a"), _document("b", IngestionRequestStatus.ERROR), _document("unrelated")
        )
        statuses = self._await(["a", "b"])
        self.assertEqual(list(statuses), ["a", "b"])
        self.assertEqual(statuses["b"][0].ingestionRequestStatus, IngestionRequestStatus.ERROR)
        self.assertEqual(self.clock.sleeps, [])

    def test_a_repeated_id_returns_every_document(self):
        self.stub.queryRequestStatus.return_value = _status_response(_document("a"), _document("a"))
        self.assertEqual(len(self._await(["a"])["a"]), 2)

    def test_one_query_per_poll_whatever_the_id_count(self):
        ids = [f"chunk-{i}" for i in range(500)]
        self.stub.queryRequestStatus.return_value = _status_response(*(_document(i) for i in ids))
        self._await(ids)
        self.assertEqual(self.stub.queryRequestStatus.call_count, 1)

    def test_query_is_provider_and_a_skew_adjusted_floor(self):
        self.stub.queryRequestStatus.return_value = _status_response(_document("a"))
        self._await(["a"])
        criteria = self.stub.queryRequestStatus.call_args[0][0].criteria
        self.assertEqual(criteria[0].providerIdCriterion.providerId, PROVIDER)
        time_range = criteria[1].timeRangeCriterion
        expected_floor = (int(SINCE.timestamp()) - int(REQUEST_STATUS_CLOCK_SKEW.total_seconds())) * 1_000_000_000
        self.assertEqual(to_epoch_nanos(time_range.beginTime), expected_floor)
        # No end: the server's "now", rather than this machine's clock.
        self.assertFalse(time_range.HasField("endTime"))
        # No request-id criterion: ids are matched here, since the API takes one id per criterion.
        self.assertFalse(any(c.HasField("requestIdCriterion") for c in criteria))

    def test_floor_is_clamped_to_what_the_server_accepts(self):
        self.stub.queryRequestStatus.return_value = _status_response(_document("a"))
        self.client.await_request_statuses(PROVIDER, ["a"], since=10)
        begin = self.stub.queryRequestStatus.call_args[0][0].criteria[1].timeRangeCriterion.beginTime
        self.assertEqual((begin.epochSeconds, begin.nanoseconds), (1, 0))

    def test_polls_until_every_id_appears(self):
        self.stub.queryRequestStatus.side_effect = [
            _status_response(),
            _status_response(_document("a")),
            _status_response(_document("a"), _document("b")),
        ]
        statuses = self._await(["a", "b"], poll_interval=0.5)
        self.assertEqual(set(statuses), {"a", "b"})
        self.assertEqual(self.clock.sleeps, [0.5, 0.5])

    def test_timeout_names_the_missing_ids(self):
        self.stub.queryRequestStatus.return_value = _status_response(_document("a"))
        with self.assertRaises(TimeoutError) as ctx:
            self._await(["a", "b", "c"], timeout=1.0, poll_interval=0.4)
        message = str(ctx.exception)
        self.assertIn("2 of 3", message)
        self.assertIn("b, c", message)
        self.assertNotIn("a,", message)
        # The last sleep is trimmed to the deadline, then one final poll runs.
        self.assertAlmostEqual(sum(self.clock.sleeps), 1.0)

    def test_a_failed_query_raises(self):
        self.stub.queryRequestStatus.side_effect = _FakeRpcError(grpc.StatusCode.UNAVAILABLE, "down")
        with self.assertRaises(RuntimeError) as ctx:
            self._await(["a"])
        self.assertIn("gRPC error: down", str(ctx.exception))

    def test_duplicate_input_ids_are_waited_for_once(self):
        self.stub.queryRequestStatus.return_value = _status_response(_document("a"))
        self.assertEqual(list(self._await(["a", "a"])), ["a"])

    def test_argument_validation(self):
        for label, call in (
            ("blank provider", lambda: self.client.await_request_statuses(" ", ["a"], since=SINCE)),
            ("no ids", lambda: self._await([])),
            ("blank id", lambda: self._await(["a", ""])),
            ("zero timeout", lambda: self._await(["a"], timeout=0)),
            ("zero interval", lambda: self._await(["a"], poll_interval=0)),
        ):
            with self.subTest(case=label), self.assertRaises(ValueError):
                call()

    def test_since_is_required(self):
        with self.assertRaises(TypeError):
            self.client.await_request_statuses(PROVIDER, ["a"])  # type: ignore[call-arg]


if __name__ == "__main__":
    unittest.main()
