import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client.machine_config_client import (
    ConfigurationQuery,
    MachineConfigClient,
    QueryConfigurationsApiResult,
    SaveConfigurationApiResult,
    SaveConfigurationRequestParams,
    to_timestamp,
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


class TestToTimestamp(unittest.TestCase):
    """Unit tests for the to_timestamp() conversion helper."""

    def test_passthrough_timestamp(self):
        ts = common_pb2.Timestamp()
        ts.epochSeconds = 123
        ts.nanoseconds = 456
        self.assertIs(to_timestamp(ts), ts)

    def test_int_epoch_seconds(self):
        ts = to_timestamp(1_700_000_000)
        self.assertEqual(ts.epochSeconds, 1_700_000_000)
        self.assertEqual(ts.nanoseconds, 0)

    def test_float_epoch_seconds_with_fraction(self):
        ts = to_timestamp(1_700_000_000.25)
        self.assertEqual(ts.epochSeconds, 1_700_000_000)
        self.assertEqual(ts.nanoseconds, 250_000_000)

    def test_aware_datetime_utc(self):
        dt = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
        expected_epoch = int(dt.timestamp())
        ts = to_timestamp(dt)
        self.assertEqual(ts.epochSeconds, expected_epoch)
        self.assertEqual(ts.nanoseconds, 0)

    def test_aware_datetime_nonzero_offset(self):
        tz = timezone(timedelta(hours=-5))
        dt = datetime(2023, 11, 14, 17, 13, 20, tzinfo=tz)  # same instant as the UTC test above
        ts = to_timestamp(dt)
        self.assertEqual(ts.epochSeconds, int(dt.timestamp()))

    def test_negative_epoch_rejected_by_uint64_field(self):
        # common.Timestamp.epochSeconds is uint64, so pre-1970 (negative) epochs cannot be represented.
        # Flooring keeps nanoseconds normalized, and the negative seconds are cleanly rejected at assignment
        # (ValueError: out of range) rather than silently producing an incorrect (seconds, nanos) pair.
        with self.assertRaises(ValueError):
            to_timestamp(-1.25)
        with self.assertRaises(ValueError):
            to_timestamp(-5)

    def test_naive_datetime_raises(self):
        with self.assertRaises(ValueError):
            to_timestamp(datetime(2023, 11, 14, 22, 13, 20))

    def test_bool_raises(self):
        with self.assertRaises(TypeError):
            to_timestamp(True)

    def test_unsupported_type_raises(self):
        with self.assertRaises(TypeError):
            to_timestamp("2023-11-14")


class TestConfigurationQuery(unittest.TestCase):
    """Unit tests for the ConfigurationQuery criterion helpers."""

    def test_name_exact_prefix_contains(self):
        c = ConfigurationQuery.name(exact=["cfg-a"], prefix=["beamline-"], contains=["prod"])
        self.assertEqual(list(c.nameCriterion.exact), ["cfg-a"])
        self.assertEqual(list(c.nameCriterion.prefix), ["beamline-"])
        self.assertEqual(list(c.nameCriterion.contains), ["prod"])

    def test_name_requires_something(self):
        with self.assertRaises(ValueError):
            ConfigurationQuery.name()
        with self.assertRaises(ValueError):
            ConfigurationQuery.name(exact=[], prefix=[], contains=[])

    def test_category(self):
        c = ConfigurationQuery.category(["optics", "vacuum"])
        self.assertEqual(list(c.categoryCriterion.values), ["optics", "vacuum"])

    def test_category_empty_raises(self):
        with self.assertRaises(ValueError):
            ConfigurationQuery.category([])

    def test_tags(self):
        c = ConfigurationQuery.tags(["production"])
        self.assertEqual(list(c.tagsCriterion.values), ["production"])

    def test_tags_empty_raises(self):
        with self.assertRaises(ValueError):
            ConfigurationQuery.tags([])

    def test_attributes(self):
        c = ConfigurationQuery.attributes("owner", ["ops", "physics"])
        self.assertEqual(c.attributesCriterion.key, "owner")
        self.assertEqual(list(c.attributesCriterion.values), ["ops", "physics"])

    def test_attributes_empty_key_raises(self):
        with self.assertRaises(ValueError):
            ConfigurationQuery.attributes("", ["v"])

    def test_attributes_empty_values_raises(self):
        with self.assertRaises(ValueError):
            ConfigurationQuery.attributes("owner", [])

    def test_parent(self):
        c = ConfigurationQuery.parent(["root-cfg"])
        self.assertEqual(list(c.parentCriterion.values), ["root-cfg"])

    def test_parent_empty_raises(self):
        with self.assertRaises(ValueError):
            ConfigurationQuery.parent([])


class TestMachineConfigClientBuildRequests(unittest.TestCase):
    """Unit tests for the request-building helpers (no gRPC calls)."""

    def setUp(self):
        self.mock_channel = Mock()
        self.client = MachineConfigClient(self.mock_channel)

    def test_build_save_request_all_fields(self):
        params = SaveConfigurationRequestParams(
            configuration_name="cfg-1",
            category="optics",
            description="a test configuration",
            parent_configuration_name="root-cfg",
            tags=["production", "stable"],
            attributes={"owner": "ops", "rev": "3"},
            modified_by="tester",
        )
        request = self.client._build_save_configuration_request(params)

        self.assertEqual(request.configurationName, "cfg-1")
        self.assertEqual(request.category, "optics")
        self.assertEqual(request.description, "a test configuration")
        self.assertEqual(request.parentConfigurationName, "root-cfg")
        self.assertEqual(list(request.tags), ["production", "stable"])
        self.assertEqual(request.modifiedBy, "tester")
        self.assertEqual({(a.name, a.value) for a in request.attributes}, {("owner", "ops"), ("rev", "3")})

    def test_build_save_request_name_only(self):
        params = SaveConfigurationRequestParams(configuration_name="cfg-1")
        request = self.client._build_save_configuration_request(params)

        self.assertEqual(request.configurationName, "cfg-1")
        self.assertEqual(request.category, "")
        self.assertEqual(request.description, "")
        self.assertEqual(request.parentConfigurationName, "")
        self.assertEqual(len(request.tags), 0)
        self.assertEqual(len(request.attributes), 0)
        self.assertEqual(request.modifiedBy, "")

    def test_build_get_request(self):
        request = self.client._build_get_configuration_request("cfg-1")
        self.assertEqual(request.configurationName, "cfg-1")

    def test_build_delete_request(self):
        request = self.client._build_delete_configuration_request("cfg-1")
        self.assertEqual(request.configurationName, "cfg-1")

    def test_build_query_request_with_criteria_limit_token(self):
        criteria = [
            ConfigurationQuery.name(prefix=["beamline-"]),
            ConfigurationQuery.tags(["production"]),
        ]
        request = self.client._build_query_configurations_request(criteria, limit=50, page_token="tok")
        self.assertEqual(len(request.criteria), 2)
        self.assertEqual(request.limit, 50)
        self.assertEqual(request.pageToken, "tok")

    def test_build_query_request_no_limit_no_token(self):
        criteria = [ConfigurationQuery.category(["optics"])]
        request = self.client._build_query_configurations_request(criteria)
        self.assertEqual(len(request.criteria), 1)
        self.assertEqual(request.limit, 0)
        self.assertEqual(request.pageToken, "")

    def test_build_query_request_limit_zero_is_set(self):
        # limit=0 must be forwarded (distinct from "not provided"); guard against a truthiness regression.
        request = self.client._build_query_configurations_request([ConfigurationQuery.tags(["x"])], limit=0)
        self.assertEqual(request.limit, 0)


class TestSaveConfiguration(unittest.TestCase):
    def setUp(self):
        self.client = MachineConfigClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.request = annotation_pb2.SaveConfigurationRequest(configurationName="cfg-1")

    def test_success(self):
        response = _response_with_field("saveConfigurationResult")
        response.saveConfigurationResult.configurationName = "cfg-1"
        self.mock_stub.saveConfiguration.return_value = response

        result = self.client._send_save_configuration(self.request)

        self.assertIsInstance(result, SaveConfigurationApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.configuration_name, "cfg-1")
        self.mock_stub.saveConfiguration.assert_called_once_with(self.request)

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "duplicate name"
        self.mock_stub.saveConfiguration.return_value = response

        result = self.client._send_save_configuration(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "duplicate name")
        self.assertIsNone(result.configuration_name)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        self.mock_stub.saveConfiguration.return_value = response

        result = self.client._send_save_configuration(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        err = grpc.RpcError()
        err.details = Mock(return_value="connection refused")
        self.mock_stub.saveConfiguration.side_effect = err

        result = self.client._send_save_configuration(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: connection refused", result.result_status.message)

    def test_general_exception(self):
        self.mock_stub.saveConfiguration.side_effect = ValueError("boom")

        result = self.client._send_save_configuration(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestGetConfiguration(unittest.TestCase):
    def setUp(self):
        self.client = MachineConfigClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.request = annotation_pb2.GetConfigurationRequest(configurationName="cfg-1")

    def test_success(self):
        response = _response_with_field("getConfigurationResult")
        response.getConfigurationResult.configuration.configurationName = "cfg-1"
        self.mock_stub.getConfiguration.return_value = response

        result = self.client._send_get_configuration(self.request)

        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.configuration.configurationName, "cfg-1")

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "not found"
        self.mock_stub.getConfiguration.return_value = response

        result = self.client._send_get_configuration(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "not found")
        self.assertIsNone(result.configuration)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        self.mock_stub.getConfiguration.return_value = response

        result = self.client._send_get_configuration(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        err = grpc.RpcError()
        err.details = Mock(return_value="timeout")
        self.mock_stub.getConfiguration.side_effect = err

        result = self.client._send_get_configuration(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: timeout", result.result_status.message)

    def test_general_exception(self):
        self.mock_stub.getConfiguration.side_effect = RuntimeError("boom")

        result = self.client._send_get_configuration(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestDeleteConfiguration(unittest.TestCase):
    def setUp(self):
        self.client = MachineConfigClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.request = annotation_pb2.DeleteConfigurationRequest(configurationName="cfg-1")

    def test_success(self):
        response = _response_with_field("deleteConfigurationResult")
        response.deleteConfigurationResult.configurationName = "cfg-1"
        self.mock_stub.deleteConfiguration.return_value = response

        result = self.client._send_delete_configuration(self.request)

        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.configuration_name, "cfg-1")

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "in use"
        self.mock_stub.deleteConfiguration.return_value = response

        result = self.client._send_delete_configuration(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "in use")
        self.assertIsNone(result.configuration_name)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        self.mock_stub.deleteConfiguration.return_value = response

        result = self.client._send_delete_configuration(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        err = grpc.RpcError()
        err.details = Mock(return_value="unavailable")
        self.mock_stub.deleteConfiguration.side_effect = err

        result = self.client._send_delete_configuration(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: unavailable", result.result_status.message)

    def test_general_exception(self):
        self.mock_stub.deleteConfiguration.side_effect = KeyError("boom")

        result = self.client._send_delete_configuration(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error", result.result_status.message)


class TestQueryConfigurations(unittest.TestCase):
    def setUp(self):
        self.client = MachineConfigClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.request = annotation_pb2.QueryConfigurationsRequest()

    def _result_response(self, names, next_token=""):
        response = _response_with_field("queryConfigurationsResult")
        configs = [common_pb2.Configuration(configurationName=n) for n in names]
        response.queryConfigurationsResult.configurations = configs
        response.queryConfigurationsResult.nextPageToken = next_token
        return response

    def test_success(self):
        self.mock_stub.queryConfigurations.return_value = self._result_response(["a", "b"], "tok")

        result = self.client._send_query_configurations(self.request)

        self.assertFalse(result.result_status.is_error)
        self.assertEqual([c.configurationName for c in result.configurations], ["a", "b"])
        self.assertEqual(result.next_page_token, "tok")

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "bad criteria"
        self.mock_stub.queryConfigurations.return_value = response

        result = self.client._send_query_configurations(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "bad criteria")
        self.assertEqual(result.configurations, [])
        self.assertEqual(result.next_page_token, "")

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        self.mock_stub.queryConfigurations.return_value = response

        result = self.client._send_query_configurations(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        err = grpc.RpcError()
        err.details = Mock(return_value="deadline exceeded")
        self.mock_stub.queryConfigurations.side_effect = err

        result = self.client._send_query_configurations(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: deadline exceeded", result.result_status.message)

    def test_general_exception(self):
        self.mock_stub.queryConfigurations.side_effect = ValueError("boom")

        result = self.client._send_query_configurations(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestIterConfigurations(unittest.TestCase):
    def setUp(self):
        self.client = MachineConfigClient(Mock())

    def _page(self, names, next_token=""):
        response = _response_with_field("queryConfigurationsResult")
        response.queryConfigurationsResult.configurations = [
            common_pb2.Configuration(configurationName=n) for n in names
        ]
        response.queryConfigurationsResult.nextPageToken = next_token
        return QueryConfigurationsApiResult(is_error=False, message="", response=response)

    def test_pages_through_all(self):
        pages = [self._page(["a", "b"], "tok1"), self._page(["c"], "")]
        self.client.query_configurations = Mock(side_effect=pages)

        names = [c.configurationName for c in self.client.iter_configurations([ConfigurationQuery.tags(["x"])])]

        self.assertEqual(names, ["a", "b", "c"])
        self.assertEqual(self.client.query_configurations.call_count, 2)

    def test_single_page(self):
        self.client.query_configurations = Mock(return_value=self._page(["only"], ""))
        names = [c.configurationName for c in self.client.iter_configurations([ConfigurationQuery.tags(["x"])])]
        self.assertEqual(names, ["only"])

    def test_error_page_raises(self):
        err_result = QueryConfigurationsApiResult(is_error=True, message="query failed")
        self.client.query_configurations = Mock(return_value=err_result)

        with self.assertRaises(RuntimeError) as ctx:
            list(self.client.iter_configurations([ConfigurationQuery.tags(["x"])]))
        self.assertIn("query failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
