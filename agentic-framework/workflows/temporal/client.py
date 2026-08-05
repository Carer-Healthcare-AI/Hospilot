"""Shared Temporal client (the FastAPI process is the client; activities run on
the separate worker). Lazily connected and reused."""
import logging

from temporalio.client import Client

from config import settings

logger = logging.getLogger("temporal")

_client: Client | None = None


async def get_temporal_client() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
        )
        logger.info("[ok] Temporal client connected  host=%s  ns=%s",
                    settings.temporal_host, settings.temporal_namespace)
    return _client


async def close_temporal_client() -> None:
    global _client
    _client = None  # the Python SDK client holds no long-lived resource to close
