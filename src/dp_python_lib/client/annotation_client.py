import logging

import grpc

from dp_python_lib.client.annotations_client import AnnotationsClient
from dp_python_lib.client.dataset_client import DataSetClient
from dp_python_lib.client.export_client import ExportClient
from dp_python_lib.client.machine_config_client import MachineConfigClient
from dp_python_lib.client.pv_metadata_client import PvMetadataClient
from dp_python_lib.client.sample_status_client import SampleStatusClient
from dp_python_lib.client.service_api_client_base import DEFAULT, TimeoutArg


class AnnotationClient:
    """
    Facade for the MLDP Annotation Service.  The upstream DpAnnotationService owns several distinct feature areas
    (PV metadata, machine configuration, sample status, datasets, annotations, export); this facade groups the
    corresponding feature-scoped clients under one object, all sharing the single Annotation Service channel.

    Exposes:
        - pv_metadata: PvMetadataClient for the PV metadata API methods.
        - machine_config: MachineConfigClient for the machine configuration API methods.
        - sample_status: SampleStatusClient for the sample status API methods.
        - datasets: DataSetClient for the DataSet API methods.
        - annotations: AnnotationsClient for the annotation and calculations API methods.
        - export: ExportClient for the data export API method.

    Note the near-collision between this facade (AnnotationClient, singular) and the feature client it exposes as
    .annotations (AnnotationsClient, plural).  Users reach both through MldpClient and construct neither directly.
    """

    def __init__(
        self,
        channel: grpc.Channel,
        *,
        call_timeout: TimeoutArg = DEFAULT,
        stream_idle_timeout: TimeoutArg = DEFAULT,
        export_timeout: TimeoutArg = DEFAULT,
        call_timeout_source: str | None = None,
        stream_idle_timeout_source: str | None = None,
        export_timeout_source: str | None = None,
    ) -> None:
        """
        :param channel: gRPC communication channel for the Annotation Service.
        :param call_timeout: Default deadline, in seconds, for each unary call of every feature client except
            export.  Omitted: DEFAULT_CALL_TIMEOUT_SECONDS.  None or 0 disables it.
        :param stream_idle_timeout: Default idle timeout, in seconds, for the streaming calls (sample status).
            Omitted: DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS.  None or 0 disables it.
        :param export_timeout: Default deadline, in seconds, for export_data().  Omitted:
            DEFAULT_EXPORT_TIMEOUT_SECONDS.  None or 0 disables it.
        :param call_timeout_source: How a DEADLINE_EXCEEDED error describes where ``call_timeout`` was set
            (MldpClient names the config setting).
        :param stream_idle_timeout_source: The same, for ``stream_idle_timeout``.
        :param export_timeout_source: The same, for ``export_timeout``.
        """
        self.logger = logging.getLogger(__name__)
        self._channel = channel
        self.pv_metadata = PvMetadataClient(channel, call_timeout=call_timeout, call_timeout_source=call_timeout_source)
        self.machine_config = MachineConfigClient(
            channel, call_timeout=call_timeout, call_timeout_source=call_timeout_source
        )
        self.sample_status = SampleStatusClient(
            channel,
            call_timeout=call_timeout,
            stream_idle_timeout=stream_idle_timeout,
            call_timeout_source=call_timeout_source,
            stream_idle_timeout_source=stream_idle_timeout_source,
        )
        self.datasets = DataSetClient(channel, call_timeout=call_timeout, call_timeout_source=call_timeout_source)
        self.annotations = AnnotationsClient(
            channel, call_timeout=call_timeout, call_timeout_source=call_timeout_source
        )
        self.export = ExportClient(channel, export_timeout=export_timeout, export_timeout_source=export_timeout_source)
        self.logger.debug("AnnotationClient initialized with channel: %s", channel)
