"""Generic registry-driven agent body -- port of RegistryAgentWorkflow.

Runs ANY DB-defined agent from its catalog (hospilot.agent_registry): fetch
sub-agents + tasks, plan each sub-agent (LLM), gate each task, dispatch built-in
tasks. A built-in task that returns {"_needs_approval": True, "confirm_task": ...}
triggers a resume-aware interrupt; on approval the named confirm task runs and the
loop continues from the next task.

The loop is resumable from a saved (sa_idx, task_idx) position so that, on resume,
already-executed (side-effecting) built-in tasks are NOT re-run. Sub-agents after
the approval point are planned lazily as they are reached (plan_subagent is a
no-op for already-planned sub-agents), preserving the original ordering.
"""

import logging

from workflows.graph import hitl
from workflows.graph.step_rec import emit_step_recommendation
from workflows.graph.trace import humanize_title
from workflows.graph.planning import should_run_task, plan_subagent, run_dynamic_tasks
from agents._shared.builtin_tasks import run_builtin_task, fetch_agent_catalog, BuiltinTaskInput

logger = logging.getLogger(__name__)


def _registry_rec_fields(res: dict, agent_id: str) -> dict:
    """Best-effort per-step recommendation content from a generic built-in task result.

    Registry task results have no fixed recommendation schema, so derive readable
    fields from what's available: the confirm_task name, any summary/message, and a
    few whitelisted display fields.
    """
    confirm_task = res.get("confirm_task") or ""
    title = humanize_title(confirm_task.replace("confirm_", "")) if confirm_task else humanize_title(agent_id)
    headline = res.get("summary") or res.get("message") or title
    rationale = res.get("rationale") or res.get("summary") or res.get("message") or ""
    extras = {"confirm_task": confirm_task}
    for k in ("matched_count", "proposed_count", "count", "patient_name", "appt_time", "specialization"):
        if res.get(k) is not None:
            extras[k] = res[k]
    return {
        "headline":  headline,
        "actions":   [title] if title else ["Confirm pending action"],
        "rationale": rationale,
        "risk":      res.get("risk", "medium"),
        "kind":      confirm_task or "task_approval",
        "extras":    extras,
    }

# --- Execution seam: route Temporal activities through run_activity ----------
from functools import partial as _partial
from workflows.graph.agents._activity import run_activity as _run_activity
for _n, _f in list(globals().items()):
    if callable(_f) and hasattr(_f, "__temporal_activity_definition") and _n != "get_prefetch_cache":
        globals()[_n] = _partial(_run_activity, _f)


async def _registry_loop(
    sid: str, agent_id: str, subagents: list, ta_results: dict,
    task_plan: dict | None, child_ctx: dict, goal: str,
    start_sa: int = 0, start_task: int = 0,
) -> dict:
    agent_tasks = {sa["id"]: sa.get("tasks", []) for sa in subagents}

    for sa_i in range(start_sa, len(subagents)):
        sa = subagents[sa_i]
        sa_id = sa["id"]
        if task_plan is not None:
            await plan_subagent(agent_id, sa_id, agent_tasks, task_plan, ta_results, goal, sid)
        tasks = sa.get("tasks", [])
        t_start = start_task if sa_i == start_sa else 0
        for t_i in range(t_start, len(tasks)):
            tid = tasks[t_i]["id"]
            if await should_run_task(tid, sa_id, ta_results, task_plan, sid):
                res = await run_builtin_task(BuiltinTaskInput(
                    agent_id=agent_id, task_id=tid, session_id=sid,
                    ta_results=ta_results, ctx=child_ctx))
                ta_results[tid] = res or {}
                if isinstance(res, dict) and res.get("_needs_approval"):
                    await hitl.save_pending(sid, agent_id, {
                        "subagents": subagents, "sa_idx": sa_i, "task_idx": t_i,
                        "ta_results": ta_results, "task_plan": task_plan,
                        "child_ctx": child_ctx, "goal": goal, "agent_id": agent_id,
                        "confirm_task": res.get("confirm_task"),
                    })
                    _rec = _registry_rec_fields(res, agent_id)
                    await emit_step_recommendation(sid, agent_id=agent_id, **_rec)
                    hitl.await_decision({"kind": "registry_approval", "session_id": sid,
                                         "agent_id": agent_id, "confirm_task": res.get("confirm_task"),
                                         "action_type": _rec["kind"], "risk": _rec["risk"]})

    dynamic = await run_dynamic_tasks(agent_id, task_plan, ta_results, sid)
    return {
        "status": "completed", "agent_id": agent_id, "ta_results": ta_results,
        **({"dynamic_tasks": dynamic} if dynamic else {}),
    }


async def _registry_resume(sid: str, pending: dict, decision: str) -> dict:
    agent_id = pending["agent_id"]
    ta_results = pending["ta_results"]
    task_plan = pending["task_plan"]
    child_ctx = pending["child_ctx"]
    subagents = pending["subagents"]
    confirm_task = pending.get("confirm_task")

    # Reflect the decision back onto the approval task's result (parity with original).
    approval_tid = subagents[pending["sa_idx"]]["tasks"][pending["task_idx"]]["id"]
    if isinstance(ta_results.get(approval_tid), dict):
        ta_results[approval_tid]["approval_decision"] = decision

    if decision == "approved" and confirm_task:
        cres = await run_builtin_task(BuiltinTaskInput(
            agent_id=agent_id, task_id=confirm_task, session_id=sid,
            ta_results=ta_results, ctx=child_ctx))
        ta_results[confirm_task] = cres or {}

    return await _registry_loop(
        sid, agent_id, subagents, ta_results, task_plan, child_ctx, pending["goal"],
        start_sa=pending["sa_idx"], start_task=pending["task_idx"] + 1,
    )


async def run_registry_body(sid: str, ctx: dict) -> dict:
    agent_id = ctx.get("_agent_id", "")

    pending = await hitl.load_pending(sid, agent_id)
    if pending is not None:
        decision = hitl.await_decision({"kind": "registry_approval", "session_id": sid, "agent_id": agent_id})
        await hitl.clear_pending(sid, agent_id)
        return await _registry_resume(sid, pending, decision)

    goal = ctx.get("_goal", "")
    task_type = ctx.get("_task_type", "")

    subagents = await fetch_agent_catalog(agent_id)
    if not subagents:
        return {"status": "completed", "agent_id": agent_id, "message": "no sub-agents in registry"}

    _raw_plan = ctx.get("_task_plan")
    task_plan: dict | None = dict(_raw_plan) if _raw_plan is not None else None
    if task_plan is not None and goal:
        for sa in subagents:
            task_plan.setdefault(sa["id"], {})

    # G10: thread upstream agent results (placed in ctx by build_ctx, keyed by base
    # agent id) into the task ctx so built-in tasks can consume the real cohort /
    # cross-agent signals (e.g. appointment booking gating on staff_agent.workload_ok),
    # mirroring the bed.py pattern. Underscore keys are seeds, re-set explicitly below.
    child_ctx = {k: v for k, v in ctx.items() if not k.startswith("_")}
    child_ctx["_goal"] = goal
    child_ctx["_task_type"] = task_type
    return await _registry_loop(sid, agent_id, subagents, ta_results={}, task_plan=task_plan,
                                child_ctx=child_ctx, goal=goal)
