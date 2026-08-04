"""Publish approved write proposals to the HIS over Kafka.

The one place in Fabric that sends a Kafka message OUTWARD to the hospital, which is
why it lives under writeback/ rather than in messaging/ — messaging/ is Hospilot-internal
only. It borrows the shared connection from messaging/producer.py; only the topic and the
envelope shape are ours.

Used solely by writeback/kafka/write_publisher.py, the kafka-mode drain loop.
"""

from config import settings
from messaging import producer


async def publish_write_proposal(
    *,
    change_id: str,
    entity: str,
    record_id: str,
    change_type: str,
    http_method: str,
    approval_needed: bool,
    bundle: dict,
    ts: str,
) -> None:
    """Publish one approved change to the DB over Kafka (integration_mode=kafka write leg).

    The value is a thin envelope carrying a single-entry, spec-clean FHIR R5 transaction
    `bundle` (built with include_approval=False, so approval lives here in the envelope,
    not on the FHIR resource). Keyed by `record_id` (falls back to `change_id` when the
    record id isn't known yet, e.g. appointment_create) so all changes to one record land
    on one partition and stay ordered. Raises on delivery failure so the publisher loop
    keeps the change queued (at-least-once); the DB dedups by change_id. No-op when the
    producer is disabled (dev)."""
    await producer.send(settings.kafka_write_topic, record_id or change_id, {
        "change_id": change_id,
        "entity": entity,
        "id": record_id,
        "change_type": change_type,
        "http_method": http_method,
        "approval_needed": approval_needed,
        "ts": ts,
        "bundle": bundle,
    })
