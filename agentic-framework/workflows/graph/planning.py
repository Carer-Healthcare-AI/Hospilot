"""Sub-agent / task planning and gating -- ported from
temporal.workflow._condition_check.

Behaviour is identical to the Temporal version; the only change is that
`workflow.execute_activity(fn, Input(...))` calls become direct `await fn(...)`
calls, since activities are now plain async functions invoked in-process inside
a LangGraph node. All WebSocket broadcasts (task_condition, agent_plan) are
preserved verbatim so the frontend contract is unchanged.
"""

import logging
from contextlib import nullcontext

from cache import redis as cache  # noqa: F401  (kept for parity / future use)
from schemas.types import TypedCondition
from api.routes.ws import broadcast
from workflows.graph.exec_context import get_exec_ctx, set_current_task
from workflows.graph.conditions import resolve_condition
from workflows.planner import plan_subagent_tasks, PlanSubagentInput
from agents._shared.condition_activities import (
    evaluate_task_condition,
    EvaluateTaskConditionInput,
)
from agents._shared.generic_activities import run_dynamic_task, DynamicTaskInput
from agents._shared.generated_activities import run_generated_task

logger = logging.getLogger(__name__)


# Sentinel for a planned sub-agent the approved plan did NOT include.
# Means "skip every task in this sub-agent" (see should_run_task). Distinct from
# an empty {} slot, which means "plan pending -> run all / plan at runtime".
PLANNED_SKIP = {"__planned__": True}


def subagent_in_plan(subagent_id: str, task_plan: dict | None, ta_results: dict | None = None) -> bool:
    """Return False only when certain this subagent should be skipped.

    Call this AFTER plan_subagent so the slot is already resolved.
    - No plan / empty plan → True (run-all fallback)
    - Subagent absent from plan → False (not selected by stage-3)
    - PLANNED_SKIP sentinel → False (stage-3 explicitly excluded it)
    - __condition__ present + ta_results provided → evaluate condition; fail-open
    - Any real plan dict → True (run the block; should_run_task gates individual tasks)
    """
    if not task_plan:
        return True
    sp = task_plan.get(subagent_id)
    if sp is None:
        logger.info("SKIP  %-28s  reason=subagent_not_in_plan", subagent_id)
        return False
    if sp.get("__planned__"):
        logger.info("SKIP  %-28s  reason=subagent_planned_skip", subagent_id)
        return False
    condition = sp.get("__condition__")
    if condition and ta_results is not None:
        merged: dict = {}
        for v in ta_results.values():
            if isinstance(v, dict):
                merged.update(v)
        if not resolve_condition(condition, merged):
            logger.info("SKIP  %-28s  reason=subagent_condition_false  cond=%r", subagent_id, condition)
            return False
    return True


def seed_planned_slots(task_plan: dict, catalog_sa_ids) -> None:
    """Fill any catalog sub-agent the approved plan didn't pin with PLANNED_SKIP.

    Hardcoded-catalog agent bodies call this once they have an approved plan
    (task_plan present + a goal). The approved plan is the single source of truth:
    once a session is planned, execution must run EXACTLY it. A sub-agent the plan
    did not list means the user did not approve it, so it is SKIPPED -- never
    re-planned at runtime. This is the fail-closed default that stops the runtime
    Haiku planner (plan_subagent) from silently ADDING tasks the user never saw.

    Note: this deliberately seeds the skip sentinel, NOT an empty {} slot. An empty
    slot would make should_run_task fall back to "run all" and let plan_subagent
    invoke the LLM -- exactly the mid-execution plan mutation we are eliminating.
    DB-registry agents (graph.agents.registry_agent) are unaffected: they have no
    materialized preplan and keep seeding {} so their runtime planning still works.
    """
    for sa_id in catalog_sa_ids:
        task_plan.setdefault(sa_id, dict(PLANNED_SKIP))


def _resolve_actual(symbol: str, ta_results: dict):
    task_ref, _, field = symbol.partition(".")
    if not field:
        return None
    return (ta_results.get(task_ref) or {}).get(field)


def _short(task_id: str) -> str:
    return task_id.removeprefix("ta_")


async def should_run_task(
    task_id: str,
    subagent_id: str,
    ta_results: dict,
    task_plan: dict,
    session_id: str = "",
) -> bool:
    """Return True if this task should execute, False to skip it.

    Wraps _should_run_task with a Langfuse span so every task decision —
    whether the task runs, is skipped by the planner, or fails a condition —
    is visible in the trace. Falls back to no-op when Langfuse is disabled.
    """
    from workflows.graph.observability import get_langfuse_client, trace_id_for
    lf = get_langfuse_client()
    _sid = session_id or (get_exec_ctx() or {}).get("session_id", "")
    tid = trace_id_for(_sid) if (lf and _sid) else None
    _span_cm = (
        lf.start_as_current_span(name=f"check:{task_id}", trace_context={"trace_id": tid})
        if (lf and tid) else nullcontext()
    )
    with _span_cm as span:
        if span is not None:
            try:
                span.update(input={"task_id": task_id, "subagent_id": subagent_id})
            except Exception:  # noqa: BLE001
                pass
        run = await _should_run_task(task_id, subagent_id, ta_results, task_plan, session_id)
        if span is not None:
            try:
                span.update(output={"decision": "RUN" if run else "SKIP"})
            except Exception:  # noqa: BLE001
                pass
    # Surface skipped tasks in the frontend trace (RUN tasks already appear as
    # their own task step once executed). Defensive: record_step never raises.
    if not run and _sid:
        from workflows.graph.trace import record_step, humanize_title
        await record_step(
            _sid, kind="decision", title=humanize_title(task_id), status="skipped",
            agent_id=(get_exec_ctx() or {}).get("agent_id"), task_id=task_id,
        )
    if run:
        set_current_task(task_id)
    return run


async def _should_run_task(
    task_id: str,
    subagent_id: str,
    ta_results: dict,
    task_plan: dict,
    session_id: str = "",
) -> bool:
    """Return True if this task should execute, False to skip it.

    When task_plan is absent all tasks run (backward compat).
    When task_plan is present, omission of a subagent or task means skip.
    Typed conditions ({symbol, op, value}) are evaluated in pure Python.
    String conditions fall back to LLM evaluation (deprecated).
    """
    if not task_plan:
        return True

    subagent_plan = task_plan.get(subagent_id)
    if subagent_plan is None:
        logger.debug("SKIP  %s  reason=subagent_not_planned", task_id)
        return False

    # {} = plan_subagent not yet called -> run all as fallback
    # {"__planned__": True} = LLM was called but selected nothing -> skip all
    if subagent_plan == {}:
        logger.debug("RUN   %s  [plan pending]", task_id)
        return True

    if subagent_plan.get("__planned__"):
        logger.debug("SKIP  %s  reason=planner_selected_nothing", task_id)
        return False

    task_entry = subagent_plan.get(task_id)
    if task_entry is None:
        logger.info("SKIP  %-35s  reason=not_selected", _short(task_id))
        if session_id:
            await broadcast(session_id, {
                "type":         "task_condition",
                "task_id":      task_id,
                "subagent_id":  subagent_id,
                "condition":    None,
                "actual":       None,
                "passed":       False,
                "unresolvable": False,
                "skipped":      True,
            })
        return False

    condition = task_entry.get("condition")
    if not condition:
        logger.info("RUN   %-35s  condition=none", _short(task_id))
        if session_id:
            await broadcast(session_id, {
                "type":         "task_condition",
                "task_id":      task_id,
                "subagent_id":  subagent_id,
                "condition":    None,
                "actual":       None,
                "passed":       True,
                "unresolvable": False,
                "skipped":      False,
            })
        return True

    if isinstance(condition, dict):
        symbol = condition.get("symbol", "?")
        op     = condition.get("op", "?")
        value  = condition.get("value")
        actual       = _resolve_actual(symbol, ta_results)
        unresolvable = actual is None and (ta_results.get(symbol.partition(".")[0]) is not None)
        result       = TypedCondition(**condition).evaluate(ta_results)
        verdict      = "SKIP" if not result else ("RUN [unresolvable->run]" if unresolvable else "PASS")
        logger.info(
            "COND  %-35s  %s %s %r  actual=%r  -> %s",
            _short(task_id),
            symbol.split(".")[-1], op, value, actual, verdict,
        )
        if session_id:
            await broadcast(session_id, {
                "type":          "task_condition",
                "task_id":       task_id,
                "subagent_id":   subagent_id,
                "condition":     condition,
                "actual":        actual,
                "passed":        result,
                "unresolvable":  unresolvable,
                "skipped":       not result,
            })
        return result

    # deprecated string condition -- LLM evaluation (was a Temporal activity)
    logger.info("EVAL  %s  condition=%r  [string->LLM]", task_id, condition)
    result = await evaluate_task_condition(
        EvaluateTaskConditionInput(
            task_id=task_id,
            subagent_id=subagent_id,
            condition=condition,
            ta_results=ta_results,
            session_id=session_id,
        )
    )
    logger.info("LLM   %s  -> %s", task_id, "PASS" if result else "FAIL")
    return result


def get_subagent_order(task_plan: dict | None, default: list[str]) -> list[str]:
    """Return the user-requested sub-agent execution order, or the default."""
    if not task_plan:
        return default
    order = task_plan.get("__subagent_order__")
    if not order:
        return default
    valid = set(default)
    filtered = [sid for sid in order if sid in valid]
    return filtered if filtered else default


async def run_dynamic_tasks(
    agent_id: str,
    task_plan: dict | None,
    ta_results: dict,
    session_id: str,
) -> dict:
    """Execute any dynamic tasks (ta_gen_* / ta_dynamic_*) the planner selected.

    Call at the END of every agent node, after all hardcoded tasks have run.
    Returns {task_id: result} for all tasks that ran so synthesis can see them.
    """
    collected: dict = {}
    if not task_plan:
        return collected
    for subagent_id, subagent_plan in task_plan.items():
        if not isinstance(subagent_plan, dict) or subagent_plan.get("__planned__"):
            continue
        for task_id, entry in subagent_plan.items():
            if task_id.startswith("__"):
                continue
            is_generated = task_id.startswith("ta_gen_")
            is_dynamic   = task_id.startswith("ta_dynamic_")
            if not is_generated and not is_dynamic:
                continue

            if not await should_run_task(task_id, subagent_id, ta_results, task_plan, session_id):
                continue

            if is_generated:
                # Dispatcher -- loads generated function from DB on demand
                result = await run_generated_task(task_id, session_id)
                logger.info("[ok] generated task  task=%s  agent=%s", task_id, agent_id)
            else:
                # Legacy: Claude reasons at runtime
                result = await run_dynamic_task(
                    DynamicTaskInput(
                        task_id=task_id,
                        task_label=entry.get("label", task_id),
                        task_outputs=entry.get("outputs", []),
                        agent_id=agent_id,
                        session_id=session_id,
                        context={k: v for k, v in ta_results.items() if not k.startswith("_")},
                    )
                )
                logger.info("[ok] dynamic task  task=%s  status=%s  agent=%s",
                            task_id, result.get("status"), agent_id)

            ta_results[task_id] = result
            collected[task_id]  = result
    return collected


async def plan_subagent(
    agent_id: str,
    subagent_id: str,
    agent_tasks: dict,
    task_plan: dict,
    ta_results: dict,
    goal: str,
    session_id: str,
) -> None:
    """Call plan_subagent_tasks and merge result into task_plan in-place.

    agent_tasks: {subagent_id: [task_schema_dicts]}.
    No-op if the subagent slot is already filled (supports static pre-planned sessions).
    """
    if task_plan.get(subagent_id) != {}:
        return

    available: list[dict] = agent_tasks.get(subagent_id, [])
    result = await plan_subagent_tasks(
        PlanSubagentInput(
            agent_id=agent_id,
            subagent_id=subagent_id,
            available_tasks=available,
            ta_results=ta_results,
            goal=goal,
            session_id=session_id,
        )
    )
    # Empty plan = LLM selected nothing. Store sentinel so should_run_task skips all.
    task_plan[subagent_id] = result if result else {"__planned__": True}

    # -- compact summary log ---------------------------------------------------
    available_ids  = [t["id"] for t in available]
    selected_ids   = list(result.keys())
    skipped_ids    = [tid for tid in available_ids if tid not in result]
    conditioned    = {tid for tid, e in result.items() if e.get("condition")}

    def _fmt(tid: str) -> str:
        return _short(tid) + ("[?]" if tid in conditioned else "")

    run_str  = " · ".join(_fmt(t) for t in selected_ids) or "--"
    skip_str = " · ".join(_short(t) for t in skipped_ids)
    logger.info(
        "PLAN  %-28s  [ok] %s%s",
        subagent_id,
        run_str,
        f"  │ skip: {skip_str}" if skip_str else "",
    )

    # -- WS event for frontend -------------------------------------------------
    if session_id:
        await broadcast(session_id, {
            "type":        "agent_plan",
            "agent_id":    agent_id,
            "subagent_id": subagent_id,
            "tasks": [
                {
                    "id":        tid,
                    "selected":  True,
                    "condition": result[tid].get("condition"),
                }
                for tid in selected_ids
            ] + [
                {"id": tid, "selected": False}
                for tid in skipped_ids
            ],
        })
