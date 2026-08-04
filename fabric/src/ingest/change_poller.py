"""change_api ingest — poll the DB's `$changed-resources` feed (INTEGRATION_MODE=change_api).

The default ingest mode, and the one to read first. Runs as a single asyncio task
(started in main.py lifespan, only when Kafka is configured). Each cycle:

  1. FHIR feed  — GET the DB's `$changed-resources` Bundle, map each changed
     resource to an event, publish, and acknowledge the feed ONLY if every event
     published successfully (at-least-once; a failure replays the batch next cycle).
  2. REST poll  — for endpoints with no change feed (OT / ambulance / appointments),
     fetch the full list and publish only rows whose content hash changed since the
     last cycle. A row is marked seen only after its event is published.

Deletes are not published (the backend's consumer relies on TTLs — see the contract).
"""

import asyncio
import logging

from clients import fhir_client as fc
from config import settings
from ingest.content_hash import content_hash
from messaging import data_events as kafka
from ingest import topic_map

logger = logging.getLogger("poller")

# REST diff state: (entity, row_id) -> content hash of the last published row.
_seen: dict[tuple[str, str], str] = {}


async def _publish_all(events: list[tuple[str, str, dict]]) -> bool:
    """Publish every event; return True only if all succeeded."""
    ok = True
    for entity, rid, data in events:
        try:
            await kafka.publish(entity, rid, data)
        except Exception as exc:
            ok = False
            logger.error("✗ publish %s/%s failed: %s", entity, rid, str(exc)[:160])
    return ok


async def _poll_fhir_feed() -> None:
    bundle = await fc.get_changed_resources()
    entries = bundle.get("entry") or []
    if not entries:
        return

    # Upserts: PUT/POST entries carry a full resource body.
    resources = [
        e["resource"] for e in entries
        if e.get("resource") and (e.get("request") or {}).get("method") in ("PUT", "POST")
    ]
    upsert_events = topic_map.fhir_resources_to_events(resources)

    # Deletes: the DB sends resource type + id + operation=DELETE; data is null.
    delete_entries = [
        e for e in entries
        if (e.get("request") or {}).get("method") == "DELETE"
    ]

    if not upsert_events and not delete_entries:
        # All-unmapped snapshot — ack so the DB clears it.
        await fc.ack_changed_resources()
        return

    ok = True
    published = 0
    if upsert_events:
        ok = await _publish_all(upsert_events)
        if ok:
            published += len(upsert_events)

    for entry in delete_entries:
        url = (entry.get("request") or {}).get("url", "")
        for entity, rid in topic_map.fhir_delete_to_events(url):
            try:
                await kafka.publish(entity, rid, None, operation="delete")
                published += 1
            except Exception as exc:
                ok = False
                logger.error("✗ delete %s/%s failed: %s", entity, rid, str(exc)[:160])

    if ok:
        await fc.ack_changed_resources()
        logger.info("→ %d clinical change(s) published & feed acked", published)
    else:
        logger.warning("⏳ publish failures — change feed NOT acked, retrying next cycle")


async def _poll_rest() -> None:
    published = 0
    for entity, fetch in topic_map.REST_ENTITIES:
        try:
            rows = await fetch()
        except Exception as exc:
            logger.warning("REST %s fetch failed: %s", entity, str(exc)[:120])
            continue
        for row in rows:
            rid = str(row.get("id") or "")
            if not rid:
                continue
            h = content_hash(row)
            if _seen.get((entity, rid)) == h:
                continue
            try:
                await kafka.publish(entity, rid, row)
                _seen[(entity, rid)] = h     # mark seen only after a successful publish
                published += 1
            except Exception as exc:
                logger.error("✗ publish %s/%s failed: %s", entity, rid, str(exc)[:160])
    if published:
        logger.info("→ %d REST change(s) published", published)


async def run() -> None:
    interval = settings.poll_interval_ms / 1000
    logger.info("▶ change poller started  interval=%.1fs  feed=%s",
                interval, settings.ehr_fhir_base_url)
    while True:
        try:
            await _poll_fhir_feed()
            await _poll_rest()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("poll cycle error: %s", str(exc)[:200])
        await asyncio.sleep(interval)
