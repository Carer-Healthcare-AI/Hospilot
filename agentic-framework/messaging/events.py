"""Event/replay bus.

Every WebSocket event emitted by `broadcast()` is published here as a durable,
replayable Kafka record so that (a) the API process can relay it to the browser's
WebSocket even when the producing code ran on a separate Temporal worker, and
(b) frontend/legacy tracing + replay consumers can read the full per-session
event history.

Topic: settings.kafka_events_topic (default "hospilot.sessions.events").
Key:   session_id  -> all events for a session land on one partition, in order.
Value: {session_id, ts, event}  where `event` is the exact WS dict.
"""
import logging
from datetime import datetime, timezone

from config import settings
from messaging.producer import publish

logger = logging.getLogger("kafka.events")


async def publish_event(session_id: str, event: dict) -> None:
    envelope = {
        "session_id": session_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    await publish(settings.kafka_events_topic, envelope, key=session_id)
