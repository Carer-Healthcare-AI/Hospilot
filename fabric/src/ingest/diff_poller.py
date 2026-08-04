"""Polling-mode ingest — field-level diff over the DB's per-resource APIs.

The alternative to the change_api poller (change_poller.py). Used when the DB exposes NO
`$changed-resources` change feed: Fabric polls each per-resource FHIR-compliant API
itself (the same reads it serves, in service/clinical.py), remembers the last value of
each mutable column per record, and publishes only WHAT CHANGED:

  • new record         → full row,  operation="upsert"  (same shape as change_api)
  • mutable col changed → only the changed columns, operation="patch", changed=[...]
  • unchanged          → nothing

Each entity polls on its OWN cadence (settings.poll_intervals_ms / poll_interval_rest_ms)
as an independent asyncio task, so a slow upstream for one entity never stalls the others.

The write direction (snapshot/pending-changes) is unchanged and common to both modes.
Vitals are NOT polled here (fetched at runtime). lab_result is polled via the keyset
/sync/lab_result API (FHIR labs are patient-scoped). Deletes are not emitted in polling
mode — a record leaving a filtered query is a status transition, not a delete; consumers
rely on terminal-status patches + TTL (same as the change_api REST diff).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from config import settings
from clients import sync_client
from ingest import topic_map
from service import clinical
from ingest.content_hash import content_hash
from messaging import data_events as kafka
from service import transform as tx

logger = logging.getLogger("poller")

_LAB_RESULT_MAX_PAGES = 1000           # safety cap for the keyset loop

# (entity, record_id) -> last-published projection of the tracked columns
# (clinical: {col: value}; REST full-row entities: {"__hash__": sha1}).
_seen: dict[tuple[str, str], dict] = {}


def reset_cache() -> None:
    """Drop the last-seen cache (used by tests; in prod the cache is process-lived)."""
    _seen.clear()


@dataclass(frozen=True)
class DiffEntity:
    entity: str                                      # Kafka topic suffix, e.g. "bed"
    fetch: Callable[[], Awaitable[list[dict]]]       # existing list coroutine (reused)
    mutable_cols: tuple[str, ...] | None             # tracked cols; None => full-row hash diff (REST)
    id_key: str = "id"


# ─── local fetch wrappers (reuse clinical.py; no fetch logic rewritten) ───────────
def _dedup_by_id(rows: list[dict]) -> list[dict]:
    out, seen = [], set()
    for r in rows:
        rid = r.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        out.append(r)
    return out


async def _fetch_beds() -> list[dict]:
    """Active + suspended (dirty) beds, so an Available↔Dirty transition is a status
    change on a present record rather than a disappear/reappear."""
    active = await clinical.beds()
    dirty = await clinical.dirty_beds()
    return _dedup_by_id([*active, *dirty])


async def _fetch_tasks() -> list[dict]:
    """Requested + overdue tasks (a completed task drops out of both — completion isn't
    observable in polling mode; consumers rely on TTL)."""
    inc = await clinical.incomplete_tasks()
    over = await clinical.overdue_tasks()
    return _dedup_by_id([*inc, *over])


async def _fetch_patients() -> list[dict]:
    """All patients — new arrivals publish as upserts to hospilot.data.patient.
    The backend data_consumer matches the phone field to resume paused registrations."""
    return await clinical.all_patients()


async def _fetch_lab_results() -> list[dict]:
    """Walk every keyset page of /sync/lab_result and normalize raw rows to the
    lab_result contract (same shape the change_api feed emits)."""
    rows: list[dict] = []
    cursor: str | None = None
    sync_id: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(_LAB_RESULT_MAX_PAGES):
        env = await sync_client.fetch_page("lab_result", limit=200, cursor=cursor, sync_id=sync_id)
        sync_id = sync_id or env.get("sync_id")
        rows.extend(env.get("rows") or [])
        pag = env.get("pagination") or {}
        nxt = pag.get("next_cursor")
        if not pag.get("has_more") or not nxt or nxt in seen_cursors:
            break
        seen_cursors.add(nxt)
        cursor = nxt
    return [tx.lab_result_row(r) for r in rows]


# ─── registry ────────────────────────────────────────────────────────────────────
CLINICAL_ENTITIES: list[DiffEntity] = [
    DiffEntity("bed",        _fetch_beds,                ("status", "is_active")),
    DiffEntity("admission",  clinical.all_admissions,
               ("status", "discharge_ready", "discharge_blocked_reason", "transfer_pending", "bed_id")),
    DiffEntity("visit",      clinical.er_visits,         ("status", "triage_score")),
    DiffEntity("lab_order",  clinical.lab_orders,        ("status", "priority")),
    DiffEntity("task",       _fetch_tasks,               ("status", "completed", "assigned_to")),
    DiffEntity("lab_result", _fetch_lab_results,         ("result_value", "flag", "reported_at")),
    # Full-row hash diff — publishes upsert on hospilot.data.patient for every new patient.
    # The backend data_consumer matches the phone field to resume paused registration flows.
    DiffEntity("patient",    _fetch_patients,            None),
]


def registry() -> list[DiffEntity]:
    """Clinical entities (field-delta) + REST entities (full-row hash diff, reused from
    topic_map.REST_ENTITIES so their contract is identical to change_api mode)."""
    rest = [DiffEntity(entity, fetch, None) for entity, fetch in topic_map.REST_ENTITIES]
    return [*CLINICAL_ENTITIES, *rest]


# ─── diff + publish ────────────────────────────────────────────────────────────────
def _project(e: DiffEntity, row: dict) -> dict:
    if e.mutable_cols is None:
        return {"__hash__": content_hash(row)}
    return {c: row.get(c) for c in e.mutable_cols}


async def _maybe_discharge_ready(entity: str, rid: str, row: dict) -> None:
    """Mirror the change_api fan-out: an admission with discharge_ready=true also
    publishes the full row to the discharge_ready topic (topic_map _map_single)."""
    if entity == "admission" and row.get("discharge_ready"):
        await kafka.publish("discharge_ready", rid, row, operation="upsert")


async def _emit_diff(e: DiffEntity, row: dict) -> bool:
    """Publish a full upsert (new / REST change) or a field-level patch (clinical change).
    Returns True iff an event was published. Cache is updated only after a successful
    publish (at-least-once — a failure replays the record next cycle)."""
    rid = str(row.get(e.id_key) or "")
    if not rid:
        return False
    key = (e.entity, rid)
    prev = _seen.get(key)
    proj = _project(e, row)
    if prev == proj:
        return False
    try:
        if prev is None or e.mutable_cols is None:
            # new record (cold start counts as new), or a REST full-row entity → full row
            await kafka.publish(e.entity, rid, row, operation="upsert")
        else:
            changed = [c for c in e.mutable_cols if proj.get(c) != prev.get(c)]
            await kafka.publish(e.entity, rid, {c: proj[c] for c in changed},
                                operation="patch", changed=changed)
        await _maybe_discharge_ready(e.entity, rid, row)
    except Exception as exc:
        logger.error("✗ publish %s/%s failed: %s", e.entity, rid, str(exc)[:160])
        return False
    _seen[key] = proj
    return True


async def _poll_entity(e: DiffEntity) -> None:
    rows = await e.fetch()
    published = 0
    for row in rows:
        if await _emit_diff(e, row):
            published += 1
    if published:
        logger.info("→ [%s] %d change(s) published", e.entity, published)


# ─── scheduling: one task per entity, each on its own cadence ──────────────────────
def _interval_s(e: DiffEntity) -> float:
    if e.mutable_cols is None:
        ms = settings.poll_interval_rest_ms
    else:
        ms = settings.poll_intervals_ms.get(e.entity, settings.poll_interval_ms)
    return ms / 1000


async def _run_entity(e: DiffEntity) -> None:
    interval = _interval_s(e)
    logger.info("▶ diff poll [%s] every %.1fs  (cold start republishes full rows)", e.entity, interval)
    while True:
        try:
            await _poll_entity(e)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[%s] poll cycle error: %s", e.entity, str(exc)[:200])
        await asyncio.sleep(interval)


async def run() -> None:
    """Spawn one poll loop per entity; cancel them all together on shutdown."""
    entities = registry()
    logger.info("▶ diff poller started (polling mode) — %d entities, per-entity cadence", len(entities))
    tasks = [asyncio.create_task(_run_entity(e), name=f"diff-{e.entity}") for e in entities]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
