"""Operating-theatre reads — thin wrappers over the DB's plain-REST /ot API.

Responses are already the dict shapes the OT agents want, so Fabric passes them
through untransformed (no FHIR involved).

Delivery paths (see fabric/README.md for the full table):
  • streamed → Kafka → the backend's internal DB:
      rooms, room_status, surgery_schedule, surgeries
      Registered in topic_map.REST_ENTITIES as ot_room / ot_room_status / ot_schedule /
      ot_surgery. Agents read the steady state from the internal DB, so these have no
      HTTP route.
  • runtime pass-through:  equipment_usage
      Not cached; served live by GET /ot/equipment-usage.
"""

from clients import rest_client as rc
from config import settings


def _base() -> str:
    return f"{settings.db_rest_base_url}/ot"


async def rooms() -> list[dict]:
    return await rc.list_(_base(), "rooms")


async def room_status() -> list[dict]:
    return await rc.list_(_base(), "room-status")


async def surgery_schedule() -> list[dict]:
    return await rc.list_(_base(), "surgery-schedule")


async def equipment_usage() -> list[dict]:
    return await rc.safe_list(_base(), "equipment-usage")


async def surgeries() -> list[dict]:
    return await rc.list_(_base(), "surgeries")
