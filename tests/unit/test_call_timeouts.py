"""
#81 T3: every public unary and paging method takes a keyword-only ``timeout`` and hands it to its stub call.

The keyword was added to 40 methods by one mechanical change, so this checks each of them rather than a sample: a
method whose sender drops the keyword would otherwise silently run with the client default.  The per-client test
modules keep pinning the default's propagation through their existing stub-call assertions.
"""

import inspect
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client import data_frame as dfb
from dp_python_lib.client.annotations_client import AnnotationsClient, SaveAnnotationRequestParams
from dp_python_lib.client.dataset_client import DataSetClient, SaveDataSetRequestParams, data_block
from dp_python_lib.client.export_client import (
    DEFAULT_EXPORT_TIMEOUT_SECONDS,
    ExportClient,
    ExportDataRequestParams,
    ExportFormat,
)
from dp_python_lib.client.ingestion_client import (
    IngestDataRequestParams,
    IngestionClient,
    RegisterProviderRequestParams,
)
from dp_python_lib.client.ingestion_client import RequestStatusQuery as RS
from dp_python_lib.client.machine_config_client import (
    MachineConfigClient,
    SaveConfigurationActivationRequestParams,
    SaveConfigurationRequestParams,
)
from dp_python_lib.client.pv_metadata_client import PvMetadataClient, SavePvMetadataRequestParams
from dp_python_lib.client.query_client import PvQuery, QueryClient, QueryParams
from dp_python_lib.client.sample_status_client import (
    QuerySampleStatusesRequestParams,
    SampleStatusClient,
    SampleStatusColumn,
    SampleStatusFrame,
    SaveSampleStatusesRequestParams,
    timestamp_list,
)
from dp_python_lib.client.service_api_client_base import DEFAULT, DEFAULT_CALL_TIMEOUT_SECONDS

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)


def _query_params():
    return QueryParams(begin_time=T0, end_time=T1, pv_selector=PvQuery.name_list(["PV:1"]))


def _frame():
    return dfb.data_frame(dfb.timestamp_list([T0]), [dfb.double_column("PV:1", [1.0])])


def _status_params():
    frame = SampleStatusFrame("dq", "op", timestamp_list([T0]), [SampleStatusColumn("PV:1", [1])])
    return SaveSampleStatusesRequestParams(frames=[frame])


# (client class, public method, positional args, kwargs, stub method, paging).  A paging method raises RuntimeError
# on the error page a bare Mock response produces, which is fine: only the stub call is checked.
CASES = [
    (PvMetadataClient, "save_pv_metadata", lambda: (SavePvMetadataRequestParams("PV:1"),), "savePvMetadata", False),
    (PvMetadataClient, "get_pv_metadata", lambda: ("PV:1",), "getPvMetadata", False),
    (PvMetadataClient, "query_pv_metadata", lambda: (), "queryPvMetadata", False),
    (PvMetadataClient, "iter_pv_metadata", lambda: (), "queryPvMetadata", True),
    (PvMetadataClient, "delete_pv_metadata", lambda: ("PV:1",), "deletePvMetadata", False),
    (
        MachineConfigClient,
        "save_configuration",
        lambda: (SaveConfigurationRequestParams("cfg"),),
        "saveConfiguration",
        False,
    ),
    (MachineConfigClient, "get_configuration", lambda: ("cfg",), "getConfiguration", False),
    (MachineConfigClient, "query_configurations", lambda: (), "queryConfigurations", False),
    (MachineConfigClient, "iter_configurations", lambda: (), "queryConfigurations", True),
    (MachineConfigClient, "delete_configuration", lambda: ("cfg",), "deleteConfiguration", False),
    (
        MachineConfigClient,
        "save_configuration_activation",
        lambda: (SaveConfigurationActivationRequestParams("cfg", T0),),
        "saveConfigurationActivation",
        False,
    ),
    (MachineConfigClient, "get_configuration_activation", lambda: ("act",), "getConfigurationActivation", False),
    (MachineConfigClient, "query_configuration_activations", lambda: (), "queryConfigurationActivations", False),
    (MachineConfigClient, "iter_configuration_activations", lambda: (), "queryConfigurationActivations", True),
    (
        MachineConfigClient,
        "delete_configuration_activation",
        lambda: ("act",),
        "deleteConfigurationActivation",
        False,
    ),
    (MachineConfigClient, "get_active_configurations", lambda: (), "getActiveConfigurations", False),
    (SampleStatusClient, "save_sample_statuses", lambda: (_status_params(),), "saveSampleStatuses", False),
    (
        SampleStatusClient,
        "query_sample_statuses",
        lambda: (QuerySampleStatusesRequestParams(T0, T1),),
        "querySampleStatuses",
        False,
    ),
    (
        SampleStatusClient,
        "iter_sample_statuses",
        lambda: (QuerySampleStatusesRequestParams(T0, T1),),
        "querySampleStatuses",
        True,
    ),
    (
        SampleStatusClient,
        "delete_sample_statuses",
        lambda: (T0, T1, "dq", "op", ["PV:1"]),
        "deleteSampleStatuses",
        False,
    ),
    (
        DataSetClient,
        "save_dataset",
        lambda: (SaveDataSetRequestParams("ds", "me", [data_block(T0, T1, ["PV:1"])]),),
        "saveDataSet",
        False,
    ),
    (DataSetClient, "get_dataset", lambda: ("id",), "getDataSet", False),
    (DataSetClient, "query_datasets", lambda: (), "queryDataSets", False),
    (DataSetClient, "iter_datasets", lambda: (), "queryDataSets", True),
    (DataSetClient, "get_datasets", lambda: (["id"],), "queryDataSets", True),
    (DataSetClient, "delete_dataset", lambda: ("id",), "deleteDataSet", False),
    (
        AnnotationsClient,
        "save_annotation",
        lambda: (SaveAnnotationRequestParams("a", "me", ["ds"]),),
        "saveAnnotation",
        False,
    ),
    (AnnotationsClient, "get_annotation", lambda: ("id",), "getAnnotation", False),
    (AnnotationsClient, "query_annotations", lambda: (), "queryAnnotations", False),
    (AnnotationsClient, "iter_annotations", lambda: (), "queryAnnotations", True),
    (AnnotationsClient, "delete_annotation", lambda: ("id",), "deleteAnnotation", False),
    (AnnotationsClient, "get_calculations", lambda: ("id",), "getCalculations", False),
    (
        ExportClient,
        "export_data",
        lambda: (ExportDataRequestParams(ExportFormat.CSV, dataset_id="ds"),),
        "exportData",
        False,
    ),
    (QueryClient, "query_samples", lambda: (_query_params(),), "querySamples", False),
    (QueryClient, "iter_query_samples", lambda: (_query_params(),), "querySamples", True),
    (QueryClient, "query_buckets", lambda: (_query_params(),), "queryBuckets", False),
    (QueryClient, "iter_query_buckets", lambda: (_query_params(),), "queryBuckets", True),
    (
        IngestionClient,
        "register_provider",
        lambda: (RegisterProviderRequestParams("p", None, None, None),),
        "registerProvider",
        False,
    ),
    (IngestionClient, "ingest_data", lambda: (IngestDataRequestParams("p", _frame()),), "ingestData", False),
    (IngestionClient, "query_request_status", lambda: ([RS.provider_id("p")],), "queryRequestStatus", False),
]


def _client(cls):
    client = cls(Mock())
    client._stub = Mock()
    return client


def _call(client, method, args, **kwargs):
    try:
        result = getattr(client, method)(*args, **kwargs)
        if inspect.isgenerator(result):
            list(result)
    except RuntimeError:
        pass  # a paging method's error page; the stub call already happened


class TestCallTimeouts(unittest.TestCase):
    def test_case_list_covers_forty_methods(self):
        self.assertEqual(40, len({(cls, m) for cls, m, *_ in CASES}))

    def test_timeout_is_keyword_only_and_defaults_to_the_sentinel(self):
        for cls, method, *_ in CASES:
            with self.subTest(method=f"{cls.__name__}.{method}"):
                param = inspect.signature(getattr(cls, method)).parameters["timeout"]
                self.assertIs(inspect.Parameter.KEYWORD_ONLY, param.kind)
                self.assertIs(DEFAULT, param.default)

    def test_per_call_timeout_reaches_the_stub(self):
        for cls, method, args, stub_method, _paging in CASES:
            with self.subTest(method=f"{cls.__name__}.{method}"):
                client = _client(cls)
                _call(client, method, args(), timeout=7)
                self.assertEqual(7, getattr(client._stub, stub_method).call_args.kwargs.get("timeout"))

    def test_none_sends_no_timeout(self):
        for cls, method, args, stub_method, _paging in CASES:
            with self.subTest(method=f"{cls.__name__}.{method}"):
                client = _client(cls)
                _call(client, method, args(), timeout=None)
                self.assertNotIn("timeout", getattr(client._stub, stub_method).call_args.kwargs)

    def test_omitted_timeout_sends_the_client_default(self):
        for cls, method, args, stub_method, _paging in CASES:
            with self.subTest(method=f"{cls.__name__}.{method}"):
                client = _client(cls)
                _call(client, method, args())
                expected = DEFAULT_EXPORT_TIMEOUT_SECONDS if cls is ExportClient else DEFAULT_CALL_TIMEOUT_SECONDS
                self.assertEqual(expected, getattr(client._stub, stub_method).call_args.kwargs.get("timeout"))

    def test_zero_timeout_raises_before_any_call(self):
        for cls, method, args, stub_method, _paging in CASES:
            with self.subTest(method=f"{cls.__name__}.{method}"):
                client = _client(cls)
                with self.assertRaisesRegex(ValueError, "timeout=None"):
                    result = getattr(client, method)(*args(), timeout=0)
                    if inspect.isgenerator(result):
                        list(result)
                getattr(client._stub, stub_method).assert_not_called()


# (client class, public stream method, positional args, stub method).
STREAM_CASES = [
    (QueryClient, "iter_query_samples_stream", lambda: (_query_params(),), "querySamplesStream"),
    (QueryClient, "iter_query_buckets_stream", lambda: (_query_params(),), "queryBucketsStream"),
    (
        SampleStatusClient,
        "iter_sample_statuses_stream",
        lambda: (QuerySampleStatusesRequestParams(T0, T1),),
        "querySampleStatusesStream",
    ),
]


class TestStreamTimeouts(unittest.TestCase):
    """Stream methods: idle_timeout plus an optional total timeout that defaults to none (#81 D2/D3)."""

    def _stub_kwargs(self, cls, method, args, stub_method, **kwargs):
        client = _client(cls)
        client._stub = Mock()
        getattr(client._stub, stub_method).return_value = iter([])
        list(getattr(client, method)(*args(), **kwargs))
        return getattr(client._stub, stub_method).call_args.kwargs

    def test_both_keywords_are_keyword_only_and_default_to_the_sentinel(self):
        for cls, method, *_ in STREAM_CASES:
            for name in ("timeout", "idle_timeout"):
                with self.subTest(method=method, param=name):
                    param = inspect.signature(getattr(cls, method)).parameters[name]
                    self.assertIs(inspect.Parameter.KEYWORD_ONLY, param.kind)
                    self.assertIs(DEFAULT, param.default)

    def test_omitted_timeout_sets_no_total_deadline(self):
        for cls, method, args, stub_method in STREAM_CASES:
            with self.subTest(method=method):
                self.assertNotIn("timeout", self._stub_kwargs(cls, method, args, stub_method))

    def test_explicit_timeout_is_the_total_deadline(self):
        for cls, method, args, stub_method in STREAM_CASES:
            with self.subTest(method=method):
                self.assertEqual(9, self._stub_kwargs(cls, method, args, stub_method, timeout=9)["timeout"])

    def test_zero_or_negative_raises_on_first_next(self):
        for cls, method, args, stub_method in STREAM_CASES:
            for kwargs in ({"timeout": 0}, {"idle_timeout": 0}, {"idle_timeout": -1}):
                with self.subTest(method=method, kwargs=kwargs):
                    client = _client(cls)
                    with self.assertRaisesRegex(ValueError, "=None to disable"):
                        list(getattr(client, method)(*args(), **kwargs))
                    getattr(client._stub, stub_method).assert_not_called()


class TestAllFortyFiveMethods(unittest.TestCase):
    """The plan's count (#81 D2, T3): 40 unary/paging methods take timeout, 5 stream methods take both keywords."""

    INGESTION_STREAMS = ("ingest_data_stream", "iter_ingest_data_bidi_stream")

    def test_forty_five(self):
        methods = {(cls, m) for cls, m, *_ in CASES} | {(cls, m) for cls, m, *_ in STREAM_CASES}
        methods |= {(IngestionClient, m) for m in self.INGESTION_STREAMS}
        self.assertEqual(45, len(methods))

    def test_ingestion_streams_take_both_keywords(self):
        for method in self.INGESTION_STREAMS:
            for name in ("timeout", "idle_timeout"):
                with self.subTest(method=method, param=name):
                    param = inspect.signature(getattr(IngestionClient, method)).parameters[name]
                    self.assertIs(inspect.Parameter.KEYWORD_ONLY, param.kind)
                    self.assertIs(DEFAULT, param.default)

    def test_await_request_statuses_keeps_its_own_total_timeout(self):
        # R3: its timeout is the total wait (default 30 s), not the D2 per-call keyword.
        self.assertEqual(30.0, inspect.signature(IngestionClient.await_request_statuses).parameters["timeout"].default)


class TestExportTimeout(unittest.TestCase):
    def _export(self, client, **kwargs):
        return client.export_data(ExportDataRequestParams(ExportFormat.CSV, dataset_id="ds"), **kwargs)

    def test_export_timeout_constructor_argument_sets_the_default(self):
        client = ExportClient(Mock(), export_timeout=60)
        client._stub = Mock()
        self._export(client)
        self.assertEqual(60, client._stub.exportData.call_args.kwargs["timeout"])

    def test_export_timeout_zero_disables_it(self):
        client = ExportClient(Mock(), export_timeout=0)
        client._stub = Mock()
        self._export(client)
        self.assertNotIn("timeout", client._stub.exportData.call_args.kwargs)

    def test_export_deadline_hint_names_export_timeout(self):
        import grpc

        error = grpc.RpcError()
        error.details = Mock(return_value="Deadline Exceeded")
        error.code = Mock(return_value=grpc.StatusCode.DEADLINE_EXCEEDED)
        client = ExportClient(Mock())
        client._stub = Mock()
        client._stub.exportData.side_effect = error
        result = self._export(client)
        self.assertIn(
            "(client deadline 3600 s, the library default; construct the client with export_timeout= to change it;",
            result.result_status.message,
        )


if __name__ == "__main__":
    unittest.main()
