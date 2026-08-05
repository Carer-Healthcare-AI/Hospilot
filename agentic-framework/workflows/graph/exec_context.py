"""Per-node execution context for task-level failure attribution.

A LangGraph node runs its agent body inside its own coroutine, so a
``contextvars.ContextVar`` set at the top of the node is isolated to that node's
execution (and propagates into every ``await`` it makes -- including the
graph.agents._activity.run_activity seam). We use it to record which task is
currently executing, so that when an activity raises, the seam can attach the
task id to a graph.errors.TaskExecutionError without threading the id through
every call site.

Cache-hit / hardcoded paths that never go through should_run_task leave
``task=None`` -- the failure is then reported at agent granularity (best effort).
"""

import contextvars

_CUR: contextvars.ContextVar[dict | None] = contextvars.ContextVar("hospilot_exec_ctx", default=None)


def set_exec_ctx(session_id: str, agent_id: str, org_id: str = "") -> None:
    """Begin a node's execution scope. Call at the top of the agent node.

    `org_id` (multi-tenancy) routes hasura tenant-table queries made anywhere
    inside this scope to the org's Hasura source (db.hasura reads it back via
    get_exec_ctx). When not passed, an org set earlier in this context (e.g. by
    the drive loop) is preserved -- "" means the default source (Carer org).
    """
    prev = _CUR.get() or {}
    _CUR.set({"session_id": session_id, "agent_id": agent_id, "task": None,
              "org_id": org_id or prev.get("org_id", "")})


def set_current_task(task_id: str) -> None:
    """Record the task about to execute (called from should_run_task on a True)."""
    ctx = _CUR.get()
    if ctx is not None:
        ctx["task"] = task_id


def get_exec_ctx() -> dict | None:
    """Return the current node's exec context, or None outside a node scope."""
    return _CUR.get()
