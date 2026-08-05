"""Kafka ack consumer -- subscribes to hospilot.sync.ack.

Fabric publishes one ack per write that the backend sent via /commit.
Ack shape:
  {
    "snapshot_id": "<session-level correlation id>",
    "change_id":   "<per-record id, matches what /commit sent in the request body>",
    "entity":      "<entity name>",
    "id":          "<record UUID>",
    "change_type": "create" | "update" | "delete",
    "status":      "accepted" | "rejected",
    "reason":      "<rejection reason or null>",
    "ts":          "<ISO timestamp>"
  }

accepted -> delete the Redis snapshot (DB confirmed; Kafka data event will arrive separately)
rejected -> restore old Redis value from snapshot so UI sees the correct state immediately
"""

import asyncio
import json
import logging

from aiokafka import AIOKafkaConsumer

from cache import redis as cache
from config import settings

logger = logging.getLogger("kafka.ack_consumer")

_task: asyncio.Task | None = None


async def _handle_ack(msg: dict) -> None:
    change_id = msg.get("change_id")
    if not change_id:
        logger.warning("ack missing change_id: %s", msg)
        return

    status = msg.get("status")
    entity = msg.get("entity", "?")
    record_id = msg.get("id", "?")

    if status == "accepted":
        await cache.delete_commit_snapshot(change_id)
        logger.debug("ack accepted  entity=%s  id=%s", entity, record_id)
    elif status == "rejected":
        await cache.restore_and_delete_snapshot(change_id)
        logger.warning(
            "ack rejected -- Redis reverted  entity=%s  id=%s  reason=%s",
            entity, record_id, msg.get("reason"),
        )
    else:
        logger.warning("ack unknown status=%s  change_id=%s", status, change_id)


async def _consumer_loop() -> None:
    topic = settings.kafka_ack_topic
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=settings.kafka_broker_list,
        group_id="hospilot-ack-handler",
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )
    await consumer.start()
    logger.info("[ok] ack consumer started  topic=%s", topic)
    try:
        async for msg in consumer:
            value = msg.value or {}
            try:
                await _handle_ack(value)
            except Exception:
                logger.warning("ack handling failed  topic=%s", msg.topic, exc_info=True)
    except asyncio.CancelledError:
        pass
    finally:
        await consumer.stop()
        logger.info("ack consumer stopped")


async def start_ack_consumer() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_consumer_loop(), name="ack-consumer")


async def stop_ack_consumer() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
