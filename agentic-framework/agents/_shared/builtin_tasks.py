"""
Built-in task dispatcher for registry-driven agents.

New agents define their tasks as plain async functions (uniform signature
    async def ta_xxx(session_id, ta_results, ctx) -> dict
) collected into a per-agent map. This single activity routes a (agent_id, task_id)
to the right function -- mirroring the run_generated_task pattern so no per-task
worker registration is needed. User-added tasks (ta_gen_* / ta_dynamic_*) are
handled separately by run_dynamic_tasks (codegen / runtime-Claude).
"""

import logging
from dataclasses import dataclass, field

from temporalio import activity

from db.hasura import hasura

logger = logging.getLogger("builtin_tasks")

# agent_id -> {task_id: async fn(session_id, ta_results, ctx) -> dict}
BUILTIN_TASKS: dict[str, dict] = {}


def builtin_task_ids(agent_id: str) -> set[str]:
    return set(BUILTIN_TASKS.get(agent_id.split(":")[0], {}).keys())


@dataclass
class BuiltinTaskInput:
    agent_id: str
    task_id: str
    session_id: str
    ta_results: dict = field(default_factory=dict)
    ctx: dict = field(default_factory=dict)


@activity.defn
async def fetch_agent_catalog(agent_id: str) -> list[dict]:
    """
    Return one agent's sub-agents + tasks from the DB registry, shaped for the
    generic workflow / planner: [{id, label, tasks:[{id, label, outputs}]}].
    Source of truth is hospilot.agent_registry -- NOT the hardcoded planner dicts.
    """
    base = agent_id.split(":")[0]
    rows = await hasura.fetch_agent_registry()
    for a in rows:
        if a.get("id") == base:
            return [
                {
                    "id": sa["id"],
                    "label": sa.get("label", ""),
                    "tasks": [
                        {"id": t["id"], "label": t.get("label", ""), "outputs": t.get("outputs") or []}
                        for t in sa.get("tasks", [])
                    ],
                }
                for sa in a.get("subagents", [])
            ]
    logger.warning("agent not found in registry  agent=%s", base)
    return []


@activity.defn
async def run_builtin_task(inp: BuiltinTaskInput) -> dict:
    fn = BUILTIN_TASKS.get(inp.agent_id.split(":")[0], {}).get(inp.task_id)
    if not fn:
        logger.warning("no builtin handler  agent=%s  task=%s", inp.agent_id, inp.task_id)
        return {"status": "skipped", "reason": "no_builtin_handler"}
    try:
        result = await fn(inp.session_id, inp.ta_results or {}, inp.ctx or {})
        return result if isinstance(result, dict) else {"result": result}
    except Exception as exc:
        logger.exception("builtin task failed  agent=%s  task=%s", inp.agent_id, inp.task_id)
        return {"status": "error", "task_id": inp.task_id, "error": str(exc)}
