import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from assignment_spy import watch_assignments

from dp_python_lib.client.query_client import (
    ConfigQuery,
    PvQuery,
    QueryBucketsApiResult,
    QueryClient,
    QueryParams,
    QuerySamplesApiResult,
    SampleStatusFilter,
)
from dp_python_lib.grpc import common_pb2, query_pb2

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

    def test_attr_key_only(self):
        # An absent/empty values list is a key-only existence search (issue #40), not a rejection.
        for criterion in (PvQuery.attr("unit"), PvQuery.attr("unit", [])):
            self.assertTrue(criterion.HasField("attributesCriterion"))
            self.assertEqual(criterion.attributesCriterion.key, "unit")
            self.assertEqual(list(criterion.attributesCriterion.values), [])


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
        for call in (
            lambda: ConfigQuery.client_activation_id([]),
            lambda: ConfigQuery.category([]),
            lambda: ConfigQuery.tags([]),
            lambda: ConfigQuery.attr("", ["v"]),
        ):
            with self.assertRaises(ValueError):
                call()

    def test_attr_key_only(self):
        # An absent/empty values list is a key-only existence search (issue #40), not a rejection.
        for criterion in (ConfigQuery.attr("owner"), ConfigQuery.attr("owner", [])):
            self.assertTrue(criterion.HasField("attributesCriterion"))
            self.assertEqual(criterion.attributesCriterion.key, "owner")
            self.assertEqual(list(criterion.attributesCriterion.values), [])


# ----------------------------------------------------------------------
# QueryParams validation
# ----------------------------------------------------------------------


class TestQueryParams(unittest.TestCase):
    def test_valid_with_pv_selector(self):
        p = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("ABC:.*"))
        self.assertIsNotNone(p.pv_selector)

    def test_omitting_the_selector_is_a_type_error(self):
        # The realistic mistake: config criteria alone.  pv_selector has no default, so this fails at the call,
        # before QueryParams' own check runs -- and mypy flags it statically.
        with self.assertRaises(TypeError):
            QueryParams(BEGIN, END, config_criteria=[ConfigQuery.configuration_name(["c"])])  # type: ignore[call-arg]

    def test_config_only_is_rejected(self):
        # The server requires a PV selector even with config criteria (#17 PR B), so fail here, pointing at ".*".
        with self.assertRaises(ValueError) as ctx:
            QueryParams(BEGIN, END, pv_selector=None, config_criteria=[ConfigQuery.configuration_name(["c"])])
        self.assertIn('PvQuery.pattern(".*")', str(ctx.exception))

    def test_requires_a_selector(self):
        with self.assertRaises(ValueError):
            QueryParams(BEGIN, END, pv_selector=None)

    def test_an_empty_selector_is_rejected(self):
        # A default-constructed PvSelector sets no oneof arm; the server rejects that too.
        with self.assertRaises(ValueError):
            QueryParams(BEGIN, END, pv_selector=query_pb2.PvSelector())

    def test_config_criteria_still_narrow_a_selector(self):
        p = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern(".*"), config_criteria=[ConfigQuery.category(["c"])])
        self.assertEqual(len(p.config_criteria), 1)

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

    def test_negative_limit_raises_at_construction(self):
        # Caught here rather than surfacing later as a raw protobuf "Value out of range" from request building.
        with self.assertRaises(ValueError):
            QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"), limit=-1)

    def test_zero_and_positive_limits_accepted(self):
        # limit=0 is meaningful per the proto ("the server selects an appropriate default"), not an error.
        self.assertEqual(QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"), limit=0).limit, 0)
        self.assertEqual(QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"), limit=10).limit, 10)

    def test_validated_timestamps_exposed(self):
        p = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"))
        self.assertEqual(p.begin_timestamp.epochSeconds, BEGIN_EPOCH)
        self.assertEqual(p.end_timestamp.epochSeconds, END_EPOCH)


# ----------------------------------------------------------------------
# Request building (_build_query_spec / _build_query_samples_request)
# ----------------------------------------------------------------------


class TestBuildRequest(unittest.TestCase):
    def setUp(self):
        self.client = QueryClient(Mock())

    def test_build_spec_roundtrip_full(self):
        p = QueryParams(
            BEGIN,
            END,
            pv_selector=PvQuery.metadata([PvQuery.pv_name(prefix=["ABC:"]), PvQuery.tags(["vacuum"])]),
            config_criteria=[ConfigQuery.configuration_name(["beamline-optics"])],
            limit=100,
        )
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
            ["beamline-optics"],
        )
        # execution options
        self.assertEqual(req.executionOptions.limit, 100)
        self.assertEqual(req.executionOptions.pageToken, "tok")
        # representation
        self.assertFalse(req.resultRepresentation.useSerializedColumns)
        self.assertFalse(req.resultRepresentation.excludeColumnMetadata)

    def test_build_with_config_criteria(self):
        p = QueryParams(
            BEGIN, END, pv_selector=PvQuery.pattern(".*"), config_criteria=[ConfigQuery.category(["optics"])]
        )
        req = self.client._build_query_samples_request(p)
        self.assertEqual(req.querySpec.pvSelector.pvNamePattern.pattern, ".*")
        self.assertEqual(len(req.querySpec.configurationSelector.criteria), 1)

    def test_build_no_limit_no_token(self):
        p = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"))
        req = self.client._build_query_samples_request(p)
        self.assertEqual(req.executionOptions.limit, 0)
        self.assertEqual(req.executionOptions.pageToken, "")

    def test_build_limit_zero_is_set(self):
        # limit=0 is a legitimate value distinct from "unset"; it must be honored (cf. issue #13).
        # A proto3 scalar reads 0 whether or not it was assigned, so watch the assignment itself.
        p = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"), limit=0)
        with watch_assignments(query_pb2, "QuerySamplesRequest", "executionOptions.limit") as assigned:
            self.client._build_query_samples_request(p)
        self.assertEqual(assigned, [0], "limit=0 must be assigned, not dropped by a truthiness guard")

    def test_build_uses_validated_timestamps_not_reconversion(self):
        # The spec must carry the timestamps QueryParams converted and range-checked at construction.  Mutating
        # the public begin_time afterwards bypasses that validation, so it must not leak into the request.
        p = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"))
        p.begin_time = END  # would invert the range if re-converted here
        req = self.client._build_query_samples_request(p)
        self.assertEqual(req.querySpec.timeRange.beginTime.epochSeconds, BEGIN_EPOCH)
        self.assertEqual(req.querySpec.timeRange.endTime.epochSeconds, END_EPOCH)

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
        response = _response_with_field("somethingElse")
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
        self.mock_stub.querySamplesStream.return_value = iter([_result_response(), _result_response()])
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
            [_result_response(), _exceptional_response("mid-stream boom")]
        )
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


# ----------------------------------------------------------------------
# SampleStatusFilter / sampleStatusSelector
# ----------------------------------------------------------------------


class TestSampleStatusFilter(unittest.TestCase):
    def test_include_sets_include_mode(self):
        selector = SampleStatusFilter.include("data_quality")
        self.assertEqual(selector.domain, "data_quality")
        self.assertEqual(selector.mode, query_pb2.SampleStatusSelector.MODE_INCLUDE_MATCHING)

    def test_exclude_sets_exclude_mode(self):
        selector = SampleStatusFilter.exclude("data_quality")
        self.assertEqual(selector.mode, query_pb2.SampleStatusSelector.MODE_EXCLUDE_MATCHING)

    def test_neither_constructor_can_produce_unspecified_mode(self):
        # MODE_UNSPECIFIED is the enum's zero value and is rejected by the server.  Routing construction through
        # the two named constructors is what makes that state unreachable, so assert it directly.
        for selector in (SampleStatusFilter.include("d"), SampleStatusFilter.exclude("d")):
            self.assertNotEqual(selector.mode, query_pb2.SampleStatusSelector.MODE_UNSPECIFIED)

    def test_layers_and_status_codes_are_carried(self):
        selector = SampleStatusFilter.exclude("data_quality", layers=["ml_v1", "ops"], status_codes=[2, 3])
        self.assertEqual(list(selector.layers), ["ml_v1", "ops"])
        self.assertEqual(list(selector.statusCodes), [2, 3])

    def test_omitted_layers_and_status_codes_stay_empty(self):
        # Empty means "match all layers / any status code", so nothing may be invented here.
        selector = SampleStatusFilter.include("data_quality")
        self.assertEqual(list(selector.layers), [])
        self.assertEqual(list(selector.statusCodes), [])

    def test_empty_domain_raises(self):
        for factory in (SampleStatusFilter.include, SampleStatusFilter.exclude):
            with self.assertRaises(ValueError) as ctx:
                factory("")
            self.assertIn("domain", str(ctx.exception))

    def test_status_code_zero_is_carried(self):
        # 0 is a legitimate int32 status code; a truthiness guard on the list would still keep it, but a per-item
        # filter would drop it.  Pin the whole list.
        selector = SampleStatusFilter.include("d", status_codes=[0, 1])
        self.assertEqual(list(selector.statusCodes), [0, 1])


class TestBuildRequestSampleStatusSelector(unittest.TestCase):
    def setUp(self):
        self.client = QueryClient(Mock())

    def test_selector_is_copied_into_the_query_spec(self):
        p = QueryParams(
            BEGIN,
            END,
            pv_selector=PvQuery.pattern("ABC:.*"),
            sample_status_filter=SampleStatusFilter.exclude("data_quality", layers=["ml_v1"], status_codes=[2]),
        )
        req = self.client._build_query_samples_request(p)

        selector = req.querySpec.sampleStatusSelector
        self.assertEqual(selector.domain, "data_quality")
        self.assertEqual(selector.mode, query_pb2.SampleStatusSelector.MODE_EXCLUDE_MATCHING)
        self.assertEqual(list(selector.layers), ["ml_v1"])
        self.assertEqual(list(selector.statusCodes), [2])

    def test_omitted_filter_leaves_selector_unset(self):
        # An unset message field must stay genuinely absent on the wire, not present-but-empty: a present selector
        # with an empty domain and MODE_UNSPECIFIED is a request the server rejects.
        p = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("ABC:.*"))
        req = self.client._build_query_samples_request(p)
        self.assertFalse(req.querySpec.HasField("sampleStatusSelector"))

    def test_selector_reaches_the_streaming_path_via_the_shared_builder(self):
        # iter_query_samples_stream() builds its request with _build_query_samples_request() -- the same builder the
        # unary path uses -- so exercising that builder is what covers the streaming path; there is no separate
        # streaming request builder to test.  Pinned here so a future split into two builders fails loudly rather
        # than silently leaving the streaming path unfiltered.
        p = QueryParams(
            BEGIN,
            END,
            pv_selector=PvQuery.pattern("ABC:.*"),
            sample_status_filter=SampleStatusFilter.include("data_quality"),
        )
        req = self.client._build_query_samples_request(p, page_token=None)
        self.assertTrue(req.querySpec.HasField("sampleStatusSelector"))
        self.assertEqual(req.querySpec.sampleStatusSelector.domain, "data_quality")

    def test_selector_coexists_with_pv_and_config_selectors(self):
        p = QueryParams(
            BEGIN,
            END,
            pv_selector=PvQuery.name_list(["ABC:1"]),
            config_criteria=[ConfigQuery.category(["optics"])],
            sample_status_filter=SampleStatusFilter.exclude("data_quality"),
        )
        req = self.client._build_query_samples_request(p)
        self.assertEqual(list(req.querySpec.pvSelector.pvNameList.pvNames), ["ABC:1"])
        self.assertEqual(len(req.querySpec.configurationSelector.criteria), 1)
        self.assertTrue(req.querySpec.HasField("sampleStatusSelector"))

    def test_params_default_filter_is_none(self):
        p = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"))
        self.assertIsNone(p.sample_status_filter)

    def test_default_constructed_selector_is_rejected(self):
        # The helpers make MODE_UNSPECIFIED unreachable, but the parameter accepts any SampleStatusSelector.  A
        # default-constructed one would set the field present-but-invalid and be rejected by the server, so catch
        # it here where the error can name the problem and point at the helpers.
        with self.assertRaises(ValueError) as ctx:
            QueryParams(
                BEGIN,
                END,
                pv_selector=PvQuery.pattern("x"),
                sample_status_filter=query_pb2.SampleStatusSelector(),
            )
        self.assertIn("domain", str(ctx.exception))

    def test_selector_with_domain_but_unspecified_mode_is_rejected(self):
        selector = query_pb2.SampleStatusSelector()
        selector.domain = "data_quality"

        with self.assertRaises(ValueError) as ctx:
            QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"), sample_status_filter=selector)
        self.assertIn("MODE_UNSPECIFIED", str(ctx.exception))

    def test_helper_built_selectors_pass_validation(self):
        for selector in (SampleStatusFilter.include("d"), SampleStatusFilter.exclude("d")):
            p = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"), sample_status_filter=selector)
            self.assertIs(p.sample_status_filter, selector)


if __name__ == "__main__":
    unittest.main()


# ----------------------------------------------------------------------
# queryBuckets / queryBucketsStream (#16)
# ----------------------------------------------------------------------


def _bucket_response(pv_names=(), next_page_token=""):
    """Build a real QueryBucketsResponse carrying one empty-bodied DataBucket per PV name and a nextPageToken."""
    response = query_pb2.QueryBucketsResponse()
    for pv_name in pv_names:
        response.bucketQueryResult.dataBuckets.add(pvName=pv_name)
    response.bucketQueryResult.nextPageToken = next_page_token
    return response


def _bucket_exceptional_response(message):
    """Build a real QueryBucketsResponse carrying an exceptionalResult with the given message."""
    response = query_pb2.QueryBucketsResponse()
    response.exceptionalResult.message = message
    return response


class TestBuildQueryBucketsRequest(unittest.TestCase):
    def setUp(self):
        self.client = QueryClient(Mock())

    def test_spec_matches_the_samples_builder(self):
        params = QueryParams(
            BEGIN,
            END,
            pv_selector=PvQuery.name_list(["A", "B"]),
            config_criteria=[ConfigQuery.configuration_name(["cfg"])],
            limit=50,
        )
        buckets = self.client._build_query_buckets_request(params, page_token="tok")
        samples = self.client._build_query_samples_request(params, page_token="tok")
        self.assertIsInstance(buckets, query_pb2.QueryBucketsRequest)
        self.assertEqual(buckets.querySpec, samples.querySpec)
        self.assertEqual(buckets.executionOptions.limit, 50)
        self.assertEqual(buckets.executionOptions.pageToken, "tok")

    def test_use_serialized_columns_is_forced_false(self):
        params = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"))
        request = self.client._build_query_buckets_request(params)
        self.assertFalse(request.resultRepresentation.useSerializedColumns)

    def test_exclude_column_metadata_is_passed_through(self):
        for exclude in (False, True):
            with self.subTest(exclude=exclude):
                params = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"), exclude_column_metadata=exclude)
                request = self.client._build_query_buckets_request(params)
                self.assertEqual(request.resultRepresentation.excludeColumnMetadata, exclude)

    def test_no_limit_no_token(self):
        request = self.client._build_query_buckets_request(QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x")))
        self.assertEqual(request.executionOptions.limit, 0)
        self.assertEqual(request.executionOptions.pageToken, "")

    def test_sample_status_filter_is_refused(self):
        params = QueryParams(
            BEGIN, END, pv_selector=PvQuery.pattern("x"), sample_status_filter=SampleStatusFilter.exclude("dq")
        )
        with self.assertRaises(ValueError) as ctx:
            self.client._build_query_buckets_request(params)
        self.assertIn("sample_status_filter", str(ctx.exception))
        self.assertIn("query_samples()", str(ctx.exception))

    def test_refusal_happens_before_any_rpc(self):
        stub = Mock()
        self.client._stub = stub
        params = QueryParams(
            BEGIN, END, pv_selector=PvQuery.pattern("x"), sample_status_filter=SampleStatusFilter.include("dq")
        )
        with self.assertRaises(ValueError):
            self.client.query_buckets(params)
        with self.assertRaises(ValueError):
            list(self.client.iter_query_buckets(params))
        with self.assertRaises(ValueError):
            list(self.client.iter_query_buckets_stream(params))
        stub.queryBuckets.assert_not_called()
        stub.queryBucketsStream.assert_not_called()


class TestQueryBucketsUnary(unittest.TestCase):
    def setUp(self):
        self.client = QueryClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.params = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"), limit=7)

    def test_success(self):
        self.mock_stub.queryBuckets.return_value = _bucket_response(["A", "B"], next_page_token="tok")
        result = self.client.query_buckets(self.params, page_token="prev")
        self.assertFalse(result.result_status.is_error)
        self.assertEqual([b.pvName for b in result.data_buckets], ["A", "B"])
        self.assertIsInstance(result.data_buckets[0], common_pb2.DataBucket)
        self.assertEqual(result.next_page_token, "tok")
        sent = self.mock_stub.queryBuckets.call_args[0][0]
        self.assertEqual(sent.executionOptions.pageToken, "prev")
        self.assertEqual(sent.executionOptions.limit, 7)

    def test_empty_result_is_success(self):
        self.mock_stub.queryBuckets.return_value = _bucket_response([])
        result = self.client.query_buckets(self.params)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.data_buckets, [])
        self.assertEqual(result.next_page_token, "")

    def test_business_error(self):
        self.mock_stub.queryBuckets.return_value = _bucket_exceptional_response("bad query")
        result = self.client.query_buckets(self.params)
        self.assertTrue(result.result_status.is_error)
        self.assertEqual(result.result_status.message, "bad query")
        self.assertEqual(result.data_buckets, [])
        self.assertEqual(result.next_page_token, "")

    def test_unexpected_response_format(self):
        self.mock_stub.queryBuckets.return_value = _response_with_field("somethingElse")
        result = self.client.query_buckets(self.params)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("neither exceptionalResult nor bucketQueryResult", result.result_status.message)

    def test_grpc_error(self):
        err = grpc.RpcError()
        err.details = lambda: "connection refused"
        self.mock_stub.queryBuckets.side_effect = err
        result = self.client.query_buckets(self.params)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: connection refused", result.result_status.message)

    def test_unexpected_exception(self):
        self.mock_stub.queryBuckets.side_effect = ValueError("boom")
        result = self.client.query_buckets(self.params)
        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestIterQueryBuckets(unittest.TestCase):
    def setUp(self):
        self.client = QueryClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.params = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"))

    def test_paging_follows_tokens_and_stops_on_empty(self):
        self.mock_stub.queryBuckets.side_effect = [
            _bucket_response(["A"], next_page_token="t1"),
            _bucket_response(["A"], next_page_token="t2"),
            _bucket_response(["B"], next_page_token=""),
        ]
        pages = list(self.client.iter_query_buckets(self.params))
        self.assertEqual([[b.pvName for b in p.data_buckets] for p in pages], [["A"], ["A"], ["B"]])
        sent_tokens = [c[0][0].executionOptions.pageToken for c in self.mock_stub.queryBuckets.call_args_list]
        self.assertEqual(sent_tokens, ["", "t1", "t2"])

    def test_error_page_raises_runtime_error(self):
        self.mock_stub.queryBuckets.side_effect = [
            _bucket_response(["A"], next_page_token="t1"),
            _bucket_exceptional_response("page boom"),
        ]
        collected = []
        with self.assertRaises(RuntimeError) as ctx:
            for page in self.client.iter_query_buckets(self.params):
                collected.append(page)
        self.assertEqual(len(collected), 1)
        self.assertIn("queryBuckets failed during paging: page boom", str(ctx.exception))


class TestQueryBucketsStream(unittest.TestCase):
    def setUp(self):
        self.client = QueryClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.params = QueryParams(BEGIN, END, pv_selector=PvQuery.pattern("x"))

    def test_stream_yields_messages(self):
        self.mock_stub.queryBucketsStream.return_value = iter([_bucket_response(["A"]), _bucket_response(["B"])])
        results = list(self.client.iter_query_buckets_stream(self.params))
        self.assertTrue(all(isinstance(r, QueryBucketsApiResult) for r in results))
        self.assertEqual([[b.pvName for b in r.data_buckets] for r in results], [["A"], ["B"]])

    def test_stream_never_sends_page_token(self):
        self.mock_stub.queryBucketsStream.return_value = iter([_bucket_response()])
        list(self.client.iter_query_buckets_stream(self.params))
        sent = self.mock_stub.queryBucketsStream.call_args[0][0]
        self.assertIsInstance(sent, query_pb2.QueryBucketsRequest)
        self.assertEqual(sent.executionOptions.pageToken, "")

    def test_empty_result_is_one_empty_message(self):
        self.mock_stub.queryBucketsStream.return_value = iter([_bucket_response([])])
        results = list(self.client.iter_query_buckets_stream(self.params))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].data_buckets, [])

    def test_business_error_mid_stream_raises(self):
        self.mock_stub.queryBucketsStream.return_value = iter(
            [_bucket_response(["A"]), _bucket_exceptional_response("single bucket for pv A exceeds the limit")]
        )
        collected = []
        with self.assertRaises(RuntimeError) as ctx:
            for r in self.client.iter_query_buckets_stream(self.params):
                collected.append(r)
        self.assertEqual(len(collected), 1)
        self.assertIn("queryBucketsStream failed during streaming", str(ctx.exception))
        self.assertIn("exceeds the limit", str(ctx.exception))

    def test_grpc_error_mid_stream_raises(self):
        err = grpc.RpcError()
        err.details = lambda: "stream reset"

        def exploding_stream():
            yield _bucket_response(["A"])
            raise err

        self.mock_stub.queryBucketsStream.return_value = exploding_stream()
        with self.assertRaises(RuntimeError) as ctx:
            list(self.client.iter_query_buckets_stream(self.params))
        self.assertIn("gRPC error: stream reset", str(ctx.exception))

    def test_unrecognized_message_raises(self):
        self.mock_stub.queryBucketsStream.return_value = iter([_response_with_field("somethingElse")])
        with self.assertRaises(RuntimeError) as ctx:
            list(self.client.iter_query_buckets_stream(self.params))
        self.assertIn("neither exceptionalResult nor bucketQueryResult", str(ctx.exception))


class TestSharedStreamSender(unittest.TestCase):
    """The samples stream sender now goes through _iter_stream(); its yielded error messages are unchanged."""

    def setUp(self):
        self.client = QueryClient(Mock())
        self.mock_stub = Mock()
        self.client._stub = self.mock_stub
        self.request = query_pb2.QuerySamplesRequest()

    def test_samples_error_texts(self):
        err = grpc.RpcError()
        err.details = lambda: "reset"

        def stream():
            yield _exceptional_response("biz")
            yield _response_with_field("somethingElse")
            raise err

        self.mock_stub.querySamplesStream.return_value = stream()
        messages = [r.result_status.message for r in self.client._send_query_samples_stream(self.request)]
        self.assertEqual(
            messages,
            [
                "biz",
                "Unexpected response format: neither exceptionalResult nor sampleQueryResult found",
                "gRPC error: reset",
            ],
        )

    def test_unexpected_exception_text(self):
        self.mock_stub.querySamplesStream.side_effect = ValueError("boom")
        results = list(self.client._send_query_samples_stream(self.request))
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], QuerySamplesApiResult)
        self.assertEqual(results[0].result_status.message, "Unexpected error: boom")
