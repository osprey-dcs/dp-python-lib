from abc import ABC


class ServiceApiClientBase(ABC):
    """
    This is the base class for the various service client classes.  It contains a constructor to save the specified
    channel to an instance variable.
    """

    def __init__(self, channel):
        """
        :param channel: gRPC communication channel for the client's backend Service.
        """
        self._channel = channel

