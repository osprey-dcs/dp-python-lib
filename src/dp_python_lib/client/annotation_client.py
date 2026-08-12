import logging

import grpc

from dp_python_lib.client.machine_config_client import MachineConfigClient
from dp_python_lib.client.pv_metadata_client import PvMetadataClient


class AnnotationClient:
    """
    Facade for the MLDP Annotation Service.  The upstream DpAnnotationService owns several distinct feature areas
    (PV metadata, machine configuration, annotations, ...); this facade groups the corresponding feature-scoped
    clients under one object, all sharing the single Annotation Service channel.

    Currently exposes:
        - pv_metadata: PvMetadataClient for the PV metadata API methods.
        - machine_config: MachineConfigClient for the machine configuration API methods.

    Future feature clients (annotations) will be added here as additional attributes.
    """

    def __init__(self, channel: grpc.Channel) -> None:
        """
        :param channel: gRPC communication channel for the Annotation Service.
        """
        self.logger = logging.getLogger(__name__)
        self._channel = channel
        self.pv_metadata = PvMetadataClient(channel)
        self.machine_config = MachineConfigClient(channel)
        self.logger.debug("AnnotationClient initialized with channel: %s", channel)
