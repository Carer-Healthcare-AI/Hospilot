"""Lab service layer.

Two data access patterns:

  • FHIR-backed, streamed — lab_samples (Specimen) + lab_analyzers (Device).
    Fabric fetches from the DB's FHIR API and transforms; the change feed also
    publishes them to Kafka, so hospilot-backend caches them and agents read the internal DB.

  • REST pass-through (agent-direct) — qc_logs, reflex_rules, validation_rules,
    capacity_history, critical_escalations. Fabric proxies the DB's plain-REST
    /api/lab/* endpoints with no transformation. Not cached anywhere.

lab_orders and lab_results are served by clinical.py (FHIR ServiceRequest /
Observation) and are streamed via the same change feed.
"""

import logging

from clients import fhir_client as fc
from clients import rest_client as rc
from config import settings
from service import transform as tx

logger = logging.getLogger("lab_service")

_REST = lambda: settings.db_rest_base_url   # http://192.46.212.81:3001/api  # noqa: E731


# ─── FHIR-backed (sync → internal DB → agents read there) ──────────────────────────

async def samples() -> list[dict]:
    specimens = await fc.search_specimens({"_count": "200"})
    return [tx.lab_sample(s) for s in specimens]


async def analyzers() -> list[dict]:
    devices = await fc.search_devices({"_count": "200"})
    return [tx.lab_analyzer(d) for d in devices]


# ─── REST pass-through (agents call Fabric, Fabric calls DB REST) ─────────────

async def qc_logs(hours: int = 24) -> list[dict]:
    return await rc.safe_list(_REST(), "lab/qc-logs", hours=hours)


async def reflex_rules() -> list[dict]:
    return await rc.safe_list(_REST(), "lab/reflex-rules")


async def validation_rules() -> list[dict]:
    return await rc.safe_list(_REST(), "lab/validation-rules")


async def capacity_history(days: int = 30) -> list[dict]:
    return await rc.safe_list(_REST(), "lab/capacity-history", days=days)


async def critical_escalations() -> list[dict]:
    return await rc.safe_list(_REST(), "lab/critical-escalations")
