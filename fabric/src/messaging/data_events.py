"""Messages Fabric publishes INTO Hospilot — never to the hospital.

Two topics, both consumed by hospilot-backend (see agentic-framework/messaging/):

  publish()     → {prefix}.{entity}      a record changed upstream; the backend
                                         upserts it into Redis and the agents read it
  publish_ack() → hospilot.sync.ack      the HIS accepted or rejected a write we
                                         proposed; the backend releases or reverts the
                                         optimistic lock it took when it issued the write

Payload shapes here are the wire contract with hospilot-backend — see docs
KAFKA_EVENT_CONTRACT before changing any field.

The outbound counterpart (proposals leaving for the HIS) is deliberately NOT here:
it lives with the rest of the write leg, in writeback/kafka/proposal_publisher.py.
"""

from messaging import producer
from config import settings


async def publish(
    entity: str,
    record_id: str,
    data: dict | None,
    operation: str = "upsert",
    changed: list[str] | None = None,
) -> None:
    """Publish one change event. Raises on delivery failure (caller decides on ack).

    `operation` is "upsert" for full creates/updates, "delete" for removals, and
    "patch" for polling-mode field-level diffs. For "delete", `data` is None —
    consumers evict the record. For "patch", `data` carries ONLY the changed columns
    and `changed` lists their names — consumers MERGE it onto existing state rather
    than replacing the record. `changed` is omitted from the payload unless given, so
    "upsert"/"delete" events keep their exact original shape.
    """
    payload = {"entity": entity, "id": record_id, "operation": operation, "data": data}
    if changed is not None:
        payload["changed"] = changed
    await producer.send(f"{settings.kafka_topic_prefix}.{entity}", record_id, payload)


async def publish_ack(
    *,
    snapshot_id: str,
    change_id: str,
    entity: str,
    record_id: str,
    change_type: str,
    status: str,
    reason: str | None,
    ts: str,
) -> None:
    """Publish one write-proposal acknowledgement to the dedicated ack topic.

    `status` is "accepted" or "rejected". Keyed by `record_id` so a record's acks stay
    ordered. Raises on delivery failure so the $confirm caller can keep the snapshot
    locked and let the HIS retry. No-op when the producer is disabled.
    """
    await producer.send(settings.kafka_ack_topic, record_id, {
        "snapshot_id": snapshot_id,
        "change_id": change_id,
        "entity": entity,
        "id": record_id,
        "change_type": change_type,
        "status": status,
        "reason": reason,
        "ts": ts,
    })
