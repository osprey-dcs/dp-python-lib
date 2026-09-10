import logging

import grpc

from dp_python_lib.client.annotations_client import AnnotationsClient
from dp_python_lib.client.dataset_client import DataSetClient
from dp_python_lib.client.export_client import ExportClient
from dp_python_lib.client.machine_config_client import MachineConfigClient
from dp_python_lib.client.pv_metadata_client import PvMetadataClient
from dp_python_lib.client.sample_status_client import SampleStatusClient


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

    def __init__(self, channel: grpc.Channel) -> None:
        """
        :param channel: gRPC communication channel for the Annotation Service.
        """
        self.logger = logging.getLogger(__name__)
        self._channel = channel
        self.pv_metadata = PvMetadataClient(channel)
        self.machine_config = MachineConfigClient(channel)
        self.sample_status = SampleStatusClient(channel)
        self.datasets = DataSetClient(channel)
        self.annotations = AnnotationsClient(channel)
        self.export = ExportClient(channel)
        self.logger.debug("AnnotationClient initialized with channel: %s", channel)
