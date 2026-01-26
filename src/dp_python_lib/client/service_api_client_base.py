from abc import ABC
import grpc
import logging


class ServiceApiClientBase(ABC):
    """
    This is the base class for the various service client classes.  It contains a constructor to save the specified
    channel to an instance variable.
    """

    def __init__(self, channel: grpc.Channel) -> None:
        """
        :param channel: gRPC communication channel for the client's backend Service.
        """
        self.logger = logging.getLogger(__name__)
        self._channel = channel
        self.logger.debug("Initialized service client with channel: %s", channel)

