from abc import ABC
from typing import Any, Callable
import grpc
import logging


class ServiceApiClientBase(ABC):
    """
    This is the base class for the various service client classes.  It saves the specified channel and creates the
    service's gRPC stub once at construction time, so subclasses reuse a single stub instance across API calls rather
    than creating a new stub for each call.
    """

    def __init__(self, channel: grpc.Channel, stub_class: Callable[[grpc.Channel], Any]) -> None:
        """
        :param channel: gRPC communication channel for the client's backend Service.
        :param stub_class: The generated gRPC stub class for the client's backend Service (e.g. DpIngestionServiceStub).
            It is instantiated once with the supplied channel and stored as self._stub.
        """
        self.logger = logging.getLogger(__name__)
        self._channel = channel
        self._stub = stub_class(channel)
        self.logger.debug(
            "Initialized service client with channel: %s, stub: %s", channel, stub_class.__name__
        )
