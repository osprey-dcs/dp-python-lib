import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

import grpc

# Add src directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from dp_python_lib.client.dataset_client import data_block
from dp_python_lib.client.export_client import (
    ExportClient,
    ExportDataApiResult,
    ExportDataRequestParams,
    ExportFormat,
    calculations_spec,
)
from dp_python_lib.grpc import annotation_pb2

BEGIN = datetime(2026, 7, 14, 18, tzinfo=timezone.utc)
END = datetime(2026, 7, 14, 19, tzinfo=timezone.utc)

_FORMAT = annotation_pb2.ExportDataRequest.ExportOutputFormat


def _response_with_field(field_name):
    """
    Build a Mock response whose HasField(field) returns True only for field_name.  This keeps both the _send_*
    oneof check and the *ApiResult property accessors (which also call HasField) consistent.
    """
    response = Mock()
    response.HasField = Mock(side_effect=lambda field: field == field_name)
    return response


class TestExportFormat(unittest.TestCase):
    """Unit tests for the ExportFormat enum."""

    def test_maps_to_proto_values(self):
        self.assertEqual(ExportFormat.HDF5.to_proto(), _FORMAT.EXPORT_FORMAT_HDF5)
        self.assertEqual(ExportFormat.CSV.to_proto(), _FORMAT.EXPORT_FORMAT_CSV)
        self.assertEqual(ExportFormat.XLSX.to_proto(), _FORMAT.EXPORT_FORMAT_XLSX)

    def test_constructible_from_string_value(self):
        self.assertIs(ExportFormat("csv"), ExportFormat.CSV)
        self.assertIs(ExportFormat("hdf5"), ExportFormat.HDF5)
        self.assertIs(ExportFormat("xlsx"), ExportFormat.XLSX)

    def test_compares_equal_to_its_string(self):
        self.assertEqual(ExportFormat.CSV, "csv")

    def test_unspecified_is_unreachable(self):
        # EXPORT_FORMAT_UNSPECIFIED is rejected server-side; it must have no enum member here.
        self.assertNotIn("EXPORT_FORMAT_UNSPECIFIED", {member.name for member in ExportFormat})
        self.assertNotIn(_FORMAT.EXPORT_FORMAT_UNSPECIFIED, {member.to_proto() for member in ExportFormat})


class TestCalculationsSpecBuilder(unittest.TestCase):
    """Unit tests for the calculations_spec() builder."""

    def test_id_only_means_all_frames_and_columns(self):
        spec = calculations_spec("calc-1")
        self.assertEqual(spec.calculationsId, "calc-1")
        self.assertEqual(len(spec.dataFrameColumns), 0)

    def test_with_frame_columns(self):
        spec = calculations_spec("calc-1", {"f1": ["x_rms", "y_rms"], "f2": ["z"]})
        self.assertEqual(spec.calculationsId, "calc-1")
        self.assertEqual(set(spec.dataFrameColumns), {"f1", "f2"})
        self.assertEqual(list(spec.dataFrameColumns["f1"].columnNames), ["x_rms", "y_rms"])
        self.assertEqual(list(spec.dataFrameColumns["f2"].columnNames), ["z"])

    def test_rejects_empty_calculations_id(self):
        with self.assertRaises(ValueError) as ctx:
            calculations_spec("")
        self.assertIn("calculations_id", str(ctx.exception))

    def test_rejects_empty_frame_name(self):
        with self.assertRaises(ValueError) as ctx:
            calculations_spec("calc-1", {"": ["x"]})
        self.assertIn("non-empty name", str(ctx.exception))

    def test_rejects_empty_column_name_list(self):
        # The server rejects an empty list, so name the alternatives here rather than let it bounce.
        with self.assertRaises(ValueError) as ctx:
            calculations_spec("calc-1", {"f1": []})
        self.assertIn("f1", str(ctx.exception))


class TestExportDataRequestParams(unittest.TestCase):
    """Unit tests for the params class's validation and format coercion."""

    def setUp(self):
        self.block = data_block(BEGIN, END, ["A:1"])

    def test_accepts_enum_member(self):
        params = ExportDataRequestParams(ExportFormat.HDF5, dataset_id="ds-1")
        self.assertIs(params.output_format, ExportFormat.HDF5)

    def test_coerces_bare_string(self):
        params = ExportDataRequestParams("csv", dataset_id="ds-1")
        self.assertIs(params.output_format, ExportFormat.CSV)

    def test_rejects_unknown_format(self):
        with self.assertRaises(ValueError) as ctx:
            ExportDataRequestParams("parquet", dataset_id="ds-1")
        message = str(ctx.exception)
        self.assertIn("parquet", message)
        self.assertIn("'csv'", message)
        self.assertIn("'hdf5'", message)
        self.assertIn("'xlsx'", message)

    def test_rejects_wrong_case_format(self):
        # The enum values are lowercase; "CSV" is not a member.
        with self.assertRaises(ValueError):
            ExportDataRequestParams("CSV", dataset_id="ds-1")

    def test_rejects_no_data_source(self):
        with self.assertRaises(ValueError) as ctx:
            ExportDataRequestParams(ExportFormat.CSV)
        self.assertIn("at least one data source", str(ctx.exception))

    def test_accepts_each_source_alone(self):
        ExportDataRequestParams(ExportFormat.CSV, dataset_id="ds-1")
        ExportDataRequestParams(ExportFormat.CSV, data_blocks=[self.block])
        ExportDataRequestParams(ExportFormat.CSV, calculations_spec=calculations_spec("calc-1"))

    def test_accepts_merged_sources(self):
        params = ExportDataRequestParams(
            ExportFormat.HDF5,
            dataset_id="ds-1",
            data_blocks=[self.block],
            calculations_spec=calculations_spec("calc-1"),
        )
        self.assertEqual(params.dataset_id, "ds-1")
        self.assertEqual(len(params.data_blocks), 1)
        self.assertEqual(params.calculations_spec.calculationsId, "calc-1")

    def test_empty_data_blocks_list_is_not_a_source(self):
        with self.assertRaises(ValueError):
            ExportDataRequestParams(ExportFormat.CSV, data_blocks=[])


class TestExportClientBuildRequest(unittest.TestCase):
    """Unit tests for the request-building helper (no gRPC calls)."""

    def setUp(self):
        self.client = ExportClient(Mock())
        self.block = data_block(BEGIN, END, ["A:1"])

    def test_build_request_all_sources(self):
        params = ExportDataRequestParams(
            ExportFormat.HDF5,
            dataset_id="ds-1",
            data_blocks=[self.block],
            calculations_spec=calculations_spec("calc-1", {"f1": ["x_rms"]}),
        )
        request = self.client._build_export_data_request(params)

        self.assertEqual(request.outputFormat, _FORMAT.EXPORT_FORMAT_HDF5)
        self.assertEqual(request.dataSetId, "ds-1")
        self.assertEqual(len(request.dataBlocks), 1)
        self.assertEqual(list(request.dataBlocks[0].pvNames), ["A:1"])
        self.assertTrue(request.HasField("calculationsSpec"))
        self.assertEqual(request.calculationsSpec.calculationsId, "calc-1")
        self.assertEqual(list(request.calculationsSpec.dataFrameColumns["f1"].columnNames), ["x_rms"])

    def test_build_request_dataset_only(self):
        params = ExportDataRequestParams(ExportFormat.CSV, dataset_id="ds-1")
        request = self.client._build_export_data_request(params)

        self.assertEqual(request.outputFormat, _FORMAT.EXPORT_FORMAT_CSV)
        self.assertEqual(request.dataSetId, "ds-1")
        self.assertEqual(len(request.dataBlocks), 0)
        self.assertFalse(request.HasField("calculationsSpec"))

    def test_build_request_calculations_only(self):
        # A calculations-only export is legal and needs no ingested time-series data.
        params = ExportDataRequestParams(ExportFormat.CSV, calculations_spec=calculations_spec("calc-1"))
        request = self.client._build_export_data_request(params)

        self.assertEqual(request.dataSetId, "")
        self.assertEqual(len(request.dataBlocks), 0)
        self.assertTrue(request.HasField("calculationsSpec"))

    def test_build_request_blocks_only(self):
        params = ExportDataRequestParams(ExportFormat.XLSX, data_blocks=[self.block])
        request = self.client._build_export_data_request(params)

        self.assertEqual(request.outputFormat, _FORMAT.EXPORT_FORMAT_XLSX)
        self.assertEqual(request.dataSetId, "")
        self.assertEqual(len(request.dataBlocks), 1)
        self.assertFalse(request.HasField("calculationsSpec"))


class TestSendExportData(unittest.TestCase):
    def setUp(self):
        self.client = ExportClient(Mock())
        self.request = annotation_pb2.ExportDataRequest(dataSetId="ds-1", outputFormat=_FORMAT.EXPORT_FORMAT_CSV)

    def test_success(self):
        response = _response_with_field("exportDataResult")
        response.exportDataResult.filePath = "/srv/exports/out.csv"
        response.exportDataResult.fileUrl = "https://example.test/exports/out.csv"
        mock_stub = Mock()
        mock_stub.exportData.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_export_data(self.request)

        self.assertIsInstance(result, ExportDataApiResult)
        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.file_path, "/srv/exports/out.csv")
        self.assertEqual(result.file_url, "https://example.test/exports/out.csv")
        mock_stub.exportData.assert_called_once_with(self.request)

    def test_empty_file_url_is_normal(self):
        # An empty fileUrl means the deployment does not publish over HTTP -- not a failure.
        response = _response_with_field("exportDataResult")
        response.exportDataResult.filePath = "/srv/exports/out.csv"
        response.exportDataResult.fileUrl = ""
        mock_stub = Mock()
        mock_stub.exportData.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_export_data(self.request)

        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.file_url, "")
        self.assertIsNotNone(result.file_url)

    def test_exceptional_result(self):
        response = _response_with_field("exceptionalResult")
        response.exceptionalResult.message = "non-scalar column cannot be exported as CSV"
        mock_stub = Mock()
        mock_stub.exportData.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_export_data(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("non-scalar column", result.result_status.message)
        self.assertIsNone(result.file_path)
        self.assertIsNone(result.file_url)

    def test_unexpected_response(self):
        response = Mock()
        response.HasField = Mock(return_value=False)
        mock_stub = Mock()
        mock_stub.exportData.return_value = response
        self.client._stub = mock_stub

        result = self.client._send_export_data(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected response format", result.result_status.message)

    def test_grpc_error(self):
        mock_stub = Mock()
        err = grpc.RpcError()
        err.details = Mock(return_value="Connection timeout")
        mock_stub.exportData.side_effect = err
        self.client._stub = mock_stub

        result = self.client._send_export_data(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("gRPC error: Connection timeout", result.result_status.message)

    def test_general_exception(self):
        mock_stub = Mock()
        mock_stub.exportData.side_effect = ValueError("boom")
        self.client._stub = mock_stub

        result = self.client._send_export_data(self.request)

        self.assertTrue(result.result_status.is_error)
        self.assertIn("Unexpected error: boom", result.result_status.message)


class TestExportDataUserFacing(unittest.TestCase):
    """The user-facing method threads params through the builder and sender."""

    def test_export_data_end_to_end(self):
        client = ExportClient(Mock())
        response = _response_with_field("exportDataResult")
        response.exportDataResult.filePath = "/srv/exports/out.h5"
        response.exportDataResult.fileUrl = ""
        mock_stub = Mock()
        mock_stub.exportData.return_value = response
        client._stub = mock_stub

        result = client.export_data(ExportDataRequestParams(ExportFormat.HDF5, dataset_id="ds-1"))

        self.assertFalse(result.result_status.is_error)
        self.assertEqual(result.file_path, "/srv/exports/out.h5")
        sent = mock_stub.exportData.call_args_list[0].args[0]
        self.assertEqual(sent.dataSetId, "ds-1")
        self.assertEqual(sent.outputFormat, _FORMAT.EXPORT_FORMAT_HDF5)


if __name__ == "__main__":
    unittest.main()
