"""Pharmacy service layer.

All pharmacy data is REST pass-through: Fabric proxies the DB's plain-REST
/api/pharmacy/* endpoints with no transformation.

pharmacy_orders (MedicationRequest) and pharmacy_inventory (InventoryItem) are
also FHIR-backed — the change feed picks them up and publishes to Kafka so the
backend warms its internal DB. Agents that need live counts read from the internal DB; these
pass-through endpoints are for full-list queries that don't fit a per-record lookup.
"""

import logging

from clients import rest_client as rc
from config import settings

logger = logging.getLogger("pharmacy_service")

_REST = lambda: settings.db_rest_base_url   # http://192.46.212.81:3001/api  # noqa: E731


async def orders() -> list[dict]:
    return await rc.safe_list(_REST(), "pharmacy/orders")


async def pending_orders() -> list[dict]:
    return await rc.safe_list(_REST(), "pharmacy/pending")


async def stat_orders() -> list[dict]:
    return await rc.safe_list(_REST(), "pharmacy/stat")


async def inventory() -> list[dict]:
    return await rc.safe_list(_REST(), "pharmacy/inventory")


async def dispensing_log(hours: int = 8) -> list[dict]:
    return await rc.safe_list(_REST(), "pharmacy/dispensing-log", hours=hours)


async def interaction_rules() -> list[dict]:
    return await rc.safe_list(_REST(), "pharmacy/interactions")


async def substitution_rules() -> list[dict]:
    return await rc.safe_list(_REST(), "pharmacy/substitutions")


async def controlled_log(hours: int = 24) -> list[dict]:
    return await rc.safe_list(_REST(), "pharmacy/controlled-log", hours=hours)


async def capacity_history(days: int = 30) -> list[dict]:
    return await rc.safe_list(_REST(), "pharmacy/capacity", days=days)
