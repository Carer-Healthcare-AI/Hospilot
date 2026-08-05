"""API-side relay consumer for the event bus.

Reads `hospilot.sessions.events` and delivers each event to THIS process's
WebSocket connections (via api.ws.deliver_local). This is what lets events
produced on a separate Temporal worker reach the browser.

Multi-replica correctness: each API replica uses a UNIQUE consumer-group id, so
every replica receives every event (broadcast fan-out) and relays only the
connections it actually holds. (A single shared group would hand each event to
just one replica -- wrong for connection fan-out.) Separate tracing/replay
consumers use their own group + offset and are unaffected.
"""
import asyncio
import json
import logging
import os
import socket

from aiokafka import AIOKafkaConsumer

from config import settings

logger = logging.getLogger("kafka.consumer")

_consumer: AIOKafkaConsumer | None = None
_task: asyncio.Task | None = None


def _group_id() -> str:
    return f"hospilot-ws-relay-{socket.gethostname()}-{os.getpid()}"


async def _relay_loop() -> None:
    from api.routes.ws import deliver_local

    global _consumer
    _consumer = AIOKafkaConsumer(
        settings.kafka_events_topic,
        bootstrap_servers=settings.kafka_broker_list,
        group_id=_group_id(),
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        auto_offset_reset="latest",     # live UI: only relay events from now on
        enable_auto_commit=True,
    )
    await _consumer.start()
    logger.info("[ok] WS relay consumer started  topic=%s  group=%s",
                settings.kafka_events_topic, _group_id())
    try:
        async for msg in _consumer:
            env = msg.value or {}
            sid = env.get("session_id")
            event = env.get("event")
            if sid and event is not None:
                try:
                    await deliver_local(sid, event)
                except Exception:  # noqa: BLE001
                    logger.warning("WS relay delivery failed  session=%s", sid, exc_info=True)
    except asyncio.CancelledError:
        pass
    finally:
        await _consumer.stop()
        logger.info("WS relay consumer stopped")


async def start_ws_relay() -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_relay_loop(), name="ws-relay")


async def stop_ws_relay() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
