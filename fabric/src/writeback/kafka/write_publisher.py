"""Kafka-mode write leg — push approved changes to the DB over Kafka.

When INTEGRATION_MODE=kafka, the two-phase HTTP pull ($pending-changes/$acknowledge/
$confirm) is replaced by this background loop: it drains the same in-memory ChangeStore
the write endpoints enqueue into, resolves deferred ids (reusing bundle.resolve_changes,
the exact logic the GET handler used), builds a single-entry FHIR R5 transaction Bundle
per change, and publishes it to `hospilot.sync.write`. The DB consumes, applies, and
produces an accepted/rejected ack to `hospilot.sync.ack` in the SAME shape Fabric's HTTP
$confirm produced — so the main backend's ack consumer is unchanged and Fabric leaves the
ack loop entirely.

Delivery is at-least-once via a peek → publish → remove pattern (not drain → requeue):
a change is dropped from the queue only after its proposal is durably sent, so a crash
mid-loop re-offers it rather than losing it. The DB dedups by `change_id`. Per-record
ordering holds because the producer keys by record_id (one partition per record) and this
loop publishes in FIFO order, stopping on the first delivery failure.

No snapshot / soft-lock is ever created in this mode (Kafka's durable log replaces it).
"""

import asyncio
import logging

from config import settings
from writeback.bundle import build_snapshot_bundle, resolve_changes
from writeback.kafka import proposal_publisher
from writeback.change_store import get_change_store, now_iso

logger = logging.getLogger("kafka_write")


async def _drain_once() -> None:
    store = get_change_store()
    pending = await store.peek_pending()
    if not pending:
        return

    resolved = await resolve_changes(pending)          # fills deferred ids; drops unresolvable
    resolved_by_id = {c.change_id: c for c in resolved}
    # Changes resolve_changes dropped (target doesn't exist) never publish — drop them from
    # the queue too, matching the HTTP pull path (drain removed them there).
    done_ids: set[str] = {c.change_id for c in pending if c.change_id not in resolved_by_id}

    # Publish in FIFO order; a single-change bundle uses change_id as its snapshot_id so
    # the DB's ack (snapshot_id == change_id) is byte-identical to the pull-path ack.
    for change in resolved:
        bundle = build_snapshot_bundle([change], change.change_id, include_approval=False)
        try:
            await proposal_publisher.publish_write_proposal(
                change_id=change.change_id,
                entity=change.entity,
                record_id=change.record_id,
                change_type=change.change_type,
                http_method=change.http_method,
                approval_needed=change.approval_needed,
                bundle=bundle,
                ts=now_iso(),
            )
            done_ids.add(change.change_id)
            logger.info("→ proposed %s/%s → %s",
                        change.entity, change.record_id or change.change_id,
                        settings.kafka_write_topic)
        except Exception as exc:  # delivery failure — keep this + the rest queued, retry next tick
            logger.error("✗ propose %s/%s failed: %s — will retry",
                         change.entity, change.record_id or change.change_id, str(exc)[:160])
            break

    await store.remove(done_ids)


async def run() -> None:
    interval = settings.write_drain_interval_ms / 1000
    logger.info("▶ kafka write publisher started  interval=%.1fs  topic=%s",
                interval, settings.kafka_write_topic)
    while True:
        try:
            await _drain_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("write drain cycle error: %s", str(exc)[:200])
        await asyncio.sleep(interval)
