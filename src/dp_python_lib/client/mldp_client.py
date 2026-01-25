from dp_python_lib.client.ingestion_client import IngestionClient


class MldpClient:

    """
    This is the main user-facing API client class.  It provides variables for accessing the Ingestion, Query,
    and Annotation Service API clients, respectively.
    """

    def __init__(self, ingestion_channel):
        """
        :param ingestion_channel: gRPC communication channel for Ingestion Service.
        """
        self.ingestion_client = IngestionClient(ingestion_channel)
