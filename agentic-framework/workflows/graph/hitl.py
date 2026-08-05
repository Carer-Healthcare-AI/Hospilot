"""Human-in-the-loop approval via LangGraph interrupt() -- replaces the Temporal
`decide` signal + workflow.wait_condition pattern.

THE RE-EXECUTION GOTCHA: a LangGraph node that calls interrupt() re-runs from the
top when resumed. To avoid (a) re-running the expensive pre-approval work, (b)
creating a duplicate Hasura approval row, and (c) emitting duplicate WS events,
each approval agent body is structured as:

    pending = await hitl.load_pending(session_id, base)
    if pending is not None:                 # RESUME PATH -- no re-work
        decision = hitl.await_decision({...})
        await hitl.clear_pending(session_id, base)
        return await _finalize(pending, decision)
    ... first-run work; create approval row; ...
    await hitl.save_pending(session_id, base, small_payload)
    decision = hitl.await_decision({...})   # raises GraphInterrupt on first run

The pending record (small, JSON-serialisable) is kept in Redis so it survives the
interrupt/checkpoint boundary and is available when the node re-runs on resume.

Resume is driven from /api/approvals/{id}/decide via Command(resume=decision) with
thread_id == session_id. The 30-min timeout (which Temporal handled internally) is
provided externally by the approval reaper resuming with decision="timeout".
"""

import logging

from langgraph.types import interrupt

from cache import redis as cache

logger = logging.getLogger(__name__)

_PENDING_TTL = 3600  # 1 hour -- longer than the 30-min approval window


def _key(session_id: str, agent_base: str) -> str:
    return f"session:{session_id}:hitl:{agent_base}"


async def load_pending(session_id: str, agent_base: str) -> dict | None:
    return await cache.get(_key(session_id, agent_base))


async def save_pending(session_id: str, agent_base: str, data: dict) -> None:
    await cache.set(_key(session_id, agent_base), data, ttl=_PENDING_TTL)


async def clear_pending(session_id: str, agent_base: str) -> None:
    await cache.delete(_key(session_id, agent_base))


def await_decision(payload: dict) -> str:
    """Pause the graph for human approval; returns the decision string on resume.

    On first execution this raises GraphInterrupt (the node suspends and the graph
    is checkpointed). On resume via Command(resume=<decision>) it returns the
    decision ("approved" / "rejected" / "timeout").
    """
    return interrupt(payload)
