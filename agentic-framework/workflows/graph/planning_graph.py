"""The 3-stage planning graph -- planning is now part of the LangGraph flow.

Replaces the single synchronous `generate_pipeline` HTTP call with a durable,
pausable StateGraph:

    plan_agents -> plan_subagents -> plan_tasks -> validate_plan
        -> critique_plan -> await_plan_approval
                |  (auto-revise, capped at 1)           |  (reorchestrate)
                +--> plan_agents (auto loop)             +--> plan_agents (human loop)

Each stage is a focused LLM pass (services.planner.generate_agents_and_edges /
select_pipeline_subagents / plan_pipeline_tasks) -- splitting the old mega-prompt
into three produces materially better plans. The critique_plan node runs an
automated quality check and may trigger ONE automatic reorchestrate before the
human sees the plan. The terminal node `interrupt()`s for human approval; on
resume the user can approve, submit an edited pipeline, or ask for reorchestration
(loop back). On approval the driver (graph.runner) materializes the task-level
preplan and launches the execution graph.

Runs on its own checkpoint thread (`{session_id}:plan`) so its approval interrupt
never collides with the execution graph's own approval interrupts.
"""

import logging
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from api.routes.ws import broadcast
from db.hasura import hasura
from workflows.planner import (
    generate_agents_and_edges,
    select_pipeline_subagents,
    plan_pipeline_tasks,
    validate_and_dedupe_plan,
    critique_pipeline,
    goal_with_feedback_history,
)

logger = logging.getLogger(__name__)


class PlanState(TypedDict, total=False):
    session_id: str
    goal: str
    constraints: str
    org_id: str          # multi-tenancy: org owning this session ("" = default/Carer)
    autonomous: bool     # Phase 3: auto-approve the plan; no human plan-approval wait
    feedback: str        # appended to the goal on reorchestrate / failure-replan
    feedback_history: list  # every user reorchestration feedback so far, oldest first
    attempt: int
    pipeline: dict
    prior_plan: dict     # the plan being revised this round (reorchestrate / critic loop)
    approved: bool
    critic_attempt: int  # automatic critic-driven reorchestrates so far (capped at _MAX_CRITIC_REVISIONS)
    critic_report: dict  # last critic JSON verdict (for UI/debug)
    critic_revise: bool  # routing flag set by _critique_plan


def _goal_with_feedback(state: PlanState) -> str:
    """Goal + the FULL user reorchestration history + any transient (critic / failure)
    feedback. Replanning is stateless apart from this string, so dropping the history
    would make each reorchestration round undo the previous one's revisions."""
    goal = goal_with_feedback_history(state.get("goal", ""), state.get("feedback_history") or [])
    fb = state.get("feedback")
    return f"{goal}\n\n{fb}" if fb else goal


async def _plan_agents(state: PlanState) -> dict:
    sid = state["session_id"]
    await broadcast(sid, {"type": "plan_stage_started", "stage": "agents"})
    # On a reorchestrate / critic loop the previous plan is still in state: revise it
    # incrementally instead of rebuilding from the goal. On a fresh entry (failure
    # replan) there is no "pipeline" yet -- fall back to the plan seeded by the caller.
    prior = state.get("pipeline") or state.get("prior_plan") or None
    pipeline = await generate_agents_and_edges(
        _goal_with_feedback(state), state.get("constraints", ""), prior_plan=prior,
    )
    await broadcast(sid, {
        "type":  "plan_stage_completed",
        "stage": "agents",
        "agents": [a["id"] for a in pipeline.get("agents", [])],
    })
    # Stash the plan being revised -- stage 2 needs it after "pipeline" is overwritten.
    return {"pipeline": pipeline, "prior_plan": prior or {}}


async def _plan_subagents(state: PlanState) -> dict:
    sid = state["session_id"]
    await broadcast(sid, {"type": "plan_stage_started", "stage": "subagents"})
    pipeline = await select_pipeline_subagents(
        state["pipeline"], _goal_with_feedback(state), prior_plan=state.get("prior_plan") or None,
    )
    await broadcast(sid, {"type": "plan_stage_completed", "stage": "subagents"})
    return {"pipeline": pipeline}


async def _plan_tasks(state: PlanState) -> dict:
    sid = state["session_id"]
    await broadcast(sid, {"type": "plan_stage_started", "stage": "tasks"})
    pipeline = await plan_pipeline_tasks(state["pipeline"], _goal_with_feedback(state))
    await broadcast(sid, {"type": "plan_stage_completed", "stage": "tasks"})
    return {"pipeline": pipeline}


async def _validate_plan(state: PlanState) -> dict:
    """Stage 3b: silently merge/remove redundant steps and branches, then persist
    the cleaned pipeline so the UI (and resume / approval) reads the clean version."""
    sid = state["session_id"]
    pipeline = await validate_and_dedupe_plan(state["pipeline"], _goal_with_feedback(state), session_id=sid)
    try:
        await hasura.update_session_pipeline(sid, pipeline)
    except Exception:  # noqa: BLE001
        logger.warning("could not persist proposed pipeline  session=%s", sid, exc_info=True)
    return {"pipeline": pipeline}


_MAX_CRITIC_REVISIONS = 1


async def _critique_plan(state: PlanState) -> dict:
    """Stage 3c: automated quality gate. Scores the plan against five principles and
    triggers ONE automatic reorchestrate if it fails, before the human sees it.
    FAIL-OPEN: any error in the critic defaults to pass so approval is never blocked."""
    sid = state["session_id"]
    await broadcast(sid, {"type": "plan_stage_started", "stage": "critique"})
    report = await critique_pipeline(state["pipeline"], _goal_with_feedback(state))
    await broadcast(sid, {
        "type":    "plan_stage_completed",
        "stage":   "critique",
        "verdict": report.get("verdict"),
        "score":   report.get("score"),
    })
    used = state.get("critic_attempt", 0)
    revise = report.get("verdict") == "needs_revision" and used < _MAX_CRITIC_REVISIONS
    if revise:
        fb = report.get("revision_instruction") or "Lead with the agent that owns the goal."
        return {
            "critic_revise":  True,
            "critic_attempt": used + 1,
            "critic_report":  report,
            "feedback": (state.get("feedback", "") + f"\n\nAutomated plan review: {fb}").strip(),
        }
    return {"critic_revise": False, "critic_report": report}


def _route_after_critique(state: PlanState) -> str:
    return "plan_agents" if state.get("critic_revise") else "await_plan_approval"


async def _await_plan_approval(state: PlanState) -> dict:
    """Pause for human approval. interrupt() raises on first run (graph checkpoints);
    on resume via Command(resume=decision) it returns the decision payload:
      {"action": "approve" | "edit" | "reorchestrate", "pipeline"?: dict, "feedback"?: str}
    """
    sid = state["session_id"]

    # Autonomous mode (Phase 3): no human plan-approval wait. Skip the interrupt
    # entirely and approve the plan as-is, so the driver launches execution in the
    # background immediately. We still emit an informational event for the UI.
    if state.get("autonomous"):
        await broadcast(sid, {
            "type":       "plan_auto_approved",
            "session_id": sid,
            "pipeline":   state.get("pipeline", {}),
        })
        return {"approved": True, "feedback": ""}

    await broadcast(sid, {
        "type":     "plan_awaiting_approval",
        "pipeline": state.get("pipeline", {}),
        "attempt":  state.get("attempt", 0),
    })
    decision = interrupt({"kind": "plan_approval", "session_id": sid}) or {}
    action = decision.get("action", "approve")

    if action == "edit" and decision.get("pipeline"):
        return {"pipeline": decision["pipeline"], "approved": True, "feedback": ""}
    if action == "reorchestrate":
        fb = (decision.get("feedback", "") or "User requested a different plan.").strip()
        # Append, never replace: the next round must honour this feedback AND every
        # earlier round's. `feedback` is cleared because it only carries transient
        # critic/failure notes -- the user's asks now live in feedback_history.
        # A caller that can see the audit log (the plan-decision route) passes the
        # complete history instead, since rounds made through /reorchestrate never
        # reached this checkpoint; it already ends with `fb`.
        history = [h.strip() for h in (decision.get("feedback_history") or []) if h and h.strip()]
        if not history:
            history = list(state.get("feedback_history") or [])
            if not history or history[-1] != fb:
                history.append(fb)
        out = {
            "approved":         False,
            "feedback":         "",
            "feedback_history": history,
            "attempt":          state.get("attempt", 0) + 1,
        }
        # Same reason: revise the plan the user is actually looking at (persisted), not
        # the possibly-stale one this checkpoint last produced.
        if decision.get("prior_plan"):
            out["pipeline"] = decision["prior_plan"]
        return out
    return {"approved": True, "feedback": ""}


def _route_after_approval(state: PlanState) -> str:
    return END if state.get("approved") else "plan_agents"


def build_planning_graph(checkpointer):
    """Compile the planning StateGraph. Cyclic (reorchestrate) -- callers must pass
    a recursion_limit in the run config (see graph.runner._plan_config).

    Flow:
      plan_agents -> plan_subagents -> plan_tasks -> validate_plan
          -> critique_plan --(needs_revision & budget left)--> plan_agents  (auto, capped at 1)
          -> critique_plan --(pass or cap reached)-----------> await_plan_approval
          -> await_plan_approval --(reorchestrate)-----------> plan_agents  (human)
          -> await_plan_approval --(approve/edit)------------- END
    """
    g = StateGraph(PlanState)
    g.add_node("plan_agents",          _plan_agents)
    g.add_node("plan_subagents",       _plan_subagents)
    g.add_node("plan_tasks",           _plan_tasks)
    g.add_node("validate_plan",        _validate_plan)
    g.add_node("critique_plan",        _critique_plan)
    g.add_node("await_plan_approval",  _await_plan_approval)

    g.add_edge(START, "plan_agents")
    g.add_edge("plan_agents",    "plan_subagents")
    g.add_edge("plan_subagents", "plan_tasks")
    g.add_edge("plan_tasks",     "validate_plan")
    g.add_edge("validate_plan",  "critique_plan")
    g.add_conditional_edges("critique_plan", _route_after_critique,
                            {"plan_agents": "plan_agents", "await_plan_approval": "await_plan_approval"})
    g.add_conditional_edges("await_plan_approval", _route_after_approval,
                            {END: END, "plan_agents": "plan_agents"})
    return g.compile(checkpointer=checkpointer)
