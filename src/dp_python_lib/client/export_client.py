import logging
from enum import Enum

import grpc

from dp_python_lib.client.result import ApiResultBase
from dp_python_lib.client.service_api_client_base import ServiceApiClientBase
from dp_python_lib.grpc import annotation_pb2, annotation_pb2_grpc, common_pb2


class ExportFormat(str, Enum):
    """
    Output file formats accepted by the exportData() API method.

    A str enum, so ExportFormat.CSV and ExportFormat("csv") are both valid and the member compares equal to its
    string value.  The proto's EXPORT_FORMAT_UNSPECIFIED zero value is deliberately absent: the server rejects it,
    so making it unreachable through this enum turns a would-be server rejection into a client-side ValueError
    naming the valid formats.

    Note the tabular formats can only represent scalar columns: exporting data containing array, image, or struct
    columns as CSV or XLSX is rejected server-side.  Use HDF5 for those.
    """

    HDF5 = "hdf5"
    CSV = "csv"
    XLSX = "xlsx"

    def to_proto(self) -> "annotation_pb2.ExportDataRequest.ExportOutputFormat":
        """
        Converts this format into its protobuf enum value.
        :return: The corresponding ExportDataRequest.ExportOutputFormat value.
        """
        return _EXPORT_FORMAT_TO_PROTO[self]


_EXPORT_FORMAT_TO_PROTO = {
    ExportFormat.HDF5: annotation_pb2.ExportDataRequest.ExportOutputFormat.EXPORT_FORMAT_HDF5,
    ExportFormat.CSV: annotation_pb2.ExportDataRequest.ExportOutputFormat.EXPORT_FORMAT_CSV,
    ExportFormat.XLSX: annotation_pb2.ExportDataRequest.ExportOutputFormat.EXPORT_FORMAT_XLSX,
}


def calculations_spec(
    calculations_id: str,
    frame_columns: dict[str, list[str]] | None = None,
) -> common_pb2.CalculationsSpec:
    """
    Builds a CalculationsSpec naming the calculations to include in an export, and optionally which of their columns.

    Omitting frame_columns includes every frame and every column of the named calculations.  Supplying it restricts
    the export to the named columns of the named frames; frames not mentioned are excluded entirely.

    :param calculations_id: Id of the calculations object to export.
    :param frame_columns: Mapping of frame name to the column names to include from that frame.  Omit for all.
    :return: A common.CalculationsSpec for the specified calculations and column filter.
    :raises ValueError: if calculations_id is empty, or if any frame name or column name list is empty.
    """
    if not calculations_id:
        raise ValueError("calculations_spec() requires a non-empty calculations_id")

    spec = common_pb2.CalculationsSpec()
    spec.calculationsId = calculations_id

    if frame_columns:
        for frame_name, column_names in frame_columns.items():
            if not frame_name:
                raise ValueError("calculations_spec() requires a non-empty name for every frame")
            if not column_names:
                raise ValueError(
                    f"calculations_spec() requires a non-empty column name list for frame '{frame_name}'; "
                    f"omit the frame entirely to exclude it, or omit frame_columns to include all columns"
                )
            spec.dataFrameColumns[frame_name].columnNames[:] = column_names

    return spec


class ExportDataRequestParams:
    """
    Encapsulates client parameters for a call to the exportData() API method.

    At least one data source is required, and the sources merge: a saved DataSet by id, ad-hoc DataBlocks specified
    inline (treated by the server as a transient DataSet), and/or a CalculationsSpec.  A calculations-only export is
    legal and needs no ingested time-series data.
    """

    def __init__(
        self,
        output_format: "ExportFormat | str",
        dataset_id: str | None = None,
        data_blocks: list[annotation_pb2.DataBlock] | None = None,
        calculations_spec: common_pb2.CalculationsSpec | None = None,
    ) -> None:
        """
        :param output_format: The export file format, as an ExportFormat or its string value ("hdf5"/"csv"/"xlsx").
            A bare string is coerced through ExportFormat(), so a misspelling raises here rather than server-side.
        :param dataset_id: Id of a saved DataSet to export.
        :param data_blocks: Ad-hoc time ranges and PV names to export without saving a DataSet (see data_block()).
        :param calculations_spec: Calculations to include in the export (see calculations_spec()).
        :raises ValueError: if output_format is not a valid format, or if no data source is specified.
        """
        try:
            self.output_format = ExportFormat(output_format)
        except ValueError:
            valid = ", ".join(repr(member.value) for member in ExportFormat)
            raise ValueError(
                f"ExportDataRequestParams received an invalid output_format {output_format!r}; "
                f"valid formats are {valid}"
            ) from None

        if not dataset_id and not data_blocks and calculations_spec is None:
            raise ValueError(
                "ExportDataRequestParams requires at least one data source: dataset_id, data_blocks, "
                "or calculations_spec"
            )

        self.dataset_id = dataset_id
        self.data_blocks = data_blocks
        self.calculations_spec = calculations_spec


class ExportDataApiResult(ApiResultBase):
    """
    Wraps the response from exportData(), with a status object including an error flag and message.

    The exported file lives on the SERVER's filesystem: file_path is a server-side path, and file_url is populated
    only when the deployment publishes exports over HTTP.  There is no RPC for retrieving the file, so this library
    offers no download convenience.
    """

    def __init__(
        self,
        is_error: bool,
        message: str,
        response: annotation_pb2.ExportDataResponse | None = None,
    ) -> None:
        """
        :param is_error: Boolean flag indicating if an error occurred in the API call.
        :param message: Error message describing the error condition.
        :param response: The ExportDataResponse returned by the API call, or None.
        """
        super().__init__(is_error, message)
        self.response = response

    @property
    def file_path(self) -> str | None:
        """Server-side path of the exported file, or None on error."""
        if self.response is not None and self.response.HasField("exportDataResult"):
            return self.response.exportDataResult.filePath
        return None

    @property
    def file_url(self) -> str | None:
        """
        URL of the exported file, or None on error.

        Empty string is normal, not a failure: it means the deployment does not publish exports over HTTP.
        """
        if self.response is not None and self.response.HasField("exportDataResult"):
            return self.response.exportDataResult.fileUrl
        return None


class ExportClient(ServiceApiClientBase):
    """
    User-facing client for the data export method of the MLDP Annotation Service.  Exports a saved DataSet, ad-hoc
    data blocks, and/or calculations to a file on the server, in HDF5, CSV, or XLSX format.

    Provides a low-level wrapper for exportData().
    """

    def __init__(self, channel: grpc.Channel) -> None:
        """
        :param channel: gRPC communication channel for the Annotation Service.
        """
        super().__init__(channel, annotation_pb2_grpc.DpAnnotationServiceStub)
        self.logger = logging.getLogger(__name__)
        self.logger.debug("ExportClient initialized with channel: %s", channel)

    # ------------------------------------------------------------------
    # exportData
    # ------------------------------------------------------------------

    def _build_export_data_request(self, request_params: ExportDataRequestParams) -> annotation_pb2.ExportDataRequest:
        """
        Builds an ExportDataRequest from the supplied ExportDataRequestParams.
        :param request_params: User parameters for the call to exportData().
        :return: An ExportDataRequest for the specified params.
        """
        self.logger.debug("Building ExportDataRequest with format: %s", request_params.output_format.value)

        request = annotation_pb2.ExportDataRequest()
        request.outputFormat = request_params.output_format.to_proto()

        if request_params.dataset_id:
            request.dataSetId = request_params.dataset_id

        if request_params.data_blocks:
            request.dataBlocks.extend(request_params.data_blocks)

        if request_params.calculations_spec is not None:
            request.calculationsSpec.CopyFrom(request_params.calculations_spec)

        self.logger.debug("ExportDataRequest built successfully")
        return request

    def _send_export_data(self, request: annotation_pb2.ExportDataRequest) -> ExportDataApiResult:
        """
        Invokes the exportData() API method with the supplied request.
        :param request: ExportDataRequest with parameters for the call.
        :return: An ExportDataApiResult with the method response and status information.
        """
        return self._dispatch(
            self._stub.exportData,
            request,
            ExportDataApiResult,
            "exportDataResult",
            "exportData",
            request_log=lambda: self.logger.info(
                "Calling exportData API with format: %s",
                annotation_pb2.ExportDataRequest.ExportOutputFormat.Name(request.outputFormat),
            ),
            success_log=lambda response: self.logger.info(
                "Successfully exported data to: %s", response.exportDataResult.filePath
            ),
        )

    def export_data(self, request_params: ExportDataRequestParams) -> ExportDataApiResult:
        """
        User-facing method for invoking the exportData() API method.

        The exported file is written on the server; the result carries a server-side path and, when the deployment
        publishes over HTTP, a URL.

        :param request_params: Contains user parameters for the call to exportData().
        :return: An ExportDataApiResult with the method response and status information.
        """
        self.logger.info("Starting exportData operation with format: %s", request_params.output_format.value)

        request = self._build_export_data_request(request_params)
        result = self._send_export_data(request)

        if result.result_status.is_error:
            self.logger.error("ExportData operation failed: %s", result.result_status.message)
        else:
            self.logger.info("ExportData operation completed successfully")

        return result
