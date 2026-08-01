import unittest
from unittest.mock import Mock
from datetime import datetime, timezone
import sys
import os
import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))

from dp_python_lib.client.query_client import (
    QueryClient,
    QueryParams,
    PvQuery,
    ConfigQuery,
    QuerySamplesApiResult,
)
from dp_python_lib.grpc import query_pb2


BEGIN = datetime(2024, 1, 1, tzinfo=timezone.utc)
END = datetime(2024, 1, 2, tzinfo=timezone.utc)
BEGIN_EPOCH = int(BEGIN.timestamp())
END_EPOCH = int(END.timestamp())


def _response_with_field(field_name):
    """
    Build a Mock response whose HasField(field) returns True only for field_name, for error-path tests that don't
    need real nested result fields.
    """
    response = Mock()
    response.HasField = Mock(side_effect=lambda field: field == field_name)
    return response


def _result_response(next_page_token=""):
    """Build a real QuerySamplesResponse carrying a sampleQueryResult with the given nextPageToken."""
    response = query_pb2.QuerySamplesResponse()
    response.sampleQueryResult.nextPageToken = next_page_token
    return response


def _exceptional_response(message):
    """Build a real QuerySamplesResponse carrying an exceptionalResult with the given message."""
    response = query_pb2.QuerySamplesResponse()
    response.exceptionalResult.message = message
    return response


# ----------------------------------------------------------------------
# PvQuery helpers
# ----------------------------------------------------------------------

class TestPvQuery(unittest.TestCase):

    def test_name_list(self):
        selector = PvQuery.name_list(["ABC:1", "ABC:2"])
        self.assertEqual(list(selector.pvNameList.pvNames), ["ABC:1", "ABC:2"])
        self.assertEqual(selector.WhichOneof("selector"), "pvNameList")

    def test_name_list_empty_raises(self):
        with self.assertRaises(ValueError):
            PvQuery.name_list([])

    def test_pattern(self):
        selector = PvQuery.pattern("ABC:.*")
        self.assertEqual(selector.pvNamePattern.pattern, "ABC:.*")
        self.assertEqual(selector.WhichOneof("selector"), "pvNamePattern")

    def test_pattern_empty_raises(self):
        with self.assertRaises(ValueError):
            PvQuery.pattern("")

    def test_metadata(self):
        selector = PvQuery.metadata([PvQuery.tags(["vacuum"])])
        self.assertEqual(selector.WhichOneof("selector"), "metadataQuery")
        self.assertEqual(len(selector.metadataQuery.criteria), 1)

    def test_metadata_empty_raises(self):
        with self.assertRaises(ValueError):
            PvQuery.metadata([])

    def test_pv_name_exact_prefix_contains_coexist(self):
        c = PvQuery.pv_name(exact=["ABC:1"], prefix=["ABC:"], contains=["B"])
        self.assertEqual(list(c.pvNameCriterion.exact), ["ABC:1"])
        self.assertEqual(list(c.pvNameCriterion.prefix), ["ABC:"])
        self.assertEqual(list(c.pvNameCriterion.contains), ["B"])

    def test_pv_name_requires_something(self):
        with self.assertRaises(ValueError):
            PvQuery.pv_name()
        with self.assertRaises(ValueError):
            PvQuery.pv_name(exact=[], prefix=[], contains=[])

    def test_aliases_exact_prefix_contains_coexist(self):
        c = PvQuery.aliases(exact=["a1"], prefix=["a"], contains=["1"])
        self.assertEqual(list(c.aliasesCriterion.exact), ["a1"])
        self.assertEqual(list(c.aliasesCriterion.prefix), ["a"])
        self.assertEqual(list(c.aliasesCriterion.contains), ["1"])

    def test_aliases_requires_something(self):
        with self.assertRaises(ValueError):
            PvQuery.aliases()

    def test_tags(self):
        c = PvQuery.tags(["vacuum"])
        self.assertEqual(list(c.tagsCriterion.values), ["vacuum"])

    def test_tags_empty_raises(self):
        with self.assertRaises(ValueError):
            PvQuery.tags([])

    def test_attr(self):
        c = PvQuery.attr("unit", ["V"])
        self.assertEqual(c.attributesCriterion.key, "unit")
        self.assertEqual(list(c.attributesCriterion.values), ["V"])

    def test_attr_empty_key_raises(self):
        with self.assertRaises(ValueError):
            PvQuery.attr("", ["V"])

    def test_attr_empty_values_raises(self):
        with self.assertRaises(ValueError):
            PvQuery.attr("unit", [])


# ----------------------------------------------------------------------
# ConfigQuery helpers
# ----------------------------------------------------------------------

class TestConfigQuery(unittest.TestCase):

    def test_configuration_name(self):
        c = ConfigQuery.configuration_name(["beamline-optics"])
        self.assertEqual(list(c.configurationNameCriterion.values), ["beamline-optics"])

    def test_configuration_name_empty_raises(self):
        with self.assertRaises(ValueError):
            ConfigQuery.configuration_name([])

    def test_client_activation_id(self):
        c = ConfigQuery.client_activation_id(["act-1"])
        self.assertEqual(list(c.clientActivationIdCriterion.values), ["act-1"])

    def test_category(self):
        c = ConfigQuery.category(["optics"])
        self.assertEqual(list(c.categoryCriterion.values), ["optics"])

    def test_tags(self):
        c = ConfigQuery.tags(["production"])
        self.assertEqual(list(c.tagsCriterion.values), ["production"])

    def test_attr(self):
        c = ConfigQuery.attr("owner", ["ops"])
        self.assertEqual(c.attributesCriterion.key, "owner")
        self.assertEqual(list(c.attributesCriterion.values), ["ops"])

    def test_empties_raise(self):
        for call in (lambda: ConfigQuery.client_activation_id([]),
                     lambda: ConfigQuery.category([]),
                     lambda: ConfigQuery.tags([]),
                     lambda: ConfigQuery.attr("k", []),
                     lambda: ConfigQuery.attr("", ["v"])):
            with self.assertRaises(ValueError):
                call()


# ----------------------------------------------------------------------
# QueryParams validation
# ----------------------------------------------------------------------

class TestQueryParams(unittest.TestCase):

    def test_valid_with_pv_selector(self):
        p = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("ABC:.*"))
        self.assertIsNotNone(p.pv_selector)

    def test_valid_config_only(self):
        # A config-only query (no pv_selector) is legal.
        p = QueryParams(BEGIN, END, config_criteria=[ConfigQuery.configuration_name(["c"])])
        self.assertIsNone(p.pv_selector)

    def test_requires_a_selector_or_config(self):
        with self.assertRaises(ValueError):
            QueryParams(BEGIN, END)

    def test_begin_equal_end_raises(self):
        with self.assertRaises(ValueError):
            QueryParams(BEGIN, BEGIN, pv_selector=PvQuery.pattern("x"))

    def test_begin_after_end_raises(self):
        with self.assertRaises(ValueError):
            QueryParams(END, BEGIN, pv_selector=PvQuery.pattern("x"))

    def test_missing_times_raise(self):
        with self.assertRaises(ValueError):
            QueryParams(None, END, pv_selector=PvQuery.pattern("x"))
        with self.assertRaises(ValueError):
            QueryParams(BEGIN, None, pv_selector=PvQuery.pattern("x"))


# ----------------------------------------------------------------------
# Request building (_build_query_spec / _build_query_samples_request)
# ----------------------------------------------------------------------

class TestBuildRequest(unittest.TestCase):

    def setUp(self):
        self.client = QueryClient(Mock())

    def test_build_spec_roundtrip_full(self):
        p = QueryParams(
            BEGIN, END,
            pv_selector=PvQuery.metadata([PvQuery.pv_name(prefix=["ABC:"]), PvQuery.tags(["vacuum"])]),
            config_criteria=[ConfigQuery.configuration_name(["beamline-optics"])],
            limit=100)
        req = self.client._build_query_samples_request(p, page_token="tok")

        # time range
        self.assertEqual(req.querySpec.timeRange.beginTime.epochSeconds, BEGIN_EPOCH)
        self.assertEqual(req.querySpec.timeRange.endTime.epochSeconds, END_EPOCH)
        # pv selector
        self.assertEqual(req.querySpec.pvSelector.WhichOneof("selector"), "metadataQuery")
        criteria = req.querySpec.pvSelector.metadataQuery.criteria
        self.assertEqual(list(criteria[0].pvNameCriterion.prefix), ["ABC:"])
        self.assertEqual(list(criteria[1].tagsCriterion.values), ["vacuum"])
        # config selector
        self.assertEqual(
            list(req.querySpec.configurationSelector.criteria[0].configurationNameCriterion.values),
            ["beamline-optics"])
        # execution options
        self.assertEqual(req.executionOptions.limit, 100)
        self.assertEqual(req.executionOptions.pageToken, "tok")
        # representation
        self.assertFalse(req.resultRepresentation.useSerializedColumns)
        self.assertFalse(req.resultRepresentation.excludeColumnMetadata)

    def test_build_config_only(self):
        p = QueryParams(BEGIN, END, config_criteria=[ConfigQuery.category(["optics"])])
        req = self.client._build_query_samples_request(p)
        self.assertEqual(req.querySpec.pvSelector.WhichOneof("selector"), None)
        self.assertEqual(len(req.querySpec.configurationSelector.criteria), 1)

    def test_build_no_limit_no_token(self):
        p = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"))
        req = self.client._build_query_samples_request(p)
        self.assertEqual(req.executionOptions.limit, 0)
        self.assertEqual(req.executionOptions.pageToken, "")

    def test_build_limit_zero_is_set(self):
        # limit=0 is a legitimate value distinct from "unset"; it must be honored.
        p = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"), limit=0)
        req = self.client._build_query_samples_request(p)
        self.assertEqual(req.executionOptions.limit, 0)

    def test_build_exclude_metadata(self):
        p = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"), exclude_column_metadata=True)
        req = self.client._build_query_samples_request(p)
        self.assertTrue(req.resultRepresentation.excludeColumnMetadata)


# ----------------------------------------------------------------------
# querySamples (unary) send + result
# ----------------------------------------------------------------------

class TestQuerySamplesUnary(unittest.TestCase):

    def setUp(self):
        self.client = QueryClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.params = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"))

    def test_success(self):
        self.mock_stub.querySamples.return_value = _result_response(next_page_token="tok")
        result = self.client.query_samples(self.params)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.next_page_token, "tok")
        self.assertIsNotNone(result.column_table)

    def test_business_error(self):
        self.mock_stub.querySamples.return_value = _exceptional_response("bad query")
        result = self.client.query_samples(self.params)
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "bad query")
        self.assertIsNone(result.column_table)
        self.assertEqual(result.next_page_token, "")

    def test_unexpected_response_format(self):
        response = _response_with_field('somethingElse')
        self.mock_stub.querySamples.return_value = response
        result = self.client.query_samples(self.params)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        err = grpc.RpcError()
        err.details = lambda: "connection refused"
        self.mock_stub.querySamples.side_effect = err
        result = self.client.query_samples(self.params)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error", result.result_status.message)

    def test_unexpected_exception(self):
        self.mock_stub.querySamples.side_effect = ValueError("boom")
        result = self.client.query_samples(self.params)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error", result.result_status.message)


# ----------------------------------------------------------------------
# iter_query_samples (unary paging)
# ----------------------------------------------------------------------

class TestIterQuerySamples(unittest.TestCase):

    def setUp(self):
        self.client = QueryClient(Mock())
        self.params = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"))

    def test_paging_threads_token(self):
        # Two pages: page 1 returns "tok", page 2 returns "" (end).
        page1 = QuerySamplesApiResult(is_error=False, message="", response=_result_response("tok"))
        page2 = QuerySamplesApiResult(is_error=False, message="", response=_result_response(""))
        calls = []

        def fake_query_samples(params, page_token=None):
            calls.append(page_token)
            return page1 if page_token is None else page2

        self.client.query_samples = Mock(side_effect=fake_query_samples)
        results = list(self.client.iter_query_samples(self.params))
        self.assertEqual(len(results), 2)
        # page 1 sent no token; page 2 sent page 1's token.
        self.assertEqual(calls, [None, "tok"])

    def test_single_page(self):
        page = QuerySamplesApiResult(is_error=False, message="", response=_result_response(""))
        self.client.query_samples = Mock(return_value=page)
        results = list(self.client.iter_query_samples(self.params))
        self.assertEqual(len(results), 1)

    def test_error_page_raises_runtime_error(self):
        err = QuerySamplesApiResult(is_error=True, message="boom")
        self.client.query_samples = Mock(return_value=err)
        with self.assertRaises(RuntimeError):
            list(self.client.iter_query_samples(self.params))


# ----------------------------------------------------------------------
# querySamplesStream (server-streaming)
# ----------------------------------------------------------------------

class TestQuerySamplesStream(unittest.TestCase):

    def setUp(self):
        self.client = QueryClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.params = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"))

    def test_stream_yields_pages(self):
        self.mock_stub.querySamplesStream.return_value = iter(
            [_result_response(), _result_response()])
        results = list(self.client.iter_query_samples_stream(self.params))
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not r.result_status.is_error for r in results))

    def test_stream_never_sends_page_token(self):
        self.mock_stub.querySamplesStream.return_value = iter([_result_response()])
        list(self.client.iter_query_samples_stream(self.params))
        sent = self.mock_stub.querySamplesStream.call_args[0][0]
        self.assertEqual(sent.executionOptions.pageToken, "")

    def test_empty_stream(self):
        self.mock_stub.querySamplesStream.return_value = iter([])
        results = list(self.client.iter_query_samples_stream(self.params))
        self.assertEqual(results, [])

    def test_business_error_mid_stream_raises(self):
        self.mock_stub.querySamplesStream.return_value = iter(
            [_result_response(), _exceptional_response("mid-stream boom")])
        collected = []
        with self.assertRaises(RuntimeError):
            for r in self.client.iter_query_samples_stream(self.params):
                collected.append(r)
        # The good page before the error was yielded.
        self.assertEqual(len(collected), 1)

    def test_grpc_error_mid_stream_raises(self):
        err = grpc.RpcError()
        err.details = lambda: "stream reset"

        def exploding_stream():
            yield _result_response()
            raise err

        self.mock_stub.querySamplesStream.return_value = exploding_stream()
        collected = []
        with self.assertRaises(RuntimeError):
            for r in self.client.iter_query_samples_stream(self.params):
                collected.append(r)
        self.assertEqual(len(collected), 1)


if __name__ == "__main__":
    unittest.main()
