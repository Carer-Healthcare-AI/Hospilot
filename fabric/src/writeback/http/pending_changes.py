"""FHIR R5 snapshot API — the two-phase, soft-locked pending-changes exchange.

The DB drains queued writes in three steps:

  1. GET  /fhir/Bundle/$pending-changes
        Mints a snapshot_id, resolves deferred ids, and returns the queued changes as a
        FHIR R5 transaction Bundle (one entry per change, `fullUrl` = change id). The
        snapshot becomes the single in-flight one; re-pulling returns the SAME snapshot.

  2. POST /fhir/Bundle/$pending-changes/$acknowledge   {snapshot_id}
        Receipt — the DB has durably received the Bundle. Fabric marks it locked (soft
        lock held). The queue is NOT cleared here.

  3. POST /fhir/Bundle/$pending-changes/$confirm
        {snapshot_id, results: [{change_id, status, reason?, assigned_id?}]}
        The DB reports accepted/rejected per change. Fabric publishes one ack event per
        change to Kafka (so the backend reconciles its internal DB + releases its lock), then
        clears the snapshot (releases the soft lock).

If the DB never confirms within SNAPSHOT_LOCK_TIMEOUT_MS, the lock expires and the
changes are re-offered on the next pull (at-least-once). Rejected changes are published
as "rejected" and dropped — no retry.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import settings
from writeback.bundle import build_snapshot_bundle, resolve_changes
from messaging import data_events, producer
from writeback.change_store import SnapshotError, get_change_store, now_iso

logger = logging.getLogger("fhir_api")


def _guard_pull_disabled() -> None:
    """In kafka write mode the DB no longer pulls — proposals are pushed to
    `hospilot.sync.write` by the write publisher. Reject the pull endpoints with 409 so
    they can't race the publisher loop for the same in-memory queue. Guarded on
    producer.enabled() too, so the kafka-mode-but-Kafka-disabled dev case keeps the pull
    active as the only write exit."""
    if settings.kafka_mode and producer.enabled():
        raise HTTPException(
            status_code=409,
            detail=("pull disabled: INTEGRATION_MODE=kafka — proposals are pushed to "
                    f"{settings.kafka_write_topic}; consume that topic instead"),
        )


router = APIRouter(dependencies=[Depends(_guard_pull_disabled)])

_FHIR_MEDIA_TYPE = "application/fhir+json"

_EMPTY_BUNDLE = {"resourceType": "Bundle", "type": "transaction", "entry": []}


class AckBody(BaseModel):
    snapshot_id: str


class ChangeResult(BaseModel):
    change_id: str
    status: str                       # "accepted" | "rejected"
    reason: str | None = None
    assigned_id: str | None = None    # id the DB assigned to a POST-created resource


class ConfirmBody(BaseModel):
    snapshot_id: str
    results: list[ChangeResult]


@router.get("/fhir/Bundle/$pending-changes")
async def get_pending_changes():
    store = get_change_store()
    # Idempotent re-pull: an in-flight (offered/locked) snapshot is returned unchanged,
    # unless it has expired (its changes are then re-queued for a fresh offer).
    snap = await store.current_inflight(settings.snapshot_lock_timeout_s)
    if snap is None:
        pending = await store.drain_pending()
        resolved = await resolve_changes(pending)        # async DB lookups, outside the store lock
        snap = await store.commit_inflight(resolved)
        if snap is None:
            return JSONResponse(content=_EMPTY_BUNDLE, media_type=_FHIR_MEDIA_TYPE)
    bundle = build_snapshot_bundle(snap.changes, snap.snapshot_id)
    return JSONResponse(content=bundle, media_type=_FHIR_MEDIA_TYPE)


@router.post("/fhir/Bundle/$pending-changes/$acknowledge")
async def acknowledge_snapshot(body: AckBody):
    """Receipt: the DB has durably received the snapshot. Holds the soft lock."""
    try:
        await get_change_store().mark_locked(body.snapshot_id)
    except SnapshotError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "snapshot_id": body.snapshot_id, "state": "locked"}


@router.post("/fhir/Bundle/$pending-changes/$confirm")
async def confirm_snapshot(body: ConfirmBody):
    """The DB reports accepted/rejected per change. Publish acks, then release the lock.

    On a publish failure the snapshot is left locked and a 502 is returned so the DB can
    retry $confirm (the backend ack consumer is idempotent)."""
    store = get_change_store()
    try:
        changes = await store.inflight_changes(body.snapshot_id)
    except SnapshotError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    by_id = {c.change_id: c for c in changes}
    ts = now_iso()
    published = 0
    failures: list[str] = []
    for result in body.results:
        change = by_id.get(result.change_id)
        if change is None:
            logger.warning("$confirm: unknown change_id %s for snapshot %s",
                           result.change_id, body.snapshot_id)
            continue
        record_id = result.assigned_id or change.record_id
        try:
            await data_events.publish_ack(
                snapshot_id=body.snapshot_id,
                change_id=change.change_id,
                entity=change.entity,
                record_id=record_id,
                change_type=change.change_type,
                status=result.status,
                reason=result.reason,
                ts=ts,
            )
            published += 1
        except Exception as exc:  # delivery failure — keep lock, let the DB retry
            failures.append(change.change_id)
            logger.error("✗ ack publish %s/%s failed: %s",
                         change.entity, record_id, str(exc)[:160])

    if failures:
        # Lock NOT released — the same snapshot stays in flight so the DB can retry
        # $confirm with the same snapshot_id (the ack consumer is idempotent).
        raise HTTPException(
            status_code=502,
            detail=f"{len(failures)} ack publish(es) failed; snapshot kept locked for retry",
        )

    await store.release(body.snapshot_id)
    logger.info("→ snapshot %s confirmed: %d ack(s) published & lock released",
                body.snapshot_id, published)
    return {"ok": True, "snapshot_id": body.snapshot_id, "published": published}
