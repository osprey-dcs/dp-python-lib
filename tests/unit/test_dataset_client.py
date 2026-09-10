import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from assignment_spy import watch_assignments

from dp_python_lib.client.dataset_client import (
    DataSetClient,
    DataSetQuery,
    DeleteDataSetApiResult,
    GetDataSetApiResult,
    QueryDataSetsApiResult,
    SaveDataSetApiResult,
    SaveDataSetRequestParams,
    data_block,
)
from dp_python_lib.grpc import annotation_pb2

BEGIN = datetime(2026, 7, 14, 18, tzinfo=timezone.utc)
END = datetime(2026, 7, 14, 19, tzinfo=timezone.utc)


def _response_with_field(field_name):
    """
    Build a Mock response whose HasField(field) returns True only for field_name.  This keeps both the _send_*
    oneof check and the *ApiResult property accessors (which also call HasField) consistent.
    """
    response = Mock()
    response.HasField = Mock(side_effect=lambda field: field == field_name)
    return response


class TestDataBlockBuilder(unittest.TestCase):
    """Unit tests for the data_block() builder."""

    def test_builds_block(self):
        block = data_block(BEGIN, END, ["A:1", "A:2"])
        self.assertEqual(block.beginTime.epochSeconds, int(BEGIN.timestamp()))
        self.assertEqual(block.endTime.epochSeconds, int(END.timestamp()))
        self.assertEqual(list(block.pvNames), ["A:1", "A:2"])

    def test_accepts_epoch_seconds_and_timestamps(self):
        block = data_block(100, 200, ["A:1"])
        self.assertEqual(block.beginTime.epochSeconds, 100)
        self.assertEqual(block.endTime.epochSeconds, 200)

    def test_rejects_empty_pv_names(self):
        with self.assertRaises(ValueError) as ctx:
            data_block(BEGIN, END, [])
        self.assertIn("pv_names", str(ctx.exception))

    def test_rejects_reversed_range(self):
        # The server validates only that each bound is non-zero, so this check is the only one there is.
        with self.assertRaises(ValueError) as ctx:
            data_block(END, BEGIN, ["A:1"])
        self.assertIn("strictly before", str(ctx.exception))

    def test_rejects_equal_bounds(self):
        with self.assertRaises(ValueError):
            data_block(BEGIN, BEGIN, ["A:1"])

    def test_rejects_reversed_range_at_nanosecond_precision(self):
        # Equal seconds, decreasing nanoseconds -- caught only if the comparison includes the nanos field.
        with self.assertRaises(ValueError):
            data_block(100.000_002, 100.000_001, ["A:1"])

    def test_rejects_naive_datetime(self):
        with self.assertRaises(ValueError):
            data_block(datetime(2026, 7, 14), END, ["A:1"])  # noqa: DTZ001 -- naive input is the condition under test

    def test_rejects_bare_string_pv_names(self):
        # Without the guard this assigns one PV name per character, saving a silently wrong DataSet.
        with self.assertRaises(ValueError) as ctx:
            data_block(BEGIN, END, "A:1")
        self.assertIn("bare string", str(ctx.exception))


class TestDataSetClientBuildRequests(unittest.TestCase):
    """Unit tests for the request-building helpers (no gRPC calls)."""

    def setUp(self):
        self.mock_channel = Mock()
        self.client = DataSetClient(self.mock_channel)
        self.block = data_block(BEGIN, END, ["A:1"])

    def test_build_save_request_all_fields(self):
        params = SaveDataSetRequestParams(
            name="ramp study",
            owner_id="cmcchesney",
            data_blocks=[self.block],
            description="a test dataset",
            tags=["ramp-study", "reviewed"],
            attributes={"runNumber": "4471", "station": "gunb"},
            modified_by="tester",
            dataset_id="ds-1",
        )
        request = self.client._build_save_dataset_request(params)

        self.assertEqual(request.id, "ds-1")
        self.assertEqual(request.name, "ramp study")
        self.assertEqual(request.ownerId, "cmcchesney")
        self.assertEqual(request.description, "a test dataset")
        self.assertEqual(list(request.tags), ["ramp-study", "reviewed"])
        self.assertEqual(request.modifiedBy, "tester")
        self.assertEqual(len(request.dataBlocks), 1)
        self.assertEqual(list(request.dataBlocks[0].pvNames), ["A:1"])
        self.assertEqual(
            {(a.name, a.value) for a in request.attributes},
            {("runNumber", "4471"), ("station", "gunb")},
        )

    def test_build_save_request_required_fields_only(self):
        params = SaveDataSetRequestParams(name="n", owner_id="o", data_blocks=[self.block])
        request = self.client._build_save_dataset_request(params)

        self.assertEqual(request.name, "n")
        self.assertEqual(request.ownerId, "o")
        self.assertEqual(request.id, "")
        self.assertEqual(request.description, "")
        self.assertEqual(list(request.tags), [])
        self.assertEqual(len(request.attributes), 0)
        self.assertEqual(request.modifiedBy, "")

    def test_build_save_request_omitted_id_is_not_assigned(self):
        # An omitted dataset_id must leave 'id' unassigned, since a present id means "replace in full".
        params = SaveDataSetRequestParams(name="n", owner_id="o", data_blocks=[self.block])
        with watch_assignments(annotation_pb2, "SaveDataSetRequest", "id") as assigned:
            self.client._build_save_dataset_request(params)
        self.assertEqual(assigned, [], "an omitted dataset_id must not be assigned")

    def test_build_get_request(self):
        request = self.client._build_get_dataset_request("ds-1")
        self.assertEqual(request.dataSetId, "ds-1")

    def test_build_delete_request(self):
        request = self.client._build_delete_dataset_request("ds-1")
        self.assertEqual(request.dataSetId, "ds-1")

    def test_build_query_request(self):
        criteria = [DataSetQuery.owners(["cmcchesney"]), DataSetQuery.tags(["ramp-study"])]
        request = self.client._build_query_datasets_request(criteria, limit=25, page_token="tok")

        self.assertEqual(len(request.criteria), 2)
        self.assertTrue(request.criteria[0].HasField("ownerCriterion"))
        self.assertEqual(list(request.criteria[0].ownerCriterion.ownerIds), ["cmcchesney"])
        self.assertTrue(request.criteria[1].HasField("tagsCriterion"))
        self.assertEqual(request.limit, 25)
        self.assertEqual(request.pageToken, "tok")

    def test_build_query_request_no_criteria(self):
        # An empty criteria list is a legal match-all, not an error.
        request = self.client._build_query_datasets_request()
        self.assertEqual(len(request.criteria), 0)

    def test_build_query_request_limit_zero_is_set(self):
        # limit=0 must be forwarded (distinct from "not provided"); guard against a truthiness regression (#13).
        # A proto3 scalar reads 0 whether or not it was assigned, so watch the assignment itself.
        with watch_assignments(annotation_pb2, "QueryDataSetsRequest", "limit") as assigned:
            self.client._build_query_datasets_request([DataSetQuery.tags(["x"])], limit=0)
        self.assertEqual(assigned, [0], "limit=0 must be assigned, not dropped by a truthiness guard")

    def test_build_query_request_limit_omitted_is_unset(self):
        with watch_assignments(annotation_pb2, "QueryDataSetsRequest", "limit") as assigned:
            self.client._build_query_datasets_request([DataSetQuery.tags(["x"])])
        self.assertEqual(assigned, [], "an omitted limit must not be assigned")

    def test_build_query_request_empty_page_token_is_unset(self):
        with watch_assignments(annotation_pb2, "QueryDataSetsRequest", "pageToken") as assigned:
            self.client._build_query_datasets_request([DataSetQuery.tags(["x"])], page_token="")
        self.assertEqual(assigned, [], "an empty page token must not be assigned")


class TestSaveDataSetRequestParamsValidation(unittest.TestCase):
    """
    Unit tests for the params validation.  The server requires all three, so catching them here turns a round trip
    into an immediate error naming the field.
    """

    def setUp(self):
        self.block = data_block(BEGIN, END, ["A:1"])

    def test_rejects_empty_name(self):
        with self.assertRaises(ValueError) as ctx:
            SaveDataSetRequestParams(name="", owner_id="cmcchesney", data_blocks=[self.block])
        self.assertIn("name", str(ctx.exception))

    def test_rejects_empty_owner_id(self):
        with self.assertRaises(ValueError) as ctx:
            SaveDataSetRequestParams(name="ramp study", owner_id="", data_blocks=[self.block])
        self.assertIn("owner_id", str(ctx.exception))

    def test_rejects_empty_data_blocks(self):
        with self.assertRaises(ValueError) as ctx:
            SaveDataSetRequestParams(name="ramp study", owner_id="cmcchesney", data_blocks=[])
        self.assertIn("data_blocks", str(ctx.exception))

    def test_accepts_required_fields(self):
        params = SaveDataSetRequestParams(name="ramp study", owner_id="cmcchesney", data_blocks=[self.block])
        self.assertEqual(params.name, "ramp study")


class TestDataSetQueryHelpers(unittest.TestCase):
    """Unit tests for the DataSetQuery criterion builders."""

    def test_ids(self):
        c = DataSetQuery.ids(["a", "b"])
        self.assertTrue(c.HasField("idCriterion"))
        self.assertEqual(list(c.idCriterion.ids), ["a", "b"])

    def test_owners(self):
        c = DataSetQuery.owners(["o1"])
        self.assertTrue(c.HasField("ownerCriterion"))
        self.assertEqual(list(c.ownerCriterion.ownerIds), ["o1"])

    def test_name_all_options(self):
        c = DataSetQuery.name(exact=["A"], prefix=["B"], contains=["C"])
        self.assertTrue(c.HasField("nameCriterion"))
        self.assertEqual(list(c.nameCriterion.exact), ["A"])
        self.assertEqual(list(c.nameCriterion.prefix), ["B"])
        self.assertEqual(list(c.nameCriterion.contains), ["C"])

    def test_text(self):
        c = DataSetQuery.text("ramp")
        self.assertTrue(c.HasField("textCriterion"))
        self.assertEqual(c.textCriterion.text, "ramp")

    def test_pv_names(self):
        c = DataSetQuery.pv_names(["A:1"])
        self.assertTrue(c.HasField("pvNameCriterion"))
        self.assertEqual(list(c.pvNameCriterion.names), ["A:1"])

    def test_tags(self):
        c = DataSetQuery.tags(["t1", "t2"])
        self.assertTrue(c.HasField("tagsCriterion"))
        self.assertEqual(list(c.tagsCriterion.values), ["t1", "t2"])

    def test_attributes_with_values(self):
        c = DataSetQuery.attributes("runNumber", ["4471"])
        self.assertTrue(c.HasField("attributesCriterion"))
        self.assertEqual(c.attributesCriterion.key, "runNumber")
        self.assertEqual(list(c.attributesCriterion.values), ["4471"])

    def test_attributes_key_only(self):
        # Unlike the older helpers, an absent/empty values list is legal: it is a key-only existence search.
        for criterion in (DataSetQuery.attributes("runNumber"), DataSetQuery.attributes("runNumber", [])):
            self.assertTrue(criterion.HasField("attributesCriterion"))
            self.assertEqual(criterion.attributesCriterion.key, "runNumber")
            self.assertEqual(list(criterion.attributesCriterion.values), [])

    def test_empty_inputs_raise(self):
        with self.assertRaises(ValueError):
            DataSetQuery.ids([])
        with self.assertRaises(ValueError):
            DataSetQuery.owners([])
        with self.assertRaises(ValueError):
            DataSetQuery.name()
        with self.assertRaises(ValueError):
            DataSetQuery.text("")
        with self.assertRaises(ValueError):
            DataSetQuery.pv_names([])
        with self.assertRaises(ValueError):
            DataSetQuery.tags([])
        with self.assertRaises(ValueError):
            DataSetQuery.attributes("")


class TestQueryDataSetsTextCriterionRule(unittest.TestCase):
    """Two text criteria cannot be ANDed, so the client rejects them before the RPC."""

    def setUp(self):
        self.client = DataSetClient(Mock())
        self.client._stub = Mock()

    def test_two_text_criteria_raise(self):
        with self.assertRaises(ValueError) as ctx:
            self.client.query_datasets([DataSetQuery.text("a"), DataSetQuery.text("b")])
        self.assertIn("at most one text criterion", str(ctx.exception))
        self.client._stub.queryDataSets.assert_not_called()

    def test_one_text_criterion_is_allowed(self):
        response = _response_with_field("dataSetsResult")
        response.dataSetsResult = annotation_pb2.QueryDataSetsResponse.DataSetsResult()
        self.client._stub.queryDataSets.return_value = response

        result = self.client.query_datasets([DataSetQuery.text("a"), DataSetQuery.tags(["t"])])

        self.assertFalse(result.result_status.is_error)

    def test_iter_datasets_also_rejects_two_text_criteria(self):
        with self.assertRaises(ValueError):
            list(self.client.iter_datasets([DataSetQuery.text("a"), DataSetQuery.text("b")]))


class TestSendSaveDataSet(unittest.TestCase):
    def setUp(self):
        self.client = DataSetClient(Mock())
        self.request = annotation_pb2.SaveDataSetRequest(name="n", ownerId="o")

    def test_success(self):
        response = _response_with_field("saveDataSetResult")
        response.saveDataSetResult.dataSetId = "ds-1"
        mock_stub = Mock()
        mock_stub.saveDataSet.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_save_dataset(self.request)

        self.assertIsInstance(result, SaveDataSetApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.dataset_id, "ds-1")
        mock_stub.saveDataSet.assert_called_once_with(self.request)

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "name must be specified"
        mock_stub = Mock()
        mock_stub.saveDataSet.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_save_dataset(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "name must be specified")
        self.assertIsNone(result.dataset_id)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.saveDataSet.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_save_dataset(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="Connection timeout")
        mock_stub.saveDataSet.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_save_dataset(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: Connection timeout", result.result_status.message)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.saveDataSet.side_effect = ValueError("boom")
        self.client._stub = mock_stub

        result = self.client._send_save_dataset(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestSendGetDataSet(unittest.TestCase):
    def setUp(self):
        self.client = DataSetClient(Mock())
        self.request = annotation_pb2.GetDataSetRequest(dataSetId="ds-1")

    def test_success(self):
        response = _response_with_field("getDataSetResult")
        response.getDataSetResult.dataSet = annotation_pb2.DataSet(id="ds-1", name="ramp study")
        mock_stub = Mock()
        mock_stub.getDataSet.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_get_dataset(self.request)

        self.assertIsInstance(result, GetDataSetApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.dataset.name, "ramp study")
        mock_stub.getDataSet.assert_called_once_with(self.request)

    def test_not_found_is_a_business_error(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "no DataSet record found for id: ds-1"
        mock_stub = Mock()
        mock_stub.getDataSet.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_get_dataset(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("no DataSet record found", result.result_status.message)
        self.assertIsNone(result.dataset)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.getDataSet.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_get_dataset(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="unavailable")
        mock_stub.getDataSet.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_get_dataset(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: unavailable", result.result_status.message)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.getDataSet.side_effect = KeyError("bad")
        self.client._stub = mock_stub

        result = self.client._send_get_dataset(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error", result.result_status.message)


class TestSendQueryDataSets(unittest.TestCase):
    def setUp(self):
        self.client = DataSetClient(Mock())
        self.request = annotation_pb2.QueryDataSetsRequest()

    def _result(self, names, next_token=""):
        result = annotation_pb2.QueryDataSetsResponse.DataSetsResult()
        for name in names:
            result.dataSets.append(annotation_pb2.DataSet(name=name))
        result.nextPageToken = next_token
        return result

    def test_success(self):
        response = _response_with_field("dataSetsResult")
        response.dataSetsResult = self._result(["a", "b"], "tok")
        mock_stub = Mock()
        mock_stub.queryDataSets.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_query_datasets(self.request)

        self.assertIsInstance(result, QueryDataSetsApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual([d.name for d in result.datasets], ["a", "b"])
        self.assertEqual(result.next_page_token, "tok")

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "invalid page token"
        mock_stub = Mock()
        mock_stub.queryDataSets.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_query_datasets(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.datasets, [])
        self.assertEqual(result.next_page_token, "")

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.queryDataSets.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_query_datasets(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="unavailable")
        mock_stub.queryDataSets.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_query_datasets(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: unavailable", result.result_status.message)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.queryDataSets.side_effect = ValueError("boom")
        self.client._stub = mock_stub

        result = self.client._send_query_datasets(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestSendDeleteDataSet(unittest.TestCase):
    def setUp(self):
        self.client = DataSetClient(Mock())
        self.request = annotation_pb2.DeleteDataSetRequest(dataSetId="ds-1")

    def test_success(self):
        response = _response_with_field("deleteDataSetResult")
        response.deleteDataSetResult.dataSetId = "ds-1"
        mock_stub = Mock()
        mock_stub.deleteDataSet.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_delete_dataset(self.request)

        self.assertIsInstance(result, DeleteDataSetApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.dataset_id, "ds-1")
        mock_stub.deleteDataSet.assert_called_once_with(self.request)

    def test_referenced_dataset_is_a_business_error(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "DataSet ds-1 is referenced by annotation an-1 (3 total)"
        mock_stub = Mock()
        mock_stub.deleteDataSet.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_delete_dataset(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("referenced by annotation", result.result_status.message)
        self.assertIsNone(result.dataset_id)

    def test_not_found_is_a_business_error(self):
        # Deleting a DataSet that does not exist is a REJECT, not a silent success.
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "no DataSet record found for id: ds-1"
        mock_stub = Mock()
        mock_stub.deleteDataSet.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_delete_dataset(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIsNone(result.dataset_id)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.deleteDataSet.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_delete_dataset(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="unavailable")
        mock_stub.deleteDataSet.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_delete_dataset(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: unavailable", result.result_status.message)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.deleteDataSet.side_effect = ValueError("boom")
        self.client._stub = mock_stub

        result = self.client._send_delete_dataset(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestIterDataSets(unittest.TestCase):
    def setUp(self):
        self.client = DataSetClient(Mock())
        self.criteria = [DataSetQuery.tags(["ramp-study"])]

    def _page(self, names, next_token):
        response = _response_with_field("dataSetsResult")
        result = annotation_pb2.QueryDataSetsResponse.DataSetsResult()
        for name in names:
            result.dataSets.append(annotation_pb2.DataSet(name=name))
        result.nextPageToken = next_token
        response.dataSetsResult = result
        return response

    def test_pages_through_all_results(self):
        mock_stub = Mock()
        mock_stub.queryDataSets.side_effect = [self._page(["a", "b"], "tok1"), self._page(["c"], "")]
        self.client._stub = mock_stub

        names = [d.name for d in self.client.iter_datasets(self.criteria, limit=2)]

        self.assertEqual(names, ["a", "b", "c"])
        self.assertEqual(mock_stub.queryDataSets.call_count, 2)
        second_request = mock_stub.queryDataSets.call_args_list[1].args[0]
        self.assertEqual(second_request.pageToken, "tok1")

    def test_single_page(self):
        mock_stub = Mock()
        mock_stub.queryDataSets.return_value = self._page(["a"], "")
        self.client._stub = mock_stub

        self.assertEqual([d.name for d in self.client.iter_datasets(self.criteria)], ["a"])
        self.assertEqual(mock_stub.queryDataSets.call_count, 1)

    def test_no_criteria_matches_all(self):
        mock_stub = Mock()
        mock_stub.queryDataSets.return_value = self._page(["a"], "")
        self.client._stub = mock_stub

        self.assertEqual([d.name for d in self.client.iter_datasets()], ["a"])
        self.assertEqual(len(mock_stub.queryDataSets.call_args_list[0].args[0].criteria), 0)

    def test_page_error_raises_runtime_error(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "page token rejected"
        mock_stub = Mock()
        mock_stub.queryDataSets.return_value = response
        self.client._stub = mock_stub

        with self.assertRaises(RuntimeError) as ctx:
            list(self.client.iter_datasets(self.criteria))
        self.assertIn("page token rejected", str(ctx.exception))


class TestGetDataSetsBatch(unittest.TestCase):
    """Unit tests for the get_datasets() batch fetch (D9)."""

    def setUp(self):
        self.client = DataSetClient(Mock())

    def _page(self, ids, next_token=""):
        response = _response_with_field("dataSetsResult")
        result = annotation_pb2.QueryDataSetsResponse.DataSetsResult()
        for dataset_id in ids:
            result.dataSets.append(annotation_pb2.DataSet(id=dataset_id, name=f"name-{dataset_id}"))
        result.nextPageToken = next_token
        response.dataSetsResult = result
        return response

    def test_returns_dict_keyed_by_id(self):
        mock_stub = Mock()
        mock_stub.queryDataSets.return_value = self._page(["a", "b"])
        self.client._stub = mock_stub

        found = self.client.get_datasets(["a", "b"])

        self.assertEqual(set(found), {"a", "b"})
        self.assertEqual(found["a"].name, "name-a")

    def test_empty_ids_makes_no_rpc(self):
        mock_stub = Mock()
        self.client._stub = mock_stub

        self.assertEqual(self.client.get_datasets([]), {})
        mock_stub.queryDataSets.assert_not_called()

    def test_deduplicates_ids_preserving_order(self):
        mock_stub = Mock()
        mock_stub.queryDataSets.return_value = self._page(["a", "b"])
        self.client._stub = mock_stub

        self.client.get_datasets(["a", "b", "a", "b", "a"])

        request = mock_stub.queryDataSets.call_args_list[0].args[0]
        self.assertEqual(list(request.criteria[0].idCriterion.ids), ["a", "b"])

    def test_unresolved_ids_are_simply_absent(self):
        # A dangling dataSetIds entry is not an error; it just does not appear in the result.
        mock_stub = Mock()
        mock_stub.queryDataSets.return_value = self._page(["a"])
        self.client._stub = mock_stub

        found = self.client.get_datasets(["a", "missing"])

        self.assertEqual(set(found), {"a"})

    def test_pages_through_results(self):
        mock_stub = Mock()
        mock_stub.queryDataSets.side_effect = [self._page(["a"], "tok"), self._page(["b"], "")]
        self.client._stub = mock_stub

        found = self.client.get_datasets(["a", "b"])

        self.assertEqual(set(found), {"a", "b"})
        self.assertEqual(mock_stub.queryDataSets.call_count, 2)

    def test_page_error_raises_runtime_error(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "boom"
        mock_stub = Mock()
        mock_stub.queryDataSets.return_value = response
        self.client._stub = mock_stub

        with self.assertRaises(RuntimeError):
            self.client.get_datasets(["a"])

    def test_chunks_long_id_lists(self):
        # The id list is caller-supplied and unbounded, so it must not become one oversized $in.
        ids = [f"id-{i}" for i in range(250)]
        mock_stub = Mock()
        mock_stub.queryDataSets.side_effect = [self._page(ids[0:100]), self._page(ids[100:200]), self._page(ids[200:])]
        self.client._stub = mock_stub

        found = self.client.get_datasets(ids)

        self.assertEqual(len(found), 250)
        self.assertEqual(mock_stub.queryDataSets.call_count, 3)
        sent = [list(call.args[0].criteria[0].idCriterion.ids) for call in mock_stub.queryDataSets.call_args_list]
        self.assertEqual([len(chunk) for chunk in sent], [100, 100, 50])
        self.assertEqual([i for chunk in sent for i in chunk], ids, "chunking must preserve order and lose nothing")

    def test_chunk_size_is_configurable(self):
        mock_stub = Mock()
        mock_stub.queryDataSets.side_effect = [self._page(["a", "b"]), self._page(["c"])]
        self.client._stub = mock_stub

        self.client.get_datasets(["a", "b", "c"], chunk_size=2)

        self.assertEqual(mock_stub.queryDataSets.call_count, 2)

    def test_rejects_non_positive_chunk_size(self):
        with self.assertRaises(ValueError) as ctx:
            self.client.get_datasets(["a"], chunk_size=0)
        self.assertIn("chunk_size", str(ctx.exception))

    def test_short_result_is_logged_at_warning(self):
        # A withheld id is indistinguishable from a dangling one, so the shortfall must not pass silently.
        mock_stub = Mock()
        mock_stub.queryDataSets.return_value = self._page(["a"])
        self.client._stub = mock_stub

        with self.assertLogs(self.client.logger, level="WARNING") as captured:
            self.client.get_datasets(["a", "missing"])

        self.assertIn("resolved only 1 of 2", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
