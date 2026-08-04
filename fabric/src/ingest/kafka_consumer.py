"""Kafka-mode ingest — event-triggered fetch-and-publish.

When INTEGRATION_MODE=kafka, replaces the change_api / diff pollers.
Subscribes to hospilot.changes.* (published by CarerOS Hasura event triggers),
fetches the updated resource via FHIR (where possible), transforms it using the
same tx.* functions the existing pollers use, and publishes to hospilot.data.*.

Event payloads are used DIRECTLY wherever a raw-row mapper exists (service.transform
`*_row`), so a cached entity costs no extra HTTP read — the event already carries the
row. A re-read is issued only when there is no row mapper for the entity, when the
event carried no `data`, or when the mapped row fails its completeness check (a field
that lives on a join and therefore cannot come from a single-table payload).
"""

import asyncio
import json
import logging

from aiokafka import AIOKafkaConsumer

from clients import fhir_client as fc
from config import settings
from messaging import data_events as kafka
from service import transform as tx

logger = logging.getLogger("kafka_consumer")

_CHANGE_TOPICS = [
    f"hospilot.changes.{e}" for e in [
        "bed", "admission", "ambulance", "dept",
        "discharge_summary", "lab", "lab_result",
        "lab_sample", "lab_analyzer", "pharmacy_order",
        "pharmacy_inventory", "ot_room", "ot_room_status",
        "ot_schedule", "ot_surgery", "task", "vital",
    ]
]


# entity -> raw-row mapper. Present here == the event payload is authoritative and no
# HTTP read is issued. Mirrors the normalized contracts in service.transform.
_ROW_MAPPERS = {
    "bed":                tx.bed_row,
    "admission":          tx.admission_row,
    "visit":              tx.visit_row,
    "lab":                tx.lab_order_row,
    "lab_result":         tx.lab_result_row,
    "task":               tx.nursing_task_row,
    "lab_sample":         tx.lab_sample_row,
    "lab_analyzer":       tx.lab_analyzer_row,
    "pharmacy_order":     tx.pharmacy_order_row,
    "pharmacy_inventory": tx.pharmacy_inventory_row,
}

# Fields that cannot be derived from a single-table payload (they live on a relation in
# the FHIR projection). If the mapped row leaves one empty we fall back to a re-read
# rather than cache a half-populated record.
_REQUIRED_FIELDS = {
    "lab":            ("test_name",),   # lab_orders -> lab_results relation
    "pharmacy_order": ("medication",),  # may be an FK to a drug master
}


def _from_payload(entity: str, raw_row: dict | None) -> dict | None:
    """Map the event's own payload onto the normalized contract, or None if it cannot
    be trusted (no mapper, no payload, no id, or a join-sourced field missing)."""
    mapper = _ROW_MAPPERS.get(entity)
    if mapper is None or not isinstance(raw_row, dict) or not raw_row:
        return None
    try:
        row = mapper(raw_row)
    except Exception as exc:
        logger.warning("row-map %s failed: %s — falling back to a read", entity, str(exc)[:120])
        return None
    if not row.get("id"):
        return None
    missing = [f for f in _REQUIRED_FIELDS.get(entity, ()) if not row.get(f)]
    if missing:
        logger.info("payload for %s/%s lacks %s — re-reading", entity, row["id"], ",".join(missing))
        return None
    return row


async def _fetch_normalized(entity: str, record_id: str, raw_row: dict | None) -> dict | None:
    """Return the normalized dict for one record.

    Preference order: the event's own payload (no network call) -> a targeted read of
    that one record -> the raw row as-is."""
    from_payload = _from_payload(entity, raw_row)
    if from_payload is not None:
        return from_payload
    try:
        if entity == "bed":
            loc = await fc.read_location(f"bed-{record_id}")
            return tx.bed(loc) if loc else None

        if entity == "admission":
            enc = await fc.read_encounter(f"ipd-{record_id}")
            return tx.admission(enc) if enc else None

        if entity == "dept":
            orgs = await fc.search_organizations({"_id": record_id})
            return tx.department(orgs[0]) if orgs else None

        if entity == "lab_result":
            obs = await fc.read_observation(f"lab-{record_id}")
            return tx.lab_result(obs) if obs else None

        if entity == "lab":
            srs = await fc.search_service_requests({"_id": record_id})
            return tx.lab_order(srs[0]) if srs else None

        if entity == "task":
            tasks = await fc.search_tasks({"_id": record_id})
            return tx.nursing_task(tasks[0]) if tasks else None

        if entity == "lab_sample":
            specs = await fc.search_specimens({"_id": record_id})
            return tx.lab_sample(specs[0]) if specs else None

        if entity == "lab_analyzer":
            devs = await fc.search_devices({"_id": record_id})
            return tx.lab_analyzer(devs[0]) if devs else None

        if entity == "pharmacy_order":
            meds = await fc.search_medication_requests({"_id": record_id})
            return tx.pharmacy_order(meds[0]) if meds else None

        if entity == "pharmacy_inventory":
            items = await fc.search_inventory_items({"_id": record_id})
            return tx.pharmacy_inventory(items[0]) if items else None

        # REST-backed entities (ambulance, ot_*, discharge_summary, vital):
        # the raw DB row is published as-is — same approach as the REST diff poller.
        return raw_row

    except Exception as exc:
        logger.warning("fetch %s/%s failed: %s — using raw row", entity, record_id, exc)
        return raw_row


# The DB's change feed calls this entity "lab" (matching hospilot.changes.lab),
# but the rest of Fabric's ingest (topic_map.py, diff_poller.py) and the backend's
# data_consumer._ROUTES key it as "lab_order". Remap on publish so kafka mode
# lands on the same hospilot.data.* topic the other two modes use.
_PUBLISH_ENTITY = {"lab": "lab_order"}


async def _handle(entity: str, record_id: str, operation: str, raw_row: dict | None) -> None:
    publish_entity = _PUBLISH_ENTITY.get(entity, entity)

    if operation == "delete":
        await kafka.publish(publish_entity, record_id, None, operation="delete")
        return

    data = await _fetch_normalized(entity, record_id, raw_row)
    if data is None:
        logger.warning("no data resolved for %s/%s — skipping publish", entity, record_id)
        return

    await kafka.publish(publish_entity, record_id, data)
    logger.info("published %s/%s → hospilot.data.%s", entity, record_id, publish_entity)

    # Mirror the discharge_ready fan-out that both existing pollers do
    if entity == "admission" and data.get("discharge_ready"):
        await kafka.publish("discharge_ready", record_id, data)


async def run() -> None:
    consumer = AIOKafkaConsumer(
        *_CHANGE_TOPICS,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id="hospilot-fabric-changes",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        auto_offset_reset="latest",
    )
    await consumer.start()
    logger.info(
        "▶ kafka consumer started — %d hospilot.changes.* topics", len(_CHANGE_TOPICS)
    )
    try:
        async for msg in consumer:
            try:
                payload   = msg.value
                entity    = payload.get("entity")
                record_id = payload.get("id")
                operation = payload.get("operation", "upsert")
                raw_row   = payload.get("data")
                if not entity or not record_id:
                    continue
                await _handle(entity, record_id, operation, raw_row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("message handling error on %s: %s", msg.topic, exc)
    except asyncio.CancelledError:
        pass
    finally:
        await consumer.stop()
        logger.info("kafka consumer stopped")
