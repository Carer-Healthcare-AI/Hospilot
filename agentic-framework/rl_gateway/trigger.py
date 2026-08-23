"""Bed-release trigger: Kafka data event -> advisory auction -> persist -> queue reward.

Wiring: messaging/data_consumer._handle calls on_data_event() for every change (a one-line,
non-fatal hook, exactly like notify_entity_change). We filter to bed-release signals, nominate
one candidate per department, run an advisory auction, and persist it.

Two safeguards live here:
  · per-(org, resource) serialisation — mitigates the engine's in-memory budget race (A7/T6)
    by never running two auctions against the same resource+budget concurrently in one process.
  · idempotency is the engine's (auction_key bucket), but we also skip if nothing is nominated.

org context: defaults to 'default' (Carer, unprefixed DB). Multi-tenant fan-out — resolving
which org a Kafka event belongs to — is the one integration point left open here.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from db.hasura import hasura

from rl_gateway.assemble import WARD_TO_UNIT
from rl_gateway.auction import advise
from rl_gateway.db import tenant_transaction
from rl_gateway.forecast import Forecaster
from rl_gateway.persist import persist
from rl_gateway.reward import enqueue_observation

log = logging.getLogger("rl_gateway.trigger")

RELEASE_ENTITIES = {"bed", "discharge_ready"}
# Bed statuses that mean a bed is coming free (verified enum, lowercased): a bed going to
# vacating/dirty/cleaning/available is a release worth auctioning the next occupant for.
FREEING_BED_STATUSES = {"vacating", "dirty", "cleaning", "available"}

_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _lock(org: str, resource: str) -> asyncio.Lock:
    key = (org, resource)
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


def _classify_release(entity: str, data: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Turn a change event into a release descriptor, or None if it isn't one we auction."""
    if not data:
        return None
    ward = str(data.get("ward", "")).strip().lower()
    unit = WARD_TO_UNIT.get(ward)
    if unit is None:
        return None
    if entity == "bed" and str(data.get("status", "")).strip().lower() not in FREEING_BED_STATUSES:
        return None
    return {
        "unit": unit,
        "resource": f"{unit}_bed",
        "query": f"a {unit} bed is opening",
        "resource_id": data.get("id") or data.get("bed_id"),
        # Cleaning ETA is a 45-min ASSUMPTION not tracked in the DB — every timing claim
        # inherits its error. predicted_free_at left to the engine's opened_at when unknown.
        "predicted_free_at": data.get("predicted_free_at"),
    }


async def nominate_candidates(hasura_client: Any, unit: str) -> list[dict[str, Any]]:
    """One strongest claim per department for the freeing `unit`. Heuristic starting point:
    the most critical current patients, one per department, mapped to er/ot/ward (icu->ward).
    Real nomination is a clinical policy decision — refine with the departments.
    """
    try:
        critical = await hasura_client.get_critical_vitals() or []
    except Exception as exc:  # noqa: BLE001
        log.warning("nomination: could not read critical vitals: %s", exc)
        return []

    # Critical vitals carry no ward (verified) — ward is via admission→bed. Resolve it.
    ward_by_admission: dict[str, str] = {}
    try:
        for adm in await hasura_client.get_admissions_with_wards() or []:
            key = adm.get("admission_id") or adm.get("id")
            if key:
                ward_by_admission[key] = str(adm.get("ward", "")).strip().lower()
    except Exception as exc:  # noqa: BLE001
        log.warning("nomination: could not resolve admission wards: %s", exc)

    by_department: dict[str, dict[str, Any]] = {}
    for patient in critical:
        token = patient.get("patient_token")
        if not token:
            continue
        admission_id = patient.get("admission_id")
        ward = ward_by_admission.get(admission_id, "")
        # ward -> department bucket (er | ot | icu), the cases.json department vocabulary.
        # icu-resident/medical demand bids as 'icu', which maps to the engine's 'ward' slot
        # (AGENT_MAP). Must NOT emit 'ward' here — that has no AGENT_MAP entry and map_agent
        # would raise IneligibleAgentError.
        department = {"ed": "er", "er": "er", "pacu": "ot", "ot": "ot"}.get(ward, "icu")
        if department in by_department:
            continue  # first (most critical; list assumed pre-ordered) wins the slot
        by_department[department] = {
            "department": department,
            "candidate_id": token,
            "patient_token": token,
            "admission_id": admission_id,
            "visit_id": None,
            # vitals have no arrived_at; recorded_at is the best available proxy.
            "arrived_at": patient.get("recorded_at"),
            "current_unit": ward or None,
        }
    return list(by_department.values())


async def open_auction(entity: str, data: Mapping[str, Any], org_slug: str = "default") -> None:
    release = _classify_release(entity, data)
    if release is None:
        return

    async with _lock(org_slug, release["resource"]):
        specs = await nominate_candidates(hasura, release["unit"])
        if not specs:
            log.info("no candidates nominated for %s release; skipping", release["unit"])
            return

        buffer: list[tuple] = []

        async def _record(endpoint, scope, horizon, value, payload, raw):
            buffer.append((endpoint, scope, horizon, value, payload, raw))

        resp = await advise(
            hasura,
            query=release["query"],
            unit=release["unit"],
            resource=release["resource"],
            candidate_specs=specs,
            forecaster=Forecaster(record=_record),
        )

        async with tenant_transaction(org_slug) as execute:
            await persist(
                resp, execute,
                trigger_source=f"cdc:{entity}",
                predicted_free_at=release["predicted_free_at"],
            )
            await enqueue_observation(execute, resp)
            for endpoint, scope, horizon, value, payload, raw in buffer:
                await _record_forecast(execute, endpoint, scope, horizon, value, payload, raw)

        log.info(
            "advisory auction %s: winner=%s outcome=%s (%s)",
            resp.get("auction_id"), resp.get("winner"), resp.get("outcome"), release["resource"],
        )


_FORECAST_SQL = """
INSERT INTO allocation.forecast_history (endpoint, scope, horizon, forecast_for, value, payload, raw_response)
VALUES (%s, %s, %s, now(), %s, %s, %s)
"""


async def _record_forecast(execute, endpoint, scope, horizon, value, payload, raw) -> None:
    import json
    await execute(_FORECAST_SQL, (
        endpoint, scope, horizon, value, json.dumps(payload or {}), json.dumps(raw) if raw else None,
    ))


def on_data_event(entity: str, record_id: str, operation: str, data: dict | None) -> None:
    """Non-fatal hook for messaging/data_consumer._handle. Fire-and-forget: never block or
    break the consumer. No-op unless the allocation gateway is enabled and configured."""
    if entity not in RELEASE_ENTITIES or operation == "delete":
        return
    try:
        asyncio.get_running_loop().create_task(open_auction(entity, data or {}))
    except Exception as exc:  # noqa: BLE001
        log.debug("allocation trigger skipped: %s", exc)
