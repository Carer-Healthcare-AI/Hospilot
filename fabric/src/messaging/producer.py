"""The single Kafka producer connection, shared by everything that publishes.

Only the connection lives here — no topic names, no payload shapes. Two callers build
messages on top of it, in opposite directions:

  messaging/data_events.py             → hospilot-backend  (data changes, write acks)
  writeback/kafka/proposal_publisher.py → the HIS          (approved write proposals)

Disabled (no-op) when KAFKA_BOOTSTRAP_SERVERS is unset, so Fabric runs without Kafka in
dev: the REST APIs still serve, only the change stream is off. `send()` raises on
delivery failure, which is what lets change_poller decide whether it may acknowledge the
HIS's change feed (at-least-once: no ack unless every event landed).

Lifecycle is owned by main.py's lifespan — start() once on boot, stop() on shutdown.
"""

import json
import logging

from config import settings

logger = logging.getLogger("kafka")

_producer = None


def enabled() -> bool:
    return settings.kafka_enabled


async def start() -> None:
    global _producer
    if not enabled():
        logger.info("✓ Kafka publishing OFF (no KAFKA_BOOTSTRAP_SERVERS)")
        return
    from aiokafka import AIOKafkaProducer

    _producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        client_id=settings.kafka_client_id,
        acks="all",
        enable_idempotence=True,
    )
    await _producer.start()
    logger.info("✓ Kafka producer connected  servers=%s prefix=%s",
                settings.kafka_bootstrap_servers, settings.kafka_topic_prefix)


async def stop() -> None:
    global _producer
    if _producer is not None:
        await _producer.stop()
        _producer = None


async def send(topic: str, key: str, payload: dict) -> None:
    """Publish one JSON message, keyed so a record's messages stay ordered.

    Keying by record id puts all of a record's messages on one partition, which is what
    preserves per-record ordering across the whole platform. Raises on delivery failure;
    no-op when the producer is disabled.
    """
    if _producer is None:
        return
    await _producer.send_and_wait(
        topic,
        value=json.dumps(payload, default=str).encode("utf-8"),
        key=str(key).encode("utf-8"),
    )
