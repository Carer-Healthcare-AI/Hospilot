"""Shared aiokafka producer (restored from the kafka_for_execution branch).

Used by the event/replay bus (messaging.events). A single producer is created per
process (the API process AND the Temporal worker each get their own) and reused.
"""
import json
import logging

from aiokafka import AIOKafkaProducer

from config import settings

logger = logging.getLogger("kafka")
_producer: AIOKafkaProducer | None = None
_unavailable = False  # sticky: once start() fails, fail fast (never poison _producer / hang)


async def get_producer() -> AIOKafkaProducer:
    """Lazily start the shared producer. Raises if Kafka is unavailable so callers
    fall back to local WS delivery. Bootstrap is attempted only ONCE per process; on
    failure _producer is left None (never a half-built, unstarted instance, whose
    send_and_wait would block until the delivery timeout instead of failing)."""
    global _producer, _unavailable
    if _unavailable:
        raise RuntimeError("kafka event bus unavailable (bootstrap failed earlier)")
    if _producer is None:
        p = AIOKafkaProducer(
            bootstrap_servers=settings.kafka_broker_list,
            value_serializer=lambda v: json.dumps(v, default=str).encode("utf-8"),
            key_serializer=lambda k: k.encode("utf-8") if k is not None else None,
        )
        try:
            await p.start()
        except Exception:
            _unavailable = True
            logger.warning("Kafka unavailable -- event bus disabled for this process; "
                           "broadcasts use local WS fallback")
            raise
        _producer = p  # assign to the global ONLY after a successful start
        logger.info("[ok] Kafka producer connected  brokers=%s", settings.kafka_brokers)
    return _producer


async def publish(topic: str, payload: dict, key: str | None = None) -> None:
    """Publish a payload. `key` (e.g. session_id) pins related messages to one
    partition so their relative order is preserved."""
    producer = await get_producer()
    await producer.send_and_wait(topic, payload, key=key)


async def close_producer() -> None:
    global _producer
    if _producer is not None:
        await _producer.stop()
        _producer = None
        logger.info("Kafka producer closed")
