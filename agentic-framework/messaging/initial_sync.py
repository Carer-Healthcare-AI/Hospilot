"""
Fabric -> Redis sync.

Cold-start: run_initial_sync() populates Redis before the Kafka change feed
takes over.  The 14 core tables use the new /sync/{table} cursor API (full
table dump, keyset pagination).  Financial data, visits, appointments, and
patients are not in the sync API so they still use individual REST endpoints.

Steady-state updates come from messaging.data_consumer -- no polling loops.
"""

import asyncio
import datetime
import logging

from cache import redis as cache
from db.fabric import fget

logger = logging.getLogger("initial_sync")

# ---------------------------------------------------------------------------
# Generic paginating fetcher for the /sync/{table} API
# ---------------------------------------------------------------------------

async def _fetch_all_pages(table: str, limit: int = 500) -> list[dict]:
    """Collect every row for `table` by following cursor pages until has_more=false."""
    cursor: str | None = None
    sync_id: str | None = None
    rows: list[dict] = []
    while True:
        page = await fget(f"/sync/{table}", limit=limit, cursor=cursor, sync_id=sync_id)
        sync_id = page.get("sync_id")
        rows.extend(page.get("rows") or [])
        pg = page.get("pagination", {})
        if not pg.get("has_more") or not pg.get("next_cursor"):
            break
        cursor = pg["next_cursor"]
    return rows


async def _sync_via_api(table: str, setter, label: str | None = None) -> None:
    """Fetch all pages for `table` and write rows to Redis via `setter(rows)`."""
    try:
        rows = await _fetch_all_pages(table)
        await setter(rows)
        logger.info("<- %s  count=%d", label or table, len(rows))
    except Exception as exc:
        logger.warning("%s sync failed: %s", label or table, exc)


# ---------------------------------------------------------------------------
# REST-only syncs (not covered by the /sync/{table} API)
# ---------------------------------------------------------------------------

async def _sync_visits() -> None:
    try:
        visits = await fget("/visits/er")
        await cache.set_visits(visits)
        for v in visits:
            vid = v.get("id")
            if vid:
                await cache.set(f"er_visit:{vid}", v, ttl=cache.VISIT_TTL)
        logger.info("<- visits  count=%d", len(visits))
    except Exception as exc:
        logger.warning("visits sync failed: %s", exc)


async def _sync_appointments() -> None:
    try:
        appointments = await fget("/appointments")
        await cache.set_appointments(appointments)
        logger.info("<- appointments  count=%d", len(appointments))
    except Exception as exc:
        logger.warning("appointments sync failed: %s", exc)


async def _sync_doctor_slots() -> None:
    try:
        slots = await fget("/appointments/slots")
        await cache.set_doctor_slots(slots)
        logger.info("<- doctor_slots  count=%d", len(slots))
    except Exception as exc:
        logger.warning("doctor_slots sync failed: %s", exc)


async def _sync_waitlist() -> None:
    # hospilot.waitlist is HIS-owned data, but Fabric has no /waitlist endpoint yet,
    # so warm Redis from the Hasura mirror (same fallback path appointments use).
    # Swap to fget("/appointments/waitlist") once Fabric exposes it.
    try:
        from db.hasura import hasura
        waitlist = await hasura.appt_list_waitlist()
        await cache.set_waitlist(waitlist)
        logger.info("<- waitlist  count=%d", len(waitlist))
    except Exception as exc:
        logger.warning("waitlist sync failed: %s", exc)


async def _sync_staff_roster() -> None:
    # hospilot.staff_roster is HIS-owned, but Fabric has no roster endpoint yet, so
    # warm Redis from the Hasura mirror (same fallback path as waitlist).
    try:
        from db.hasura import hasura
        roster = await hasura.staff_list_roster()
        await cache.set_staff_roster(roster)
        logger.info("<- staff_roster  count=%d", len(roster))
    except Exception as exc:
        logger.warning("staff_roster sync failed: %s", exc)


async def _sync_service_slots() -> None:
    # hospilot.service_slots (sample_collection / pharmacy_pickup) is HIS-owned, no
    # Fabric endpoint yet -- warm Redis from the Hasura mirror (like waitlist/roster).
    try:
        from db.hasura import hasura
        slots = await hasura.appt_list_service_slots()
        await cache.set_service_slots(slots)
        logger.info("<- service_slots  count=%d", len(slots))
    except Exception as exc:
        logger.warning("service_slots sync failed: %s", exc)


async def _sync_patients() -> None:
    try:
        tokens = await fget("/patients/tokens")
        if not tokens:
            return
        total = 0
        for i in range(0, len(tokens), 100):
            chunk = tokens[i:i + 100]
            data = await fget("/patients", ids=",".join(chunk))
            for token, patient in data.items():
                await cache.set_patient(token, patient)
            total += len(data)
        logger.info("<- patients  count=%d", total)
    except Exception as exc:
        logger.warning("patients sync failed: %s", exc)


async def _sync_ot_equipment() -> None:
    try:
        equipment = await fget("/ot/equipment-usage")
        await cache.set_ot_equipment_usage(equipment)
        logger.info("<- ot_equipment  count=%d", len(equipment))
    except Exception as exc:
        logger.warning("ot_equipment sync failed: %s", exc)


async def _sync_invoices() -> None:
    try:
        invoices = await fget("/financial/invoices", payment_status="Unpaid,Partial")
        await cache.set_invoices(invoices)
        logger.info("<- invoices  count=%d", len(invoices))
    except Exception as exc:
        logger.warning("invoices sync failed: %s", exc)


_CLAIM_CHILDREN_CONCURRENCY = 3


async def _sync_claims() -> None:
    try:
        claims = await fget("/financial/claims")
        await cache.set_claims(claims)
        sem = asyncio.Semaphore(_CLAIM_CHILDREN_CONCURRENCY)

        async def _fetch_children(cid: str) -> None:
            async with sem:
                try:
                    lines   = await fget(f"/financial/claims/{cid}/line-items")
                    history = await fget(f"/financial/claims/{cid}/history")
                    queries = await fget(f"/financial/claims/{cid}/queries")
                    if lines:
                        await cache.set_claim_line_items([{**li, "claim_id": cid} for li in lines])
                    if history:
                        await cache.set_claim_history([{**h,  "claim_id": cid} for h in history])
                    if queries:
                        await cache.set_claim_queries([{**q,  "claim_id": cid} for q in queries])
                except Exception:
                    pass

        await asyncio.gather(*[_fetch_children(c["id"]) for c in claims if c.get("id")])
        logger.info("<- claims  count=%d", len(claims))
    except Exception as exc:
        logger.warning("claims sync failed: %s", exc)


_PAYMENT_ENTRIES_CONCURRENCY = 5


async def _sync_payments() -> None:
    try:
        payments = await fget("/financial/payments")
        await cache.set_payments(payments)
        sem = asyncio.Semaphore(_PAYMENT_ENTRIES_CONCURRENCY)

        async def _fetch_entries(pid: str) -> None:
            async with sem:
                try:
                    entries = await fget(f"/financial/payments/{pid}/entries")
                    if entries:
                        await cache.set_payment_entries([{**e, "payment_id": pid} for e in entries])
                except Exception:
                    pass

        await asyncio.gather(*[_fetch_entries(p["id"]) for p in payments if p.get("id")])
        logger.info("<- payments  count=%d", len(payments))
    except Exception as exc:
        logger.warning("payments sync failed: %s", exc)


async def _sync_refunds() -> None:
    try:
        refunds = await fget("/financial/refunds")
        await cache.set_refunds(refunds)
        logger.info("<- refunds  count=%d", len(refunds))
    except Exception as exc:
        logger.warning("refunds sync failed: %s", exc)


_CONTRACT_RATES_CONCURRENCY = 3


async def _sync_contracts() -> None:
    try:
        contracts = await fget("/financial/contracts")
        await cache.set_contracts(contracts)
        sem = asyncio.Semaphore(_CONTRACT_RATES_CONCURRENCY)

        async def _fetch_rates(cid: str) -> None:
            async with sem:
                try:
                    rates = await fget(f"/financial/contracts/{cid}/rates")
                    if rates:
                        await cache.set_contract_rates([{**r, "contract_id": cid} for r in rates])
                except Exception:
                    pass

        await asyncio.gather(*[_fetch_rates(c["id"]) for c in contracts if c.get("id")])
        logger.info("<- contracts  count=%d", len(contracts))
    except Exception as exc:
        logger.warning("contracts sync failed: %s", exc)


async def _sync_financial_today() -> None:
    today = datetime.date.today().isoformat()
    try:
        data = await fget(f"/financial/collections/{today}")
        await cache.set_cached_collections(today, data)
        logger.info("<- collections  date=%s", today)
    except Exception as exc:
        logger.warning("collections sync failed: %s", exc)
    try:
        data = await fget(f"/financial/reconciliation/{today}")
        records = data if isinstance(data, list) else [data]
        await cache.set_reconciliation(records)
        logger.info("<- reconciliation  date=%s", today)
    except Exception as exc:
        logger.warning("reconciliation sync failed: %s", exc)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

async def run_initial_sync() -> None:
    logger.info("initial Fabric->Redis sync...")

    # 18 tables via /sync/{table} cursor API (all independent -- run concurrently)
    # Financial data, visits, appointments and patients use REST endpoints (not in sync API)
    await asyncio.gather(
        # --- /sync/{table} ---
        _sync_via_api("bed",                cache.set_beds),
        _sync_via_api("admission",          cache.set_admissions),
        _sync_via_api("discharge_ready",    cache.set_discharge_ready),
        _sync_via_api("discharge_summary",  cache.set_discharge_summaries),
        _sync_via_api("lab",                cache.set_lab_orders,         label="lab_orders"),
        _sync_via_api("lab_result",         cache.set_lab_results),
        _sync_via_api("lab_sample",         cache.set_lab_samples),
        _sync_via_api("lab_analyzer",       cache.set_lab_analyzers),
        _sync_via_api("pharmacy_order",     cache.set_pharmacy_orders),
        _sync_via_api("pharmacy_inventory", cache.set_pharmacy_inventory),
        _sync_via_api("task",               cache.set_tasks),
        _sync_via_api("ot_room",            cache.set_ot_rooms),
        _sync_via_api("ot_room_status",     cache.set_ot_room_status),
        _sync_via_api("ot_schedule",        cache.set_ot_schedule),
        _sync_via_api("ot_surgery",         cache.set_ot_surgeries),
        _sync_via_api("ambulance",          cache.set_ambulances),
        _sync_via_api("dept",               cache.set_departments),
        _sync_via_api("ventilator",         cache.set_ventilators),
        _sync_via_api("staff_roster",       cache.set_staff_roster),
        _sync_via_api("staff",              cache.set_staff),
        # --- REST endpoints (not in sync API) ---
        _sync_visits(),
        _sync_appointments(),
        _sync_doctor_slots(),
        _sync_waitlist(),
        _sync_service_slots(),
        _sync_ot_equipment(),
        _sync_invoices(),
        _sync_claims(),
        _sync_payments(),
        _sync_refunds(),
        _sync_contracts(),
        _sync_financial_today(),
        return_exceptions=True,
    )
    await _sync_patients()   # after admissions so patient tokens are warm
    logger.info("[ok] initial sync complete")


async def start_poller() -> None:
    """Polling loops replaced by messaging.data_consumer (event-driven Redis updates).
    Retained so main.py can still create a task without import changes.
    run_initial_sync() above still seeds Redis on cold-start."""
    logger.info("poller loops disabled — Redis updates driven by Kafka data consumer")
