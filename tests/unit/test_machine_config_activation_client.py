import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client.machine_config_client import (
    ConfigurationActivationQuery,
    MachineConfigClient,
    QueryConfigurationActivationsApiResult,
    SaveConfigurationActivationRequestParams,
)
from dp_python_lib.grpc import annotation_pb2, common_pb2


def _response_with_field(field_name):
    response = Mock()
    response.HasField = Mock(side_effect=lambda field: field == field_name)
    return response


class TestConfigurationActivationQuery(unittest.TestCase):
    def test_timestamp(self):
        c = ConfigurationActivationQuery.timestamp(1_700_000_000)
        self.assertEqual(c.timestampCriterion.timestamp.epochSeconds, 1_700_000_000)

    def test_time_range(self):
        c = ConfigurationActivationQuery.time_range(100, 200)
        self.assertEqual(c.timeRangeCriterion.startTime.epochSeconds, 100)
        self.assertEqual(c.timeRangeCriterion.endTime.epochSeconds, 200)

    def test_time_range_accepts_datetime(self):
        start = datetime(2023, 1, 1, tzinfo=timezone.utc)
        end = datetime(2023, 1, 2, tzinfo=timezone.utc)
        c = ConfigurationActivationQuery.time_range(start, end)
        self.assertEqual(c.timeRangeCriterion.startTime.epochSeconds, int(start.timestamp()))
        self.assertEqual(c.timeRangeCriterion.endTime.epochSeconds, int(end.timestamp()))

    def test_configuration_name(self):
        c = ConfigurationActivationQuery.configuration_name(["cfg-1", "cfg-2"])
        self.assertEqual(list(c.configurationNameCriterion.values), ["cfg-1", "cfg-2"])

    def test_configuration_name_empty_raises(self):
        with self.assertRaises(ValueError):
            ConfigurationActivationQuery.configuration_name([])

    def test_client_activation_id(self):
        c = ConfigurationActivationQuery.client_activation_id(["id-1"])
        self.assertEqual(list(c.clientActivationIdCriterion.values), ["id-1"])

    def test_client_activation_id_empty_raises(self):
        with self.assertRaises(ValueError):
            ConfigurationActivationQuery.client_activation_id([])

    def test_category(self):
        c = ConfigurationActivationQuery.category(["optics"])
        self.assertEqual(list(c.categoryCriterion.values), ["optics"])

    def test_category_empty_raises(self):
        with self.assertRaises(ValueError):
            ConfigurationActivationQuery.category([])

    def test_tags(self):
        c = ConfigurationActivationQuery.tags(["prod"])
        self.assertEqual(list(c.tagsCriterion.values), ["prod"])

    def test_tags_empty_raises(self):
        with self.assertRaises(ValueError):
            ConfigurationActivationQuery.tags([])

    def test_attributes(self):
        c = ConfigurationActivationQuery.attributes("owner", ["ops"])
        self.assertEqual(c.attributesCriterion.key, "owner")
        self.assertEqual(list(c.attributesCriterion.values), ["ops"])

    def test_attributes_empty_key_raises(self):
        with self.assertRaises(ValueError):
            ConfigurationActivationQuery.attributes("", ["v"])

    def test_attributes_empty_values_raises(self):
        with self.assertRaises(ValueError):
            ConfigurationActivationQuery.attributes("owner", [])


class TestBuildSaveActivationRequest(unittest.TestCase):
    def setUp(self):
        self.client = MachineConfigClient(Mock())

    def test_all_fields(self):
        params = SaveConfigurationActivationRequestParams(
            configuration_name="cfg-1",
            start_time=100,
            end_time=200,
            client_activation_id="act-1",
            description="a run",
            tags=["prod", "stable"],
            attributes={"owner": "ops"},
            modified_by="tester",
        )
        request = self.client._build_save_configuration_activation_request(params)

        self.assertEqual(request.configurationName, "cfg-1")
        self.assertEqual(request.startTime.epochSeconds, 100)
        self.assertEqual(request.endTime.epochSeconds, 200)
        self.assertEqual(request.clientActivationId, "act-1")
        self.assertEqual(request.description, "a run")
        self.assertEqual(list(request.tags), ["prod", "stable"])
        self.assertEqual(request.modifiedBy, "tester")
        self.assertEqual({(a.name, a.value) for a in request.attributes}, {("owner", "ops")})

    def test_minimal_fields(self):
        params = SaveConfigurationActivationRequestParams(configuration_name="cfg-1", start_time=100, end_time=200)
        request = self.client._build_save_configuration_activation_request(params)

        self.assertEqual(request.configurationName, "cfg-1")
        self.assertEqual(request.startTime.epochSeconds, 100)
        self.assertEqual(request.endTime.epochSeconds, 200)
        self.assertEqual(request.clientActivationId, "")
        self.assertEqual(len(request.tags), 0)
        self.assertEqual(len(request.attributes), 0)

    def test_datetime_inputs(self):
        start = datetime(2023, 1, 1, tzinfo=timezone.utc)
        end = datetime(2023, 1, 2, tzinfo=timezone.utc)
        params = SaveConfigurationActivationRequestParams(configuration_name="cfg-1", start_time=start, end_time=end)
        request = self.client._build_save_configuration_activation_request(params)
        self.assertEqual(request.startTime.epochSeconds, int(start.timestamp()))
        self.assertEqual(request.endTime.epochSeconds, int(end.timestamp()))

    def test_naive_datetime_rejected(self):
        params = SaveConfigurationActivationRequestParams(
            configuration_name="cfg-1", start_time=datetime(2023, 1, 1), end_time=200
        )
        with self.assertRaises(ValueError):
            self.client._build_save_configuration_activation_request(params)

    def test_open_ended_omits_end_time(self):
        """end_time=None leaves endTime genuinely absent, meaning "still in effect"."""
        params = SaveConfigurationActivationRequestParams(configuration_name="cfg-1", start_time=100, end_time=None)
        request = self.client._build_save_configuration_activation_request(params)

        self.assertEqual(request.startTime.epochSeconds, 100)
        self.assertFalse(request.HasField("endTime"))

    def test_end_time_defaults_to_open_ended(self):
        """end_time may be omitted entirely, not just passed as None."""
        params = SaveConfigurationActivationRequestParams(configuration_name="cfg-1", start_time=100)
        request = self.client._build_save_configuration_activation_request(params)

        self.assertEqual(request.configurationName, "cfg-1")
        self.assertFalse(request.HasField("endTime"))

    def test_open_ended_datetime_start(self):
        start = datetime(2023, 1, 1, tzinfo=timezone.utc)
        params = SaveConfigurationActivationRequestParams(
            configuration_name="cfg-1", start_time=start, client_activation_id="act-open"
        )
        request = self.client._build_save_configuration_activation_request(params)

        self.assertEqual(request.startTime.epochSeconds, int(start.timestamp()))
        self.assertEqual(request.clientActivationId, "act-open")
        self.assertFalse(request.HasField("endTime"))

    def test_bounded_activation_still_sets_end_time(self):
        """The bounded case is unchanged: endTime is present when supplied."""
        params = SaveConfigurationActivationRequestParams(configuration_name="cfg-1", start_time=100, end_time=200)
        request = self.client._build_save_configuration_activation_request(params)

        self.assertTrue(request.HasField("endTime"))
        self.assertEqual(request.endTime.epochSeconds, 200)

    def test_end_time_zero_is_not_treated_as_absent(self):
        """Epoch 0 is a real timestamp; only None means open-ended."""
        params = SaveConfigurationActivationRequestParams(configuration_name="cfg-1", start_time=0, end_time=0)
        request = self.client._build_save_configuration_activation_request(params)

        self.assertTrue(request.HasField("endTime"))
        self.assertEqual(request.endTime.epochSeconds, 0)


class TestActivationKeyValidation(unittest.TestCase):
    def setUp(self):
        self.client = MachineConfigClient(Mock())

    def test_get_build_by_id(self):
        request = self.client._build_get_configuration_activation_request("act-1", None, None)
        self.assertEqual(request.WhichOneof("key"), "clientActivationId")
        self.assertEqual(request.clientActivationId, "act-1")

    def test_get_build_by_composite(self):
        request = self.client._build_get_configuration_activation_request(None, "cfg-1", 100)
        self.assertEqual(request.WhichOneof("key"), "compositeKey")
        self.assertEqual(request.compositeKey.configurationName, "cfg-1")
        self.assertEqual(request.compositeKey.startTime.epochSeconds, 100)

    def test_delete_build_by_id(self):
        request = self.client._build_delete_configuration_activation_request("act-1", None, None)
        self.assertEqual(request.WhichOneof("key"), "clientActivationId")

    def test_delete_build_by_composite(self):
        request = self.client._build_delete_configuration_activation_request(None, "cfg-1", 100)
        self.assertEqual(request.WhichOneof("key"), "compositeKey")
        self.assertEqual(request.compositeKey.configurationName, "cfg-1")

    def test_both_forms_raises(self):
        with self.assertRaises(ValueError):
            self.client._build_get_configuration_activation_request("act-1", "cfg-1", 100)

    def test_neither_form_raises(self):
        with self.assertRaises(ValueError):
            self.client._build_get_configuration_activation_request(None, None, None)

    def test_partial_composite_raises(self):
        with self.assertRaises(ValueError):
            self.client._build_get_configuration_activation_request(None, "cfg-1", None)
        with self.assertRaises(ValueError):
            self.client._build_get_configuration_activation_request(None, None, 100)

    def test_id_and_partial_composite_raises(self):
        # client_activation_id plus a stray composite component is ambiguous -> error.
        with self.assertRaises(ValueError):
            self.client._build_get_configuration_activation_request("act-1", "cfg-1", None)

    def test_empty_string_id_treated_as_not_provided(self):
        # An empty client_activation_id must not silently set an empty oneof; with no composite key it is an error.
        with self.assertRaises(ValueError):
            self.client._build_get_configuration_activation_request("", None, None)
        with self.assertRaises(ValueError):
            self.client._build_delete_configuration_activation_request("", None, None)

    def test_empty_string_id_does_not_block_composite(self):
        # Empty id + a valid composite key should be accepted as the composite form (empty id ignored).
        request = self.client._build_get_configuration_activation_request("", "cfg-1", 100)
        self.assertEqual(request.WhichOneof("key"), "compositeKey")
        self.assertEqual(request.compositeKey.configurationName, "cfg-1")

    def test_empty_configuration_name_treated_as_not_provided(self):
        with self.assertRaises(ValueError):
            self.client._build_get_configuration_activation_request(None, "", 100)

    def test_composite_with_epoch_zero_start_time_is_valid(self):
        # start_time of epoch 0 is falsy but legitimate; presence is keyed on "is not None".
        request = self.client._build_get_configuration_activation_request(None, "cfg-1", 0)
        self.assertEqual(request.WhichOneof("key"), "compositeKey")
        self.assertEqual(request.compositeKey.startTime.epochSeconds, 0)


class TestSaveConfigurationActivation(unittest.TestCase):
    def setUp(self):
        self.client = MachineConfigClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.request = annotation_pb2.SaveConfigurationActivationRequest(configurationName="cfg-1")

    def test_success(self):
        response = _response_with_field("saveConfigurationActivationResult")
        response.saveConfigurationActivationResult.clientActivationId = "act-1"
        self.mock_stub.saveConfigurationActivation.return_value = response

        result = self.client._send_save_configuration_activation(self.request)

        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.client_activation_id, "act-1")
        self.mock_stub.saveConfigurationActivation.assert_called_once_with(self.request)

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "overlap"
        self.mock_stub.saveConfigurationActivation.return_value = response

        result = self.client._send_save_configuration_activation(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "overlap")
        self.assertIsNone(result.client_activation_id)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        self.mock_stub.saveConfigurationActivation.return_value = response
        result = self.client._send_save_configuration_activation(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        err = grpc.RpcError()
        err.details = Mock(return_value="refused")
        self.mock_stub.saveConfigurationActivation.side_effect = err
        result = self.client._send_save_configuration_activation(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: refused", result.result_status.message)

    def test_general_exception(self):
        self.mock_stub.saveConfigurationActivation.side_effect = ValueError("boom")
        result = self.client._send_save_configuration_activation(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestGetConfigurationActivation(unittest.TestCase):
    def setUp(self):
        self.client = MachineConfigClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.request = annotation_pb2.GetConfigurationActivationRequest(clientActivationId="act-1")

    def test_success(self):
        response = _response_with_field("getConfigurationActivationResult")
        response.getConfigurationActivationResult.configurationActivation.clientActivationId = "act-1"
        self.mock_stub.getConfigurationActivation.return_value = response
        result = self.client._send_get_configuration_activation(self.request)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.configuration_activation.clientActivationId, "act-1")

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "not found"
        self.mock_stub.getConfigurationActivation.return_value = response
        result = self.client._send_get_configuration_activation(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIsNone(result.configuration_activation)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        self.mock_stub.getConfigurationActivation.return_value = response
        result = self.client._send_get_configuration_activation(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        err = grpc.RpcError()
        err.details = Mock(return_value="timeout")
        self.mock_stub.getConfigurationActivation.side_effect = err
        result = self.client._send_get_configuration_activation(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: timeout", result.result_status.message)

    def test_general_exception(self):
        self.mock_stub.getConfigurationActivation.side_effect = RuntimeError("boom")
        result = self.client._send_get_configuration_activation(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestDeleteConfigurationActivation(unittest.TestCase):
    def setUp(self):
        self.client = MachineConfigClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.request = annotation_pb2.DeleteConfigurationActivationRequest(clientActivationId="act-1")

    def test_success(self):
        response = _response_with_field("deleteConfigurationActivationResult")
        response.deleteConfigurationActivationResult.clientActivationId = "act-1"
        self.mock_stub.deleteConfigurationActivation.return_value = response
        result = self.client._send_delete_configuration_activation(self.request)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.client_activation_id, "act-1")

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "not found"
        self.mock_stub.deleteConfigurationActivation.return_value = response
        result = self.client._send_delete_configuration_activation(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIsNone(result.client_activation_id)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        self.mock_stub.deleteConfigurationActivation.return_value = response
        result = self.client._send_delete_configuration_activation(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        err = grpc.RpcError()
        err.details = Mock(return_value="unavailable")
        self.mock_stub.deleteConfigurationActivation.side_effect = err
        result = self.client._send_delete_configuration_activation(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: unavailable", result.result_status.message)

    def test_general_exception(self):
        self.mock_stub.deleteConfigurationActivation.side_effect = KeyError("boom")
        result = self.client._send_delete_configuration_activation(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error", result.result_status.message)


class TestQueryConfigurationActivations(unittest.TestCase):
    def setUp(self):
        self.client = MachineConfigClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.request = annotation_pb2.QueryConfigurationActivationsRequest()

    def _result_response(self, ids, next_token=""):
        response = _response_with_field("queryConfigurationActivationsResult")
        response.queryConfigurationActivationsResult.configurationActivations = [
            common_pb2.ConfigurationActivation(clientActivationId=i) for i in ids
        ]
        response.queryConfigurationActivationsResult.nextPageToken = next_token
        return response

    def test_build_request(self):
        criteria = [ConfigurationActivationQuery.configuration_name(["cfg-1"])]
        request = self.client._build_query_configuration_activations_request(criteria, limit=25, page_token="tok")
        self.assertEqual(len(request.criteria), 1)
        self.assertEqual(request.limit, 25)
        self.assertEqual(request.pageToken, "tok")

    def test_build_request_limit_zero_is_set(self):
        request = self.client._build_query_configuration_activations_request(
            [ConfigurationActivationQuery.tags(["x"])], limit=0
        )
        self.assertEqual(request.limit, 0)

    def test_success(self):
        self.mock_stub.queryConfigurationActivations.return_value = self._result_response(["a", "b"], "tok")
        result = self.client._send_query_configuration_activations(self.request)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual([c.clientActivationId for c in result.configuration_activations], ["a", "b"])
        self.assertEqual(result.next_page_token, "tok")

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "bad criteria"
        self.mock_stub.queryConfigurationActivations.return_value = response
        result = self.client._send_query_configuration_activations(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.configuration_activations, [])
        self.assertEqual(result.next_page_token, "")

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        self.mock_stub.queryConfigurationActivations.return_value = response
        result = self.client._send_query_configuration_activations(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        err = grpc.RpcError()
        err.details = Mock(return_value="deadline exceeded")
        self.mock_stub.queryConfigurationActivations.side_effect = err
        result = self.client._send_query_configuration_activations(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: deadline exceeded", result.result_status.message)

    def test_general_exception(self):
        self.mock_stub.queryConfigurationActivations.side_effect = ValueError("boom")
        result = self.client._send_query_configuration_activations(self.request)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestIterConfigurationActivations(unittest.TestCase):
    def setUp(self):
        self.client = MachineConfigClient(Mock())

    def _page(self, ids, next_token=""):
        response = _response_with_field("queryConfigurationActivationsResult")
        response.queryConfigurationActivationsResult.configurationActivations = [
            common_pb2.ConfigurationActivation(clientActivationId=i) for i in ids
        ]
        response.queryConfigurationActivationsResult.nextPageToken = next_token
        return QueryConfigurationActivationsApiResult(is_error=False, message="", response=response)

    def test_pages_through_all(self):
        pages = [self._page(["a", "b"], "tok1"), self._page(["c"], "")]
        self.client.query_configuration_activations = Mock(side_effect=pages)
        ids = [
            c.clientActivationId
            for c in self.client.iter_configuration_activations([ConfigurationActivationQuery.tags(["x"])])
        ]
        self.assertEqual(ids, ["a", "b", "c"])
        self.assertEqual(self.client.query_configuration_activations.call_count, 2)

    def test_error_page_raises(self):
        err_result = QueryConfigurationActivationsApiResult(is_error=True, message="query failed")
        self.client.query_configuration_activations = Mock(return_value=err_result)
        with self.assertRaises(RuntimeError) as ctx:
            list(self.client.iter_configuration_activations([ConfigurationActivationQuery.tags(["x"])]))
        self.assertIn("query failed", str(ctx.exception))


class TestGetActiveConfigurations(unittest.TestCase):
    def setUp(self):
        self.client = MachineConfigClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub

    def test_build_request_explicit_timestamp(self):
        request = self.client._build_get_active_configurations_request(1_700_000_000)
        self.assertEqual(request.timestamp.epochSeconds, 1_700_000_000)

    def test_build_request_defaults_to_now(self):
        # No timestamp -> a current-time timestamp should be populated (non-zero epoch seconds).
        request = self.client._build_get_active_configurations_request()
        self.assertGreater(request.timestamp.epochSeconds, 1_600_000_000)

    def test_success(self):
        response = _response_with_field("getActiveConfigurationsResult")
        response.getActiveConfigurationsResult.configurationActivations = [
            common_pb2.ConfigurationActivation(clientActivationId="a"),
            common_pb2.ConfigurationActivation(clientActivationId="b"),
        ]
        self.mock_stub.getActiveConfigurations.return_value = response
        request = annotation_pb2.GetActiveConfigurationsRequest()
        result = self.client._send_get_active_configurations(request)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual([c.clientActivationId for c in result.configuration_activations], ["a", "b"])

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "bad time"
        self.mock_stub.getActiveConfigurations.return_value = response
        result = self.client._send_get_active_configurations(annotation_pb2.GetActiveConfigurationsRequest())
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.configuration_activations, [])

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        self.mock_stub.getActiveConfigurations.return_value = response
        result = self.client._send_get_active_configurations(annotation_pb2.GetActiveConfigurationsRequest())
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        err = grpc.RpcError()
        err.details = Mock(return_value="refused")
        self.mock_stub.getActiveConfigurations.side_effect = err
        result = self.client._send_get_active_configurations(annotation_pb2.GetActiveConfigurationsRequest())
        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: refused", result.result_status.message)

    def test_general_exception(self):
        self.mock_stub.getActiveConfigurations.side_effect = ValueError("boom")
        result = self.client._send_get_active_configurations(annotation_pb2.GetActiveConfigurationsRequest())
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


if __name__ == "__main__":
    unittest.main()
