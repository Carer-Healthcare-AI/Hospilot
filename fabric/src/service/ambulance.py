"""Ambulance fleet reads — thin wrapper over the DB's plain-REST /ambulance API.

Delivery path: STREAMED only. fleet() is registered in topic_map.REST_ENTITIES as
`ambulance`, so the diff poller publishes changes to Kafka and hospilot-backend
caches them in its internal DB; agents read from there. There is deliberately no HTTP route
for it — Fabric exposes no ambulance endpoint at all.
"""

from clients import rest_client as rc
from config import settings


def _base() -> str:
    return settings.db_rest_base_url


async def fleet() -> list[dict]:
    return await rc.list_(_base(), "ambulance")
