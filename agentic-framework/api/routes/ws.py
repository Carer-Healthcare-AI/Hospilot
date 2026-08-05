import asyncio
import logging
from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from config import settings

router = APIRouter()
logger = logging.getLogger("ws")

# session_id -> set of connected WebSocket clients (this process only)
_connections: dict[str, set[WebSocket]] = defaultdict(set)


async def deliver_local(session_id: str, event: dict) -> None:
    """Fan an event out to THIS process's WebSocket clients for the session.

    Called directly when the Kafka bus is disabled, and by the Kafka relay
    consumer (messaging.consumer) when it is enabled.
    """
    dead = set()
    for ws in _connections.get(session_id, set()):
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _connections[session_id].discard(ws)


async def broadcast(session_id: str, event: dict) -> None:
    """Emit a session event.

    With Kafka enabled, publish to the durable event bus -- the API relay
    consumer delivers it to WebSockets (so events produced on a separate Temporal
    worker still reach the browser), and tracing/replay consumers also see it.
    Otherwise deliver straight to local WebSockets (single-process dev/tests).
    A bus hiccup never breaks execution -- we fall back to local delivery.
    """
    if event.get("type") == "alert" and "agent_id" not in event:
        from workflows.graph.exec_context import get_exec_ctx
        ctx = get_exec_ctx()
        if ctx and ctx.get("agent_id"):
            event = {**event, "agent_id": ctx["agent_id"]}
    if settings.kafka_enabled:
        try:
            from messaging.events import publish_event
            await publish_event(session_id, event)
            return
        except Exception:
            logger.warning("event bus publish failed; local fallback  session=%s",
                           session_id, exc_info=True)
    await deliver_local(session_id, event)


async def _authorize_ws(ws: WebSocket, session_id: str) -> bool:
    """Multi-tenant WS auth: a valid v2 token whose org can see this session.

    The session stream carries approval payloads and agent results, so it can't
    stay open to anyone holding a UUID. Token comes as ?token= (browser
    WebSocket API can't set an Authorization header). Close code 4401 = bad/
    missing token, 4403 = session not visible to the caller's org."""
    from api.routes.auth import _decode_token, TOKEN_VERSION
    import jwt as _jwt

    token = ws.query_params.get("token", "")
    if not token:
        await ws.close(code=4401)
        return False
    try:
        claims = _decode_token(token)
    except _jwt.InvalidTokenError:
        await ws.close(code=4401)
        return False
    if claims.get("ver") != TOKEN_VERSION:
        await ws.close(code=4401)
        return False

    role = claims.get("role", "")
    org_id = claims.get("org_id")
    try:
        from db.hasura import hasura
        if role == "super_admin":
            from workflows.graph.runner import org_of_session
            await org_of_session(session_id)   # locates it in any source (or default)
            session = True                     # super_admin may watch any session
        else:
            session = await hasura.get_session(session_id, org_id=org_id)
    except Exception:  # noqa: BLE001
        logger.warning("ws session auth lookup failed  session=%s", session_id, exc_info=True)
        session = None
    if not session:
        await ws.close(code=4403)
        return False
    return True


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(ws: WebSocket, session_id: str):
    await ws.accept()
    if not await _authorize_ws(ws, session_id):
        return
    _connections[session_id].add(ws)
    try:
        while True:
            # Keep alive -- client can send pings
            await asyncio.sleep(30)
            await ws.send_json({"type": "ping"})
    except (WebSocketDisconnect, RuntimeError):
        _connections[session_id].discard(ws)
