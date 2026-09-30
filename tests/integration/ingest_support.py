"""
Ingesting test data through the library's own IngestionClient, shared by the integration tests.

Every test that needs archived samples -- a dataset's data block, a v2 query, a sample status to filter on --
ingests its own, and synchronizes on the request-status document rather than on an ack or a probe loop.  An ack
means only that the request passed validation; the status document is written after the buckets, so a SUCCESS
status means the data is persisted and queryable (plan/tickets/17/plan.md, T2).

It also holds require_services(), the reachability check every ingesting test class runs first.
"""

import unittest
import uuid
from datetime import datetime, timezone

import grpc

from dp_python_lib.client import IngestDataRequestParams, IngestionRequestStatus, RegisterProviderRequestParams

INGESTION_ADDRESS = "localhost:50051"
QUERY_ADDRESS = "localhost:50052"
ANNOTATION_ADDRESS = "localhost:50053"


def require_services(*services: tuple[str, str]) -> None:
    """Skips the calling test class unless each (label, address) accepts a connection within 5s."""
    for label, address in services:
        channel = grpc.insecure_channel(address)
        try:
            grpc.channel_ready_future(channel).result(timeout=5)
        except grpc.FutureTimeoutError:
            raise unittest.SkipTest(f"MLDP {label} service not available at {address}") from None
        finally:
            channel.close()


def register_provider(ingestion, name: str) -> str:
    """Registers (or re-finds) a provider by name and returns its id, raising RuntimeError on failure."""
    result = ingestion.register_provider(
        RegisterProviderRequestParams(name, description=None, tag_list=None, attribute_map=None)
    )
    if result.provider_id is None:
        raise RuntimeError(f"could not register ingestion provider {name!r}: {result.result_status.message}")
    return result.provider_id


def ingest_confirmed(ingestion, provider_id: str, frame, *, timeout: float = 30.0) -> str:
    """
    Ingests one frame and waits for its request to reach SUCCESS, returning the client request id.

    :raises RuntimeError: if the request is rejected, or its status document says anything other than SUCCESS.
    :raises TimeoutError: if no status document appears within the timeout.
    """
    params = IngestDataRequestParams(provider_id, frame, client_request_id=f"itest-{uuid.uuid4()}")
    since = datetime.now(timezone.utc)  # captured BEFORE sending: the status query's time floor
    ack = ingestion.ingest_data(params)
    if ack.result_status.is_error:
        raise RuntimeError(f"ingest request {params.client_request_id} was rejected: {ack.result_status.message}")
    statuses = ingestion.await_request_statuses(provider_id, [params.client_request_id], since=since, timeout=timeout)
    for document in statuses[params.client_request_id]:
        if document.ingestionRequestStatus != IngestionRequestStatus.SUCCESS:
            raise RuntimeError(
                f"ingest request {params.client_request_id} ended "
                f"{IngestionRequestStatus(document.ingestionRequestStatus).name}: {document.statusMessage}"
            )
    return params.client_request_id
