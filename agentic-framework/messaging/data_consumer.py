"""Kafka data consumer — subscribes to fabric data-change topics, updates Redis.

Message format (confirmed with Fabric team 2026-06-12):
  {
    "entity":    "<entity_name>",       # matches topic suffix
    "id":        "<record_id>",         # bare UUID, also the Kafka message key
    "operation": "upsert" | "delete",
    "data":      { ... } | null         # full row for upsert; null for delete
  }

Topic structure: one topic per entity — settings.kafka_data_topic_prefix + "." + entity
Delivery: at-least-once. Upserts/deletes are idempotent by id, so re-delivery is safe.

Delete support: only FHIR-backed entities emit delete events (bed, admission, visit,
lab_result, lab_order, task). REST-backed entities and vital/discharge_ready are
upsert-only per the Fabric contract.
"""

import asyncio
import datetime
import json
import logging

from aiokafka import AIOKafkaConsumer

from cache import redis as cache
from config import settings

logger = logging.getLogger("kafka.data_consumer")

_task: asyncio.Task | None = None

# One topic per entity (confirmed with Fabric team). Flip to True only if Fabric
# switches to a single multiplexed topic with an `entity` field in the body.
_MULTIPLEXED = False

# ---------------------------------------------------------------------------
# Routing table: entity name → (redis prefix, TTL)
# Matches exactly the 15 topics Fabric publishes (KAFKA_PUBLISHER_INTEGRATION.md §3).
# Add rows here when Fabric adds new topics (e.g. pharmacy_order).
# ---------------------------------------------------------------------------
_ROUTES: dict[str, tuple[str, int]] = {
    # FHIR-backed entities (change feed)
    "bed":                  ("bed",                cache.BED_TTL),
    "admission":            ("admission",          cache.ADMISSION_TTL),
    "discharge_ready":      ("discharge_ready",    cache.DISCHARGE_READY_TTL),
    "visit":                ("visit",              cache.VISIT_TTL),
    "lab_result":           ("lab_result",         cache.LAB_TTL),
    "lab_order":            ("lab",                cache.LAB_TTL),
    "lab_sample":           ("lab_sample",         cache.LAB_TTL),
    "lab_analyzer":         ("lab_analyzer",       cache.LAB_TTL),
    "pharmacy_order":       ("pharmacy_order",     cache.PHARMACY_TTL),
    "pharmacy_inventory":   ("pharmacy_inventory", cache.PHARMACY_TTL),
    "task":                 ("task",               cache.TASK_TTL),
    # REST-polled entities (diff-detected by Fabric)
    "ot_room":              ("ot_room",            cache.OT_ROOM_TTL),
    "ot_room_status":       ("ot_room_status",     cache.OT_ROOM_STATUS_TTL),
    "ot_surgery":           ("ot_surgery",         cache.OT_SURGERY_TTL),
    "ot_schedule":          ("ot_schedule",        cache.OT_SCHEDULE_TTL),
    "ambulance":            ("ambulance",          cache.AMBULANCE_TTL),
    "appointment":          ("appointment",        cache.APPOINTMENT_TTL),
    "doctor_slot":          ("doctor_slot",        cache.DOCTOR_SLOT_TTL),
    # Operational tables (change_api mode: raw pass-through via fabricSync.js)
    "ventilator":           ("ventilator",         cache.VENTILATOR_TTL),
    "staff_roster":         ("staff_roster",       cache.STAFF_ROSTER_TTL),
    "staff":                ("staff",              cache.STAFF_TTL),
}


def _topics() -> list[str]:
    prefix = settings.kafka_data_topic_prefix
    if _MULTIPLEXED:
        return [prefix]
    # `patient` is not part of the agent Redis projection (no _ROUTES row); we subscribe
    # to it only to wake flows paused on patient registration (see _maybe_resume_registration).
    return [f"{prefix}.{entity}" for entity in _ROUTES] + [f"{prefix}.vital", f"{prefix}.patient"]


async def _maybe_resume_registration(data: dict) -> None:
    """A `patient` upsert arrived from Fabric. If a flow is paused waiting for THIS
    patient to be registered, wake it. The match + all-registered check live in
    graph.patient; we only need to fire the resume once it returns a session id."""
    mobile = data.get("mobile") or data.get("phone") or data.get("mobile_number") or data.get("contact_number")
    if not mobile:
        return
    from workflows.graph import patient
    sid = await patient.record_registration_and_check(mobile)
    if not sid:
        return
    from workflows.graph.runner import resume_patient_registration
    await resume_patient_registration(sid, {"status": "registered", "mobile": mobile})
    logger.info("\033[1m[KAFKA] patient registered -- resuming session=%s\033[0m", sid)


async def _enrich_ambulance(record_id: str, data: dict) -> dict:
    """Inject available_since when a unit transitions into Available status.

    If the DB already provides available_since (post-migration), pass through as-is.
    Otherwise compute it from the status transition detected against the current Redis value.
    """
    if data.get("status") != "Available":
        return data
    if data.get("available_since"):
        # DB-provided — post-migration, nothing to compute
        return data

    current = await cache.get(f"ambulance:{record_id}")
    if current is None or current.get("status") != "Available":
        # First time Available (new record or status change): stamp now
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return {**data, "available_since": now}
    else:
        # Already Available in Redis — preserve the existing timestamp
        existing = current.get("available_since")
        return {**data, "available_since": existing} if existing else data


async def _handle(entity: str, record_id: str, operation: str, data: dict | None) -> None:
    """Route a single change event to the right Redis operation."""

    # Vitals are sensitive runtime data. Do not store vital:* in Redis; agents
    # fetch them live through Fabric (/vitals/latest, /vitals/critical).
    if entity == "vital":
        logger.info("\033[1m[KAFKA] SKIP   entity=vital  reason=api_only\033[0m")
        return

    # patient: not cached in the agent projection -- consumed only to resume a flow
    # paused on patient registration (the DB just created a previously-unknown patient).
    if entity == "patient":
        if operation == "upsert" and data:
            await _maybe_resume_registration(data)
        return

    route = _ROUTES.get(entity)
    if route is None:
        logger.warning("unknown entity in data event: %s", entity)
        return

    prefix, ttl = route

    if operation == "delete":
        await cache.delete_indexed(prefix, record_id, ttl)
        logger.info("\033[1m[KAFKA] DELETE  entity=%s  id=%s\033[0m", entity, record_id)
    else:
        if not data:
            logger.warning("upsert with null data  entity=%s  id=%s", entity, record_id)
            return
        if entity == "ambulance":
            data = await _enrich_ambulance(record_id, data)
        await cache.upsert_indexed(prefix, data, ttl)
        logger.info("\033[1m[KAFKA] UPSERT  entity=%s  id=%s\033[0m", entity, record_id)

    # Nudge the advisory engine (workflows/graph/advisory.py): rules with this
    # entity in trigger_entities are evaluated on its next wake. No-op where the
    # engine isn't running.
    try:
        from workflows.graph.advisory import notify_entity_change
        notify_entity_change(entity)
    except Exception:  # noqa: BLE001
        pass


async def _consumer_loop() -> None:
    topics = _topics()
    consumer = AIOKafkaConsumer(
        *topics,
        bootstrap_servers=settings.kafka_broker_list,
        group_id="hospilot-data-sync",
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        # latest: run_initial_sync() seeds Redis on startup;
        # we only need events from this point forward.
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )
    await consumer.start()
    logger.info("[ok] data consumer started  topics=%s", topics)
    try:
        async for msg in consumer:
            value = msg.value or {}
            try:
                record_id = value.get("id") or ""
                operation = value.get("operation", "upsert")
                data      = value.get("data")
                if _MULTIPLEXED:
                    entity = value.get("entity", "")
                else:
                    entity = msg.topic.rsplit(".", 1)[-1]
                await _handle(entity, record_id, operation, data)
            except Exception:
                logger.warning("data event handling failed  topic=%s", msg.topic, exc_info=True)
    except asyncio.CancelledError:
        pass
    finally:
        await consumer.stop()
        logger.info("data consumer stopped")


async def start_data_consumer() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_consumer_loop(), name="data-consumer")


async def stop_data_consumer() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
