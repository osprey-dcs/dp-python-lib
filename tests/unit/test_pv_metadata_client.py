import os
import sys
import unittest
from unittest.mock import Mock

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client.pv_metadata_client import (
    DeletePvMetadataApiResult,
    GetPvMetadataApiResult,
    PvMetadataClient,
    PvMetadataQuery,
    QueryPvMetadataApiResult,
    SavePvMetadataApiResult,
    SavePvMetadataRequestParams,
)
from dp_python_lib.grpc import annotation_pb2, common_pb2


def _response_with_field(field_name):
    """
    Build a Mock response whose HasField(field) returns True only for field_name.  This keeps both the _send_*
    oneof check and the *ApiResult property accessors (which also call HasField) consistent.
    """
    response = Mock()
    response.HasField = Mock(side_effect=lambda field: field == field_name)
    return response


class TestPvMetadataClientBuildRequests(unittest.TestCase):
    """Unit tests for the request-building helpers (no gRPC calls)."""

    def setUp(self):
        self.mock_channel = Mock()
        self.client = PvMetadataClient(self.mock_channel)

    def test_build_save_request_all_fields(self):
        params = SavePvMetadataRequestParams(
            pv_name="ABC:1",
            aliases=["a1", "a2"],
            tags=["vacuum", "beam"],
            attributes={"unit": "V", "system": "vac"},
            modified_by="tester",
            description="a test PV",
        )
        request = self.client._build_save_pv_metadata_request(params)

        self.assertEqual(request.pvName, "ABC:1")
        self.assertEqual(list(request.aliases), ["a1", "a2"])
        self.assertEqual(list(request.tags), ["vacuum", "beam"])
        self.assertEqual(request.modifiedBy, "tester")
        self.assertEqual(request.description, "a test PV")
        self.assertEqual({(a.name, a.value) for a in request.attributes}, {("unit", "V"), ("system", "vac")})

    def test_build_save_request_name_only(self):
        params = SavePvMetadataRequestParams(pv_name="ABC:1")
        request = self.client._build_save_pv_metadata_request(params)

        self.assertEqual(request.pvName, "ABC:1")
        self.assertEqual(list(request.aliases), [])
        self.assertEqual(list(request.tags), [])
        self.assertEqual(len(request.attributes), 0)
        self.assertEqual(request.modifiedBy, "")
        self.assertEqual(request.description, "")

    def test_build_get_request(self):
        request = self.client._build_get_pv_metadata_request("ABC:1")
        self.assertEqual(request.pvNameOrAlias, "ABC:1")

    def test_build_delete_request(self):
        request = self.client._build_delete_pv_metadata_request("alias-1")
        self.assertEqual(request.pvNameOrAlias, "alias-1")

    def test_build_query_request(self):
        criteria = [
            PvMetadataQuery.pv_name(prefix=["ABC:"]),
            PvMetadataQuery.tags(["vacuum"]),
        ]
        request = self.client._build_query_pv_metadata_request(criteria, limit=25, page_token="tok")

        self.assertEqual(len(request.criteria), 2)
        self.assertTrue(request.criteria[0].HasField("pvNameCriterion"))
        self.assertEqual(list(request.criteria[0].pvNameCriterion.prefix), ["ABC:"])
        self.assertTrue(request.criteria[1].HasField("tagsCriterion"))
        self.assertEqual(list(request.criteria[1].tagsCriterion.values), ["vacuum"])
        self.assertEqual(request.limit, 25)
        self.assertEqual(request.pageToken, "tok")


class TestPvMetadataQueryHelpers(unittest.TestCase):
    """Unit tests for the PvMetadataQuery criterion builders."""

    def test_pv_name_all_options(self):
        c = PvMetadataQuery.pv_name(exact=["A"], prefix=["B"], contains=["C"])
        self.assertTrue(c.HasField("pvNameCriterion"))
        self.assertEqual(list(c.pvNameCriterion.exact), ["A"])
        self.assertEqual(list(c.pvNameCriterion.prefix), ["B"])
        self.assertEqual(list(c.pvNameCriterion.contains), ["C"])

    def test_aliases(self):
        c = PvMetadataQuery.aliases(exact=["a1"])
        self.assertTrue(c.HasField("aliasesCriterion"))
        self.assertEqual(list(c.aliasesCriterion.exact), ["a1"])

    def test_tags(self):
        c = PvMetadataQuery.tags(["t1", "t2"])
        self.assertTrue(c.HasField("tagsCriterion"))
        self.assertEqual(list(c.tagsCriterion.values), ["t1", "t2"])

    def test_attributes(self):
        c = PvMetadataQuery.attributes("unit", ["V", "A"])
        self.assertTrue(c.HasField("attributesCriterion"))
        self.assertEqual(c.attributesCriterion.key, "unit")
        self.assertEqual(list(c.attributesCriterion.values), ["V", "A"])

    def test_pv_name_empty_raises(self):
        with self.assertRaises(ValueError):
            PvMetadataQuery.pv_name()
        with self.assertRaises(ValueError):
            PvMetadataQuery.pv_name(exact=[], prefix=[], contains=[])

    def test_aliases_empty_raises(self):
        with self.assertRaises(ValueError):
            PvMetadataQuery.aliases()

    def test_tags_empty_raises(self):
        with self.assertRaises(ValueError):
            PvMetadataQuery.tags([])

    def test_attributes_empty_raises(self):
        with self.assertRaises(ValueError):
            PvMetadataQuery.attributes("", ["V"])
        with self.assertRaises(ValueError):
            PvMetadataQuery.attributes("unit", [])


class TestSendSavePvMetadata(unittest.TestCase):
    def setUp(self):
        self.mock_channel = Mock()
        self.client = PvMetadataClient(self.mock_channel)
        self.request = annotation_pb2.SavePvMetadataRequest(pvName="ABC:1")

    def test_success(self):
        response = _response_with_field("savePvMetadataResult")
        response.savePvMetadataResult.pvName = "ABC:1"
        mock_stub = Mock()
        mock_stub.savePvMetadata.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_save_pv_metadata(self.request)

        self.assertIsInstance(result, SavePvMetadataApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.response, response)
        self.assertEqual(result.pv_name, "ABC:1")
        mock_stub.savePvMetadata.assert_called_once_with(self.request)

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "PV already exists"
        mock_stub = Mock()
        mock_stub.savePvMetadata.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_save_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "PV already exists")
        self.assertIsNone(result.response)
        self.assertIsNone(result.pv_name)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.savePvMetadata.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_save_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)
        self.assertIsNone(result.response)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="Connection timeout")
        mock_stub.savePvMetadata.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_save_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: Connection timeout", result.result_status.message)
        self.assertIsNone(result.response)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.savePvMetadata.side_effect = ValueError("boom")
        self.client._stub = mock_stub

        result = self.client._send_save_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)
        self.assertIsNone(result.response)


class TestSendGetPvMetadata(unittest.TestCase):
    def setUp(self):
        self.mock_channel = Mock()
        self.client = PvMetadataClient(self.mock_channel)
        self.request = annotation_pb2.GetPvMetadataRequest(pvNameOrAlias="ABC:1")

    def test_success(self):
        response = _response_with_field("getPvMetadataResult")
        pv = common_pb2.PvMetadata(pvName="ABC:1")
        response.getPvMetadataResult.pvMetadata = pv
        mock_stub = Mock()
        mock_stub.getPvMetadata.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_get_pv_metadata(self.request)

        self.assertIsInstance(result, GetPvMetadataApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.pv_metadata.pvName, "ABC:1")
        mock_stub.getPvMetadata.assert_called_once_with(self.request)

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "not found"
        mock_stub = Mock()
        mock_stub.getPvMetadata.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_get_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "not found")
        self.assertIsNone(result.pv_metadata)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.getPvMetadata.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_get_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="unavailable")
        mock_stub.getPvMetadata.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_get_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: unavailable", result.result_status.message)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.getPvMetadata.side_effect = KeyError("bad")
        self.client._stub = mock_stub

        result = self.client._send_get_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error", result.result_status.message)


class TestSendDeletePvMetadata(unittest.TestCase):
    def setUp(self):
        self.mock_channel = Mock()
        self.client = PvMetadataClient(self.mock_channel)
        self.request = annotation_pb2.DeletePvMetadataRequest(pvNameOrAlias="ABC:1")

    def test_success(self):
        response = _response_with_field("deletePvMetadataResult")
        response.deletePvMetadataResult.pvName = "ABC:1"
        mock_stub = Mock()
        mock_stub.deletePvMetadata.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_delete_pv_metadata(self.request)

        self.assertIsInstance(result, DeletePvMetadataApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.pv_name, "ABC:1")
        mock_stub.deletePvMetadata.assert_called_once_with(self.request)

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "not found"
        mock_stub = Mock()
        mock_stub.deletePvMetadata.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_delete_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "not found")
        self.assertIsNone(result.pv_name)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.deletePvMetadata.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_delete_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="denied")
        mock_stub.deletePvMetadata.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_delete_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: denied", result.result_status.message)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.deletePvMetadata.side_effect = RuntimeError("oops")
        self.client._stub = mock_stub

        result = self.client._send_delete_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: oops", result.result_status.message)


class TestSendQueryPvMetadata(unittest.TestCase):
    def setUp(self):
        self.mock_channel = Mock()
        self.client = PvMetadataClient(self.mock_channel)
        self.request = annotation_pb2.QueryPvMetadataRequest()

    def _build_page(self, pv_names, next_token):
        response = _response_with_field("pvMetadataResult")
        result = annotation_pb2.QueryPvMetadataResponse.PvMetadataResult()
        for name in pv_names:
            result.pvMetadata.append(common_pb2.PvMetadata(pvName=name))
        result.nextPageToken = next_token
        response.pvMetadataResult = result
        return response

    def test_success(self):
        response = self._build_page(["ABC:1", "ABC:2"], "next-tok")
        mock_stub = Mock()
        mock_stub.queryPvMetadata.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_query_pv_metadata(self.request)

        self.assertIsInstance(result, QueryPvMetadataApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual([p.pvName for p in result.pv_metadata_list], ["ABC:1", "ABC:2"])
        self.assertEqual(result.next_page_token, "next-tok")

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "bad query"
        mock_stub = Mock()
        mock_stub.queryPvMetadata.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_query_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "bad query")
        self.assertEqual(result.pv_metadata_list, [])
        self.assertEqual(result.next_page_token, "")

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.queryPvMetadata.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_query_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="timeout")
        mock_stub.queryPvMetadata.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_query_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: timeout", result.result_status.message)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.queryPvMetadata.side_effect = ValueError("nope")
        self.client._stub = mock_stub

        result = self.client._send_query_pv_metadata(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: nope", result.result_status.message)


class TestIterPvMetadata(unittest.TestCase):
    def setUp(self):
        self.mock_channel = Mock()
        self.client = PvMetadataClient(self.mock_channel)
        self.criteria = [PvMetadataQuery.pv_name(prefix=["ABC:"])]

    def _page(self, pv_names, next_token):
        response = _response_with_field("pvMetadataResult")
        result = annotation_pb2.QueryPvMetadataResponse.PvMetadataResult()
        for name in pv_names:
            result.pvMetadata.append(common_pb2.PvMetadata(pvName=name))
        result.nextPageToken = next_token
        response.pvMetadataResult = result
        return response

    def test_pages_through_all_results(self):
        page1 = self._page(["ABC:1", "ABC:2"], "tok1")
        page2 = self._page(["ABC:3"], "")  # empty next token = last page
        mock_stub = Mock()
        mock_stub.queryPvMetadata.side_effect = [page1, page2]
        self.client._stub = mock_stub

        names = [pv.pvName for pv in self.client.iter_pv_metadata(self.criteria, limit=2)]

        self.assertEqual(names, ["ABC:1", "ABC:2", "ABC:3"])
        # Two RPC calls were made (one per page)
        self.assertEqual(mock_stub.queryPvMetadata.call_count, 2)
        # Second call carried the page token from the first response
        second_request = mock_stub.queryPvMetadata.call_args_list[1].args[0]
        self.assertEqual(second_request.pageToken, "tok1")

    def test_single_page(self):
        mock_stub = Mock()
        mock_stub.queryPvMetadata.return_value = self._page(["ABC:1"], "")
        self.client._stub = mock_stub

        names = [pv.pvName for pv in self.client.iter_pv_metadata(self.criteria)]

        self.assertEqual(names, ["ABC:1"])
        self.assertEqual(mock_stub.queryPvMetadata.call_count, 1)

    def test_raises_on_error_page(self):
        error_response = _response_with_field("exceptionalResult")
        error_response.exceptionalResult.message = "query failed"
        mock_stub = Mock()
        mock_stub.queryPvMetadata.return_value = error_response
        self.client._stub = mock_stub

        with self.assertRaises(RuntimeError) as ctx:
            list(self.client.iter_pv_metadata(self.criteria))
        self.assertIn("query failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
