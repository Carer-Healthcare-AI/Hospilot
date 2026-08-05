"""Mid-flow per-step recommendations (Phase 1 "important step").

When a blocking step requests human input (parks at an `hitl.await_decision()`
interrupt), we emit a standalone recommendation *at that moment* rather than
waiting for the final synthesis to bundle everything at the end. Each emission:

  1. persists a record to a per-session Redis list (`cache.append_step_rec`), so a
     reconnecting / late-joining client can fetch the whole stream via
     `GET /api/sessions/{id}/step-recommendations`, and
  2. (for action-approval steps) marks the agent in a Redis set
     (`cache.add_midflow_agent`) so the terminal `synthesise_node` EXCLUDES it from
     the final recommendation -- items handled mid-flow are not re-surfaced, and
  3. broadcasts a `{"type": "step_recommendation", ...}` event over the existing
     WebSocket channel for live display.

The persisted record and the broadcast event carry the SAME payload. Patient
identification/registration steps emit for visibility but pass
`exclude_from_synthesis=False` -- they are prerequisite input steps, not action
recommendations the final synthesis would re-surface.

Fully defensive: a step-rec failure must never break the run (mirrors trace.py).
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


async def emit_step_recommendation(
    session_id: str,
    *,
    agent_id: str,
    headline: str,
    actions: list[str],
    rationale: str,
    kind: str,
    risk: str = "medium",
    extras: dict | None = None,
    exclude_from_synthesis: bool = True,
) -> None:
    """Emit + persist a per-step recommendation at a blocking interrupt point.

    Persists to Redis and (when `exclude_from_synthesis`) marks the agent for
    synthesis exclusion BEFORE broadcasting, so the exclusion set stays
    authoritative even if the WebSocket send fails. Never raises.
    """
    if not session_id:
        return
    try:
        from cache import redis as cache
        from api.routes.ws import broadcast

        record = {
            "seq":       await cache.next_step_rec_seq(session_id),
            "ts":        time.time(),
            "agent_id":  agent_id,
            "kind":      kind,
            "headline":  headline,
            "actions":   actions or [],
            "rationale": rationale,
            "risk":      risk,
            **(extras or {}),
        }
        await cache.append_step_rec(session_id, record)
        if exclude_from_synthesis:
            await cache.add_midflow_agent(session_id, agent_id)
        await broadcast(session_id, {"type": "step_recommendation", **record})
    except Exception:  # noqa: BLE001 -- tracing must never break the run
        logger.warning("step_recommendation emit failed  session=%s  agent=%s",
                       session_id, agent_id, exc_info=True)
