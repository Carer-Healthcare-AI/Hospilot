import json
import time
from typing import Any

import redis.asyncio as aioredis

from config import settings

_client: aioredis.Redis | None = None


async def init_redis() -> None:
    global _client
    _client = aioredis.from_url(settings.redis_url, decode_responses=True)


async def close_redis() -> None:
    if _client:
        await _client.aclose()


def get_client() -> aioredis.Redis:
    if _client is None:
        raise RuntimeError("Redis not initialised -- call init_redis() first")
    return _client


# -------------------------------------------------------------------------
# Generic helpers
# -------------------------------------------------------------------------

async def get(key: str) -> Any | None:
    raw = await get_client().get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


async def set(key: str, value: Any, ttl: int | None = None) -> None:
    raw = json.dumps(value) if not isinstance(value, str) else value
    if ttl:
        await get_client().setex(key, ttl, raw)
    else:
        await get_client().set(key, raw)


async def delete(key: str) -> None:
    await get_client().delete(key)


async def keys(pattern: str) -> list[str]:
    return await get_client().keys(pattern)


async def get_many(pattern: str) -> list[Any]:
    ks = await keys(pattern)
    if not ks:
        return []
    raws = await get_client().mget(ks)
    result = []
    for raw in raws:
        if raw is not None:
            try:
                result.append(json.loads(raw))
            except json.JSONDecodeError:
                result.append(raw)
    return result


async def incr(key: str) -> int:
    """Atomically increment a counter and return the new value (creates at 1)."""
    return int(await get_client().incr(key))


async def expire(key: str, ttl: int) -> None:
    await get_client().expire(key, ttl)


async def delete_pattern(pattern: str) -> int:
    """Delete every key matching a glob pattern. Returns count deleted."""
    ks = await keys(pattern)
    if not ks:
        return 0
    return int(await get_client().delete(*ks))


# Failure-reorchestration loop guard: max auto-recommendations per session before
# the session is marked failed and we stop re-planning (see graph.synthesis).
FAILURE_REPLAN_CAP = 2


# -------------------------------------------------------------------------
# Bed helpers
# -------------------------------------------------------------------------

BED_TTL        = None
ADMISSION_TTL  = None
VISIT_TTL      = None
DEPARTMENT_TTL = None
VITAL_TTL      = None


#async def get_all_beds() -> list[dict]:
#    beds = [b for b in await _get_all_indexed("bed") if b]
#    if beds:
#        return beds
#    return await get_many("bed:*")

async def get_all_beds() -> list[dict]:
    beds = [b for b in (await _get_all_indexed("bed") or []) if b]
    
    if beds:
        return beds
    return await get_many("bed:*")

async def set_beds(beds: list[dict]) -> None:
    await _set_indexed("bed", beds, ttl=BED_TTL)


async def set_bed(bed_id: str, data: dict) -> None:
    """Replace the whole bed record. Callers with a PARTIAL update want patch_bed."""
    await set(f"bed:{bed_id}", data, ttl=BED_TTL)


async def patch_bed(bed_id: str, patch: dict) -> dict:
    """Merge `patch` into the cached bed, keeping every field not being changed.

    set_bed() overwrites the whole record, so handing it a partial dict silently drops
    ward / bed_number / room_type / is_active. That matters most for `ward`: the ICU
    filters key on it (is_icu_bed reads bed["ward"]), so a flattened record disappears
    from every ICU query until the next change event happens to restore it.

    Also keeps bed:ids current, which plain set_bed does not -- so a bed patched before
    it was ever indexed still shows up in get_all_beds().
    """
    existing = await get(f"bed:{bed_id}")
    if not isinstance(existing, dict):
        existing = {}
    merged = {**existing, **patch, "id": bed_id}
    await upsert_indexed("bed", merged, BED_TTL)
    return merged


async def mark_bed_freed(bed_id: str) -> None:
    """Track beds Hospilot just freed -- protects them from poller overwrites for 120s."""
    await get_client().sadd("hospilot:freed_beds", bed_id)
    await get_client().expire("hospilot:freed_beds", 120)


async def get_freed_bed_ids() -> frozenset:
    members = await get_client().smembers("hospilot:freed_beds")
    return frozenset(members) if members else frozenset()


async def get_all_admissions() -> list[dict]:
    return await _get_all_indexed("admission")


async def set_admissions(admissions: list[dict]) -> None:
    await _set_indexed("admission", admissions, ttl=ADMISSION_TTL)


async def get_all_visits() -> list[dict]:
    return await _get_all_indexed("visit")


async def set_visits(visits: list[dict]) -> None:
    await _set_indexed("visit", visits, ttl=VISIT_TTL)


async def get_all_departments() -> list[dict]:
    return await _get_all_indexed("dept")


async def set_departments(departments: list[dict]) -> None:
    await _set_indexed("dept", departments, ttl=DEPARTMENT_TTL)


# -------------------------------------------------------------------------
# Patient helpers
# -------------------------------------------------------------------------

async def get_patient(token: str) -> dict | None:
    return await get(f"patient:{token}")


async def set_patient(token: str, data: dict) -> None:
    await set(f"patient:{token}", data, ttl=300)


# -------------------------------------------------------------------------
# Session state helpers
# -------------------------------------------------------------------------

async def set_session_overrides(session_id: str, agent_id: str, tasks: list) -> None:
    await set(f"session:{session_id}:overrides:{agent_id}", tasks, ttl=3600)


async def get_session_overrides(session_id: str, agent_id: str) -> list | None:
    return await get(f"session:{session_id}:overrides:{agent_id}")


async def set_session_result(session_id: str, key: str, data: dict) -> None:
    await set(f"session:{session_id}:{key}", data, ttl=3600)


async def get_session_result(session_id: str, key: str) -> dict | None:
    return await get(f"session:{session_id}:{key}")


# -------------------------------------------------------------------------
# Bed lock (optimistic locking to prevent double booking)
# -------------------------------------------------------------------------

async def acquire_bed_lock(lock_key: str, session_id: str, ttl: int = 60) -> bool:
    """lock_key should be the full key, e.g. 'bed_lock:bed-icu-01'.

    Returns True if we now hold the lock. Re-acquire by the SAME session is a
    success (idempotent) -- so a Temporal activity retry or a LangGraph node
    re-run doesn't get falsely rejected for a bed it already holds. Another
    session still gets False (no double-booking).
    """
    result = await get_client().set(lock_key, session_id, nx=True, ex=ttl)
    if result is True:
        return True
    return (await get_client().get(lock_key)) == session_id


async def release_bed_lock(lock_key: str, session_id: str | None = None) -> None:
    """Only releases the lock if session_id matches (or unconditionally if None)."""
    if session_id is not None:
        current = await get_client().get(lock_key)
        if current == session_id:
            await delete(lock_key)
    else:
        await delete(lock_key)


# -------------------------------------------------------------------------
# Generic id-based helpers
# -------------------------------------------------------------------------

async def _set_indexed(prefix: str, records: list[dict], ttl: int) -> None:
    """Store each record at {prefix}:{id} and write an index key {prefix}:ids."""
    ids = []
    for rec in records:
        rid = rec.get("id")
        if rid:
            await set(f"{prefix}:{rid}", rec, ttl=ttl)
            ids.append(rid)
    await set(f"{prefix}:ids", ids, ttl=ttl)


async def _get_all_indexed(prefix: str) -> list[dict]:
    """Retrieve all records via the index key (no KEYS scan)."""
    ids = await get(f"{prefix}:ids") or []
    if not ids:
        return []
    client = get_client()
    raws = await client.mget([f"{prefix}:{i}" for i in ids])
    result = []
    for raw in raws:
        if raw is not None:
            try:
                result.append(json.loads(raw))
            except json.JSONDecodeError:
                pass
    return result


async def upsert_indexed(prefix: str, record: dict, ttl: int) -> None:
    """Upsert a single record without wiping the rest of the index.

    Used by the Kafka data consumer to apply individual change events.
    Reads the current ids list, appends the new id if absent, writes back.
    """
    rid = record.get("id")
    if not rid:
        return
    await set(f"{prefix}:{rid}", record, ttl=ttl)
    ids: list = await get(f"{prefix}:ids") or []
    if rid not in ids:
        ids.append(rid)
    await set(f"{prefix}:ids", ids, ttl=ttl)


async def delete_indexed(prefix: str, record_id: str, ttl: int) -> None:
    """Remove a single record from the index on a Kafka delete event."""
    await delete(f"{prefix}:{record_id}")
    ids: list = await get(f"{prefix}:ids") or []
    if record_id in ids:
        ids.remove(record_id)
        await set(f"{prefix}:ids", ids, ttl=ttl)


# -------------------------------------------------------------------------
# Canonical FHIR mirror -- parallel `fhir:{Type}:{id}` keyspace.
# Written by the poller alongside the agent projection (dual-write). Agents
# never read these; they exist for the outbound API / audit / future use.
# -------------------------------------------------------------------------

async def set_fhir(resource_type: str, resources: list, ttl: int) -> None:
    """Store each fhir.resources model as JSON at fhir:{type}:{id} + an index."""
    prefix = f"fhir:{resource_type}"
    client = get_client()
    ids: list[str] = []
    for r in resources:
        rid = getattr(r, "id", None)
        if not rid:
            continue
        raw = r.model_dump_json(exclude_none=True, by_alias=True)
        await client.setex(f"{prefix}:{rid}", ttl, raw)
        ids.append(rid)
    await set(f"{prefix}:ids", ids, ttl=ttl)


async def get_fhir(resource_type: str, rid: str) -> dict | None:
    return await get(f"fhir:{resource_type}:{rid}")


async def get_all_fhir(resource_type: str) -> list[dict]:
    return await _get_all_indexed(f"fhir:{resource_type}")


# -------------------------------------------------------------------------
# Invoice helpers (Revenue Agent)
# -------------------------------------------------------------------------

INVOICE_TTL = None


async def get_all_invoices() -> list[dict]:
    return await _get_all_indexed("invoice")


async def set_invoices(invoices: list[dict]) -> None:
    await _set_indexed("invoice", invoices, ttl=INVOICE_TTL)


async def get_invoice(invoice_id: str) -> dict | None:
    return await get(f"invoice:{invoice_id}")


# -------------------------------------------------------------------------
# Daily collections helpers (Revenue Agent)
# -------------------------------------------------------------------------

COLLECTIONS_TTL = None


async def get_cached_collections(date_str: str) -> dict | None:
    return await get(f"collections:{date_str}")


async def set_cached_collections(date_str: str, data: dict) -> None:
    await set(f"collections:{date_str}", data, ttl=COLLECTIONS_TTL)


# -------------------------------------------------------------------------
# Claims (Billing Agent)
# -------------------------------------------------------------------------

CLAIM_TTL = None


async def set_claims(claims: list[dict]) -> None:
    await _set_indexed("claim", claims, ttl=CLAIM_TTL)


async def get_all_claims() -> list[dict]:
    return await _get_all_indexed("claim")


async def get_claim(claim_id: str) -> dict | None:
    return await get(f"claim:{claim_id}")


# -------------------------------------------------------------------------
# Claim child tables -- keyed by parent claim_id
# -------------------------------------------------------------------------

CLAIM_CHILD_TTL = None


async def set_claim_line_items(line_items: list[dict]) -> None:
    by_claim: dict[str, list] = {}
    for item in line_items:
        cid = item.get("claim_id")
        if cid:
            by_claim.setdefault(cid, []).append(item)
    for cid, items in by_claim.items():
        await set(f"claim:lines:{cid}", items, ttl=CLAIM_CHILD_TTL)


async def get_claim_line_items(claim_id: str) -> list[dict]:
    return await get(f"claim:lines:{claim_id}") or []


async def set_claim_history(history: list[dict]) -> None:
    by_claim: dict[str, list] = {}
    for entry in history:
        cid = entry.get("claim_id")
        if cid:
            by_claim.setdefault(cid, []).append(entry)
    for cid, entries in by_claim.items():
        await set(f"claim:history:{cid}", entries, ttl=CLAIM_CHILD_TTL)


async def get_claim_history(claim_id: str) -> list[dict]:
    return await get(f"claim:history:{claim_id}") or []


async def set_claim_queries(queries: list[dict]) -> None:
    by_claim: dict[str, list] = {}
    for q in queries:
        cid = q.get("claim_id")
        if cid:
            by_claim.setdefault(cid, []).append(q)
    for cid, qs in by_claim.items():
        await set(f"claim:queries:{cid}", qs, ttl=CLAIM_CHILD_TTL)


async def get_claim_queries(claim_id: str) -> list[dict]:
    return await get(f"claim:queries:{claim_id}") or []


# -------------------------------------------------------------------------
# Insurance contracts + rates -- slow reference data
# -------------------------------------------------------------------------

CONTRACT_TTL = None


async def set_contracts(contracts: list[dict]) -> None:
    await _set_indexed("contract", contracts, ttl=CONTRACT_TTL)


async def get_all_contracts() -> list[dict]:
    return await _get_all_indexed("contract")


async def set_contract_rates(rates: list[dict]) -> None:
    by_contract: dict[str, list] = {}
    for rate in rates:
        cid = rate.get("contract_id")
        if cid:
            by_contract.setdefault(cid, []).append(rate)
    for cid, rs in by_contract.items():
        await set(f"contract:rates:{cid}", rs, ttl=CONTRACT_TTL)


async def get_contract_rates(contract_id: str) -> list[dict]:
    return await get(f"contract:rates:{contract_id}") or []


# -------------------------------------------------------------------------
# Invoice line items -- keyed by invoice_id
# -------------------------------------------------------------------------

INVOICE_LINE_TTL = None


async def set_invoice_line_items(line_items: list[dict]) -> None:
    by_invoice: dict[str, list] = {}
    for item in line_items:
        iid = item.get("invoice_id")
        if iid:
            by_invoice.setdefault(iid, []).append(item)
    for iid, items in by_invoice.items():
        await set(f"invoice:lines:{iid}", items, ttl=INVOICE_LINE_TTL)


async def get_invoice_line_items(invoice_id: str) -> list[dict]:
    return await get(f"invoice:lines:{invoice_id}") or []


# -------------------------------------------------------------------------
# Payments + entries
# -------------------------------------------------------------------------

PAYMENT_TTL = None


async def set_payments(payments: list[dict]) -> None:
    await _set_indexed("payment", payments, ttl=PAYMENT_TTL)


async def get_all_payments() -> list[dict]:
    return await _get_all_indexed("payment")


async def set_payment_entries(entries: list[dict]) -> None:
    by_payment: dict[str, list] = {}
    for entry in entries:
        pid = entry.get("payment_id")
        if pid:
            by_payment.setdefault(pid, []).append(entry)
    for pid, es in by_payment.items():
        await set(f"payment:entries:{pid}", es, ttl=PAYMENT_TTL)


async def get_payment_entries(payment_id: str) -> list[dict]:
    return await get(f"payment:entries:{payment_id}") or []


# -------------------------------------------------------------------------
# Refunds -- keyed by invoice_id for fast lookup
# -------------------------------------------------------------------------

REFUND_TTL = None


async def set_refunds(refunds: list[dict]) -> None:
    await _set_indexed("refund", refunds, ttl=REFUND_TTL)
    by_invoice: dict[str, list] = {}
    for r in refunds:
        iid = r.get("invoice_id")
        if iid:
            by_invoice.setdefault(iid, []).append(r)
    for iid, rs in by_invoice.items():
        await set(f"refund:invoice:{iid}", rs, ttl=REFUND_TTL)


async def get_all_refunds() -> list[dict]:
    return await _get_all_indexed("refund")


async def get_refunds_for_invoice(invoice_id: str) -> list[dict]:
    return await get(f"refund:invoice:{invoice_id}") or []


# -------------------------------------------------------------------------
# Payment reconciliation -- keyed by date
# -------------------------------------------------------------------------

RECONCILIATION_TTL = None


async def set_reconciliation(records: list[dict]) -> None:
    for rec in records:
        date_str = str(rec.get("reconciliation_date", ""))
        if date_str:
            await set(f"reconciliation:{date_str}", rec, ttl=RECONCILIATION_TTL)


async def get_reconciliation(date_str: str) -> dict | None:
    return await get(f"reconciliation:{date_str}")


# -------------------------------------------------------------------------
# OT helpers
# -------------------------------------------------------------------------

OT_ROOM_TTL        = None
OT_ROOM_STATUS_TTL = None
OT_SURGERY_TTL     = None
OT_SCHEDULE_TTL    = None
OT_EQUIPMENT_TTL   = None


async def set_ot_rooms(rooms: list[dict]) -> None:
    await _set_indexed("ot_room", rooms, ttl=OT_ROOM_TTL)

async def get_all_ot_rooms() -> list[dict]:
    return await _get_all_indexed("ot_room")

async def get_ot_room(room_id: str) -> dict | None:
    return await get(f"ot_room:{room_id}")


async def set_ot_room_status(statuses: list[dict]) -> None:
    await _set_indexed("ot_room_status", statuses, ttl=OT_ROOM_STATUS_TTL)

async def get_all_ot_room_status() -> list[dict]:
    return await _get_all_indexed("ot_room_status")


async def set_ot_surgeries(surgeries: list[dict]) -> None:
    await _set_indexed("ot_surgery", surgeries, ttl=OT_SURGERY_TTL)

async def get_all_ot_surgeries() -> list[dict]:
    return await _get_all_indexed("ot_surgery")

async def get_ot_surgery(surgery_id: str) -> dict | None:
    return await get(f"ot_surgery:{surgery_id}")


async def set_ot_schedule(schedule: list[dict]) -> None:
    await _set_indexed("ot_schedule", schedule, ttl=OT_SCHEDULE_TTL)

async def get_all_ot_schedule() -> list[dict]:
    return await _get_all_indexed("ot_schedule")


async def set_ot_equipment_usage(equipment: list[dict]) -> None:
    by_surgery: dict[str, list] = {}
    for item in equipment:
        sid = item.get("surgery_id")
        if sid:
            by_surgery.setdefault(sid, []).append(item)
    for sid, items in by_surgery.items():
        await set(f"ot_equipment:{sid}", items, ttl=OT_EQUIPMENT_TTL)

async def get_ot_equipment_usage(surgery_id: str) -> list[dict]:
    return await get(f"ot_equipment:{surgery_id}") or []


# -------------------------------------------------------------------------
# Discharge summary helpers
# -------------------------------------------------------------------------

DISCHARGE_SUMMARY_TTL = None


async def set_discharge_summaries(summaries: list[dict]) -> None:
    await _set_indexed("discharge_summary", summaries, ttl=DISCHARGE_SUMMARY_TTL)


async def get_all_discharge_summaries() -> list[dict]:
    return await _get_all_indexed("discharge_summary")


# -------------------------------------------------------------------------
# Discharge-ready admissions (with summary data, from /admissions/discharge-ready)
# -------------------------------------------------------------------------

DISCHARGE_READY_TTL = None


async def set_discharge_ready(admissions: list[dict]) -> None:
    await _set_indexed("discharge_ready", admissions, ttl=DISCHARGE_READY_TTL)


async def get_discharge_ready() -> list[dict]:
    return await _get_all_indexed("discharge_ready")


# -------------------------------------------------------------------------
# Bulk patient fetch by token list
# -------------------------------------------------------------------------


async def get_patients(tokens: list[str]) -> dict[str, dict]:
    if not tokens:
        return {}
    client = get_client()
    raws = await client.mget([f"patient:{t}" for t in tokens])
    result = {}
    for token, raw in zip(tokens, raws):
        if raw is not None:
            try:
                result[token] = json.loads(raw)
            except json.JSONDecodeError:
                pass
    return result


# -------------------------------------------------------------------------
# Task helpers
# -------------------------------------------------------------------------

TASK_TTL = None


async def set_tasks(tasks: list[dict]) -> None:
    await _set_indexed("task", tasks, ttl=TASK_TTL)


async def get_all_tasks() -> list[dict]:
    return await _get_all_indexed("task")


async def set_overdue_tasks(tasks: list[dict]) -> None:
    await set("task:overdue", tasks, ttl=TASK_TTL)


async def get_overdue_tasks() -> list[dict]:
    return await get("task:overdue") or []


# -------------------------------------------------------------------------
# Lab helpers
# -------------------------------------------------------------------------

LAB_TTL = None


async def set_lab_orders(orders: list[dict]) -> None:
    await _set_indexed("lab", orders, ttl=LAB_TTL)


async def get_all_lab_orders() -> list[dict]:
    return await _get_all_indexed("lab")


async def set_lab_results(results: list[dict]) -> None:
    await _set_indexed("lab_result", results, ttl=LAB_TTL)


async def get_all_lab_results() -> list[dict]:
    return await _get_all_indexed("lab_result")


async def set_lab_samples(samples: list[dict]) -> None:
    await _set_indexed("lab_sample", samples, ttl=LAB_TTL)


async def get_all_lab_samples() -> list[dict]:
    return await _get_all_indexed("lab_sample")


async def set_lab_analyzers(analyzers: list[dict]) -> None:
    await _set_indexed("lab_analyzer", analyzers, ttl=LAB_TTL)


async def get_all_lab_analyzers() -> list[dict]:
    return await _get_all_indexed("lab_analyzer")


# -------------------------------------------------------------------------
# Pharmacy helpers
# -------------------------------------------------------------------------

PHARMACY_TTL = None


async def set_pharmacy_orders(orders: list[dict]) -> None:
    await _set_indexed("pharmacy_order", orders, ttl=PHARMACY_TTL)


async def get_all_pharmacy_orders() -> list[dict]:
    return await _get_all_indexed("pharmacy_order")


async def set_pharmacy_inventory(inventory: list[dict]) -> None:
    await _set_indexed("pharmacy_inventory", inventory, ttl=PHARMACY_TTL)


async def get_all_pharmacy_inventory() -> list[dict]:
    return await _get_all_indexed("pharmacy_inventory")


# -------------------------------------------------------------------------
# Ambulance helpers
# -------------------------------------------------------------------------

AMBULANCE_TTL = None


async def set_ambulances(ambulances: list[dict]) -> None:
    import datetime as _dt
    enriched = []
    for amb in ambulances:
        if amb.get("status") == "Available" and not amb.get("available_since"):
            rid = amb.get("id")
            existing = await get(f"ambulance:{rid}") if rid else None
            # Preserve timestamp from a previous run; fall back to now() on first boot
            since = (existing or {}).get("available_since") or _dt.datetime.now(_dt.timezone.utc).isoformat()
            amb = {**amb, "available_since": since}
        enriched.append(amb)
    await _set_indexed("ambulance", enriched, ttl=AMBULANCE_TTL)


async def get_all_ambulances() -> list[dict]:
    return await _get_all_indexed("ambulance")


# -------------------------------------------------------------------------
# Session staging (pre-commit decisions, written by confirm_* activities)
# -------------------------------------------------------------------------

STAGED_TTL = 7200  # 2 hours -- user may delay clicking Commit


async def stage(session_id: str, key: str, data: Any) -> None:
    await set(f"session:{session_id}:staged:{key}", data, ttl=STAGED_TTL)


async def get_staged(session_id: str, key: str) -> Any | None:
    return await get(f"session:{session_id}:staged:{key}")


async def clear_staged(session_id: str) -> None:
    staged_keys = await keys(f"session:{session_id}:staged:*")
    for k in staged_keys:
        await delete(k)


# -------------------------------------------------------------------------
# Execution trace (human-readable step stream exposed to the frontend)
# -------------------------------------------------------------------------

TRACE_TTL = 86400  # 24h -- matches subagent_preplan; trace outlives a typical run


async def next_trace_seq(session_id: str) -> int:
    """Return the next 0-based sequence number for this session's trace.

    Backed by an atomic counter so concurrent agents never collide on ordering;
    it stays in lock-step with the trace list index (the Nth appended step has
    seq N-1). The counter shares the trace TTL.
    """
    key = f"session:{session_id}:trace_seq"
    n = int(await get_client().incr(key))
    if n == 1:
        await get_client().expire(key, TRACE_TTL)
    return n - 1


async def append_trace_step(session_id: str, step: dict) -> int:
    """Append a humanized step to the session's trace list. Returns new length."""
    key = f"session:{session_id}:trace"
    n = int(await get_client().rpush(key, json.dumps(step)))
    await get_client().expire(key, TRACE_TTL)
    return n


async def set_agent_trace_seq(session_id: str, agent_id: str, seq: int) -> None:
    """Remember the seq of an agent's `running` trace step so its later terminal
    step (completed/failed) can reuse it and upsert in place. Survives HITL
    interrupt/resume across process invocations. Shares the trace TTL."""
    key = f"session:{session_id}:agent_trace_seq"
    await get_client().hset(key, agent_id, str(seq))
    await get_client().expire(key, TRACE_TTL)


async def get_agent_trace_seq(session_id: str, agent_id: str) -> int | None:
    """Return the seq previously stamped for this agent's step, or None."""
    raw = await get_client().hget(f"session:{session_id}:agent_trace_seq", agent_id)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def get_trace(session_id: str) -> list[dict]:
    """Return the full list of humanized steps for a session, ordered by seq.

    Steps are appended in arrival order, which under concurrent agents can differ
    slightly from the seq counter (two parallel agents can append out of seq
    order). Sort by seq so the frontend gets a deterministic, stable ordering.
    """
    raws = await get_client().lrange(f"session:{session_id}:trace", 0, -1)
    out: list[dict] = []
    for r in raws:
        try:
            out.append(json.loads(r))
        except json.JSONDecodeError:
            continue
    out.sort(key=lambda s: s.get("seq", 0))
    return out


# -------------------------------------------------------------------------
# Mid-flow step recommendations (per-step recs emitted when a blocking step
# requests human input -- Phase 1 "important step"). Mirrors the trace stream.
# -------------------------------------------------------------------------

STEP_REC_TTL = 86400  # 24h -- matches trace; recs outlive a typical run


async def next_step_rec_seq(session_id: str) -> int:
    """Return the next 0-based sequence number for this session's step-rec stream.

    Backed by a DEDICATED atomic counter (not the trace counter) so step-rec and
    trace sequences never interleave. Shares the step-rec TTL.
    """
    key = f"session:{session_id}:step_rec_seq"
    n = int(await get_client().incr(key))
    if n == 1:
        await get_client().expire(key, STEP_REC_TTL)
    return n - 1


async def append_step_rec(session_id: str, rec: dict) -> int:
    """Append a per-step recommendation to the session's step-rec list. Returns new length."""
    key = f"session:{session_id}:step_recs"
    n = int(await get_client().rpush(key, json.dumps(rec)))
    await get_client().expire(key, STEP_REC_TTL)
    return n


async def get_step_recs(session_id: str) -> list[dict]:
    """Return the full list of per-step recommendations for a session, ordered by seq."""
    raws = await get_client().lrange(f"session:{session_id}:step_recs", 0, -1)
    out: list[dict] = []
    for r in raws:
        try:
            out.append(json.loads(r))
        except json.JSONDecodeError:
            continue
    out.sort(key=lambda s: s.get("seq", 0))
    return out


async def add_midflow_agent(session_id: str, agent_id: str) -> None:
    """Mark an agent as having emitted a mid-flow recommendation (drives synthesis exclusion)."""
    key = f"session:{session_id}:midflow_agents"
    await get_client().sadd(key, agent_id)
    await get_client().expire(key, STEP_REC_TTL)


async def get_midflow_agents(session_id: str) -> "set[str]":
    """Return the set of agent ids that emitted a mid-flow recommendation.

    Fail-open: on any error return an empty set so synthesis proceeds unfiltered
    (same behaviour as before Phase 1) rather than crashing.

    NOTE: this module shadows the builtin ``set`` with its own generic setter, so
    the builtin type is unavailable by name here -- build the set via a
    comprehension (which uses the real ``set``) rather than calling ``set(...)``.
    """
    try:
        members = await get_client().smembers(f"session:{session_id}:midflow_agents")
    except Exception:
        members = []
    return {m for m in members}


# -------------------------------------------------------------------------
# Execution-queue tracking (Phase 2 background-execution foundation)
#
# Two Redis SET index sets track which flows are actively executing vs waiting
# for a concurrency slot, so GET /api/queues/execution never has to KEYS-scan.
# `session:{sid}:current_step` holds the humanized step the flow is at (written
# by trace.record_step); `session:{sid}:exec_started` stamps the first moment a
# flow entered the running set (for elapsed). `sessions:paused` is reserved for
# the Phase 4 Paused queue.
# -------------------------------------------------------------------------

SESSIONS_RUNNING = "sessions:running"
SESSIONS_QUEUED  = "sessions:queued"
SESSIONS_PAUSED  = "sessions:paused"   # populated in Phase 4

CURRENT_STEP_TTL = 86400  # 24h -- matches trace; outlives a typical run


async def mark_session_queued(session_id: str) -> None:
    """Flow reached execution but is waiting for a concurrency slot."""
    await get_client().sadd(SESSIONS_QUEUED, session_id)


async def mark_session_running(session_id: str) -> None:
    """Flow acquired a slot and is now executing. Stamps exec-start once (NX)."""
    client = get_client()
    await client.srem(SESSIONS_QUEUED, session_id)
    await client.sadd(SESSIONS_RUNNING, session_id)
    await client.set(f"session:{session_id}:exec_started", str(time.time()),
                     nx=True, ex=CURRENT_STEP_TTL)


async def unmark_session_execution(session_id: str) -> None:
    """Flow finished a drive (completed, failed, or parked) -- no longer executing."""
    client = get_client()
    await client.srem(SESSIONS_RUNNING, session_id)
    await client.srem(SESSIONS_QUEUED, session_id)


async def get_running_session_ids() -> list[str]:
    return list(await get_client().smembers(SESSIONS_RUNNING))


async def get_queued_session_ids() -> list[str]:
    return list(await get_client().smembers(SESSIONS_QUEUED))


async def set_current_step(session_id: str, step: dict) -> None:
    await set(f"session:{session_id}:current_step", step, ttl=CURRENT_STEP_TTL)


async def get_current_step(session_id: str) -> dict | None:
    return await get(f"session:{session_id}:current_step")


async def get_exec_started(session_id: str) -> float | None:
    v = await get(f"session:{session_id}:exec_started")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# -------------------------------------------------------------------------
# Paused queue + cooperative pause signal (Phase 4)
#
# Two distinct concepts:
#   * `session:{sid}:pause_requested`  -- the PRE-park SIGNAL. Set by POST /pause,
#     polled by the runner's _drive astream loop each superstep; the flow parks at
#     the next step boundary (checkpoint intact) and clears it.
#   * `sessions:paused` SET            -- the POST-park STATE. The flow is now parked
#     by user request; read by GET /queues/paused and resume/cancel. No KEYS scan.
# -------------------------------------------------------------------------

PAUSE_FLAG_TTL = 86400  # 24h -- matches current_step / exec-start


async def mark_session_paused(session_id: str) -> None:
    """Flow has parked at a step boundary by user request. Leaves the execution
    index sets (it is no longer running/queued) and enters the paused set."""
    client = get_client()
    await client.srem(SESSIONS_RUNNING, session_id)
    await client.srem(SESSIONS_QUEUED, session_id)
    await client.sadd(SESSIONS_PAUSED, session_id)


async def unmark_session_paused(session_id: str) -> None:
    """Flow is no longer user-paused (resumed or cancelled)."""
    await get_client().srem(SESSIONS_PAUSED, session_id)


async def get_paused_session_ids() -> list[str]:
    return list(await get_client().smembers(SESSIONS_PAUSED))


async def request_pause(session_id: str) -> None:
    """Signal the driving flow to park at its next step boundary (cooperative)."""
    await get_client().set(f"session:{session_id}:pause_requested", "1", ex=PAUSE_FLAG_TTL)


async def is_pause_requested(session_id: str) -> bool:
    return bool(await get_client().get(f"session:{session_id}:pause_requested"))


async def clear_pause_request(session_id: str) -> None:
    await get_client().delete(f"session:{session_id}:pause_requested")


# -- Autonomous flag (Phase 5) ------------------------------------------------
# The execution graph does not carry `autonomous` in its state (it stops at the
# planning graph), so the runner stamps a per-session flag at execution launch and
# reads it on park to decide whether to apply the autonomy policy engine. A plain
# string flag (not an index set) -- it is only ever read for the single parked session.

async def mark_session_autonomous(session_id: str) -> None:
    """Record that this session's execution is running in autonomous mode."""
    await get_client().set(f"session:{session_id}:autonomous", "1", ex=PAUSE_FLAG_TTL)


async def is_session_autonomous(session_id: str) -> bool:
    return bool(await get_client().get(f"session:{session_id}:autonomous"))


# -------------------------------------------------------------------------
# Commit snapshots (optimistic writes -- revert on hospilot.sync.ack rejection)
# -------------------------------------------------------------------------

COMMIT_SNAPSHOT_TTL = 10800  # 3 hrs -- snapshot auto-expires if no hospilot.sync.ack arrives


async def save_commit_snapshot(change_id: str, redis_key: str, old_data: Any, ttl: int) -> None:
    """Snapshot current Redis value before an optimistic write so it can be reverted."""
    await set(
        f"commit:{change_id}:snapshot",
        {"redis_key": redis_key, "old_data": old_data, "ttl": ttl},
        ttl=COMMIT_SNAPSHOT_TTL,
    )


async def restore_and_delete_snapshot(change_id: str) -> None:
    """Restore the old Redis value and delete the snapshot (call on rejected ack or inline failure)."""
    snap = await get(f"commit:{change_id}:snapshot")
    if not snap:
        return
    redis_key: str = snap["redis_key"]
    old_data = snap["old_data"]
    ttl: int = snap["ttl"]
    if old_data is not None:
        await set(redis_key, old_data, ttl=ttl)
    else:
        await delete(redis_key)
    await delete(f"commit:{change_id}:snapshot")


async def delete_commit_snapshot(change_id: str) -> None:
    """Clean up snapshot after a confirmed (accepted) write."""
    await delete(f"commit:{change_id}:snapshot")


# -------------------------------------------------------------------------
# Appointment helpers
# -------------------------------------------------------------------------

APPOINTMENT_TTL = None
DOCTOR_SLOT_TTL = None


async def set_appointments(appointments: list[dict]) -> None:
    await _set_indexed("appointment", appointments, ttl=APPOINTMENT_TTL)


async def get_all_appointments() -> list[dict]:
    return await _get_all_indexed("appointment")


async def set_doctor_slots(slots: list[dict]) -> None:
    await _set_indexed("doctor_slot", slots, ttl=DOCTOR_SLOT_TTL)


async def get_all_doctor_slots() -> list[dict]:
    return await _get_all_indexed("doctor_slot")


WAITLIST_TTL = None
STAFF_ROSTER_TTL = None
VENTILATOR_TTL = None
STAFF_TTL = None


async def set_waitlist(waitlist: list[dict]) -> None:
    await _set_indexed("waitlist", waitlist, ttl=WAITLIST_TTL)


async def get_all_waitlist() -> list[dict]:
    return await _get_all_indexed("waitlist")


async def set_staff_roster(roster: list[dict]) -> None:
    await _set_indexed("staff_roster", roster, ttl=STAFF_ROSTER_TTL)


async def get_all_staff_roster() -> list[dict]:
    return await _get_all_indexed("staff_roster")


SERVICE_SLOT_TTL = None


async def set_service_slots(slots: list[dict]) -> None:
    await _set_indexed("service_slot", slots, ttl=SERVICE_SLOT_TTL)


async def get_all_service_slots() -> list[dict]:
    return await _get_all_indexed("service_slot")


# -------------------------------------------------------------------------
# Ventilator helpers
# -------------------------------------------------------------------------


async def set_ventilators(ventilators: list[dict]) -> None:
    await _set_indexed("ventilator", ventilators, ttl=VENTILATOR_TTL)


async def get_all_ventilators() -> list[dict]:
    return await _get_all_indexed("ventilator")


# -------------------------------------------------------------------------
# Staff helpers
# -------------------------------------------------------------------------


async def set_staff(staff: list[dict]) -> None:
    await _set_indexed("staff", staff, ttl=STAFF_TTL)


async def get_all_staff() -> list[dict]:
    return await _get_all_indexed("staff")
