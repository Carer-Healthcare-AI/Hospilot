"""Final synthesis node -- ported verbatim from api.agents.synthesise_session
(+ _format_findings, _broadcast_fallback_recommendation, _SYNTHESIS_PROMPT).

Runs once at the end of the session graph: synthesises all agent findings into
a recommendation, persists the results into the Hasura pipeline_snapshot (so
commit_session still works), and emits the same WS events as before
(synthesis_started -> session_recommendation -> session_completed).
"""

import asyncio
import json
import logging
from pathlib import Path

from api.routes.ws import broadcast
from cache import redis as cache
from db.hasura import hasura
from llm_client import llm_chat

logger = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).parent.parent / "system_prompts"
_SYNTHESIS_PROMPT = (_PROMPT_DIR / "synthesis.txt").read_text(encoding="utf-8")


def _format_findings(results: dict) -> str:
    lines = []
    for agent_id, result in results.items():
        if not isinstance(result, dict) or agent_id.startswith("_"):
            continue
        lines.append(f"[{agent_id}]")
        for k, v in result.items():
            if k not in ("agent_id",) and v is not None:
                lines.append(f"  {k}: {v}")
    return "\n".join(lines) if lines else "No agent findings available."


async def _broadcast_fallback_recommendation(session_id: str, results: dict) -> None:
    """Rule-based recommendation when Claude is unavailable."""
    try:
        actions: list[str] = []
        risk = "low"
        parts: list[str] = []

        session = await hasura.get_session(session_id)
        goal = (session.get("goal", "") if session else "") or "hospital operational task"

        er = results.get("er_agent", {})
        if er.get("critical_selected", 0) or er.get("triaged", 0):
            n = er.get("critical_selected") or er.get("triaged", 0)
            parts.append(f"{n} ER patient(s) assessed")
            if er.get("critical_selected", 0) > 0:
                risk = "high"
                actions.append(f"Admit {er['critical_selected']} critical ER patient(s) immediately")

        icu = results.get("icu_agent", {})
        if icu.get("step_down", 0):
            parts.append(f"{icu['step_down']} ICU step-down candidate(s)")
            actions.append(f"Initiate step-down transfer for {icu['step_down']} stable ICU patient(s)")
            if risk == "low":
                risk = "medium"

        dis = results.get("discharge_agent", {})
        if dis.get("ready", 0):
            parts.append(f"{dis['ready']} patient(s) discharge-ready")
            actions.append(f"Discharge {dis['ready']} patient(s) to free beds immediately")

        bed = results.get("bed_agent", {})
        if bed.get("beds_reserved", 0):
            parts.append(f"{bed['beds_reserved']} bed(s) reserved")
            actions.append(f"Confirm {bed['beds_reserved']} bed reservation(s) and assign incoming patients")
        elif bed.get("bed_id"):
            parts.append("1 bed reserved")
            actions.append("Confirm bed reservation and assign incoming patient")

        rev = results.get("revenue_agent", {})
        if rev.get("risk_level") in ("medium", "high"):
            actions.append("Review billing gaps flagged by Revenue agent")
            if risk == "low":
                risk = "medium"

        if not actions:
            actions.append("Review individual agent findings in the output panel")

        headline = " · ".join(parts) if parts else f"Agents completed for: {goal[:80]}"
        summary = (
            f"Responding to: \"{goal[:120]}\". "
            + (" ".join(parts) + "." if parts else "All agents completed.")
            + " AI synthesis was unavailable -- findings shown above."
        )
        await broadcast(session_id, {
            "type":     "session_recommendation",
            "headline": headline,
            "actions":  actions[:3],
            "risk":     risk,
            "summary":  summary,
        })
    except Exception as exc:
        logger.warning("fallback recommendation failed  session=%s  err=%s", session_id, exc)


async def _synthesise(session_id: str, results: dict, goal: str) -> dict | None:
    findings = _format_findings(results)
    last_exc: Exception | None = None
    response = None
    for attempt in range(4):
        if attempt:
            await asyncio.sleep(2 ** attempt)
        try:
            text = await llm_chat(
                user=_SYNTHESIS_PROMPT.format(goal=goal, findings=findings),
                max_tokens=1024,
                temperature=0,
                tier="quality",
            )
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            logger.warning("synthesis API error (attempt %d/4)  err=%s", attempt + 1, exc)
    if last_exc is not None:
        raise last_exc  # type: ignore[misc]
    start = text.find('{')
    end   = text.rfind('}')
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in synthesis response: {text[:200]!r}")
    return json.loads(text[start:end + 1])


async def _recommend_reorchestration(session_id: str, state: dict) -> dict:
    """A task failed after retries: recommend a revised plan and park for approval.

    Loops back into the planning graph (seeded with failure context as feedback) so
    it re-plans around the failed task and pauses at the approval interrupt with the
    recommendation. A Redis attempt-counter caps auto-recommendations.
    """
    from workflows.graph.runner import start_planning  # late import -- avoids runner<->synthesis cycle

    failure = state.get("_task_failed", {})
    results = state.get("results", {})

    attempt = await cache.incr(f"session:{session_id}:reorch_attempts")
    await cache.expire(f"session:{session_id}:reorch_attempts", 86400)
    if attempt > cache.FAILURE_REPLAN_CAP:
        logger.warning("reorchestration cap reached  session=%s  attempts=%d", session_id, attempt)
        await hasura.update_session_status(session_id, "failed", pipeline_snapshot=results)
        await broadcast(session_id, {
            "type": "session_failed",
            "failed_task": failure,
            "error": f"task {failure.get('task_id')} failed; reorchestration cap reached",
        })
        return {}

    session = await hasura.get_session(session_id)
    goal = (session.get("goal", "") if session else "") or state.get("goal", "")
    constraints = session.get("constraints", "") if session else ""
    completed = [k for k, v in results.items() if isinstance(v, dict) and not k.startswith("_")]
    feedback = (
        f"EXECUTION FAILURE -- replan around it. Agent '{failure.get('base')}' failed at task "
        f"'{failure.get('task_id')}' (activity {failure.get('activity')}): {failure.get('error')}. "
        f"Prefer a path that does not depend on the failed task, or drop that branch. "
        f"Agents completed before the failure: {completed}."
    )

    # DB status enum is fixed -> keep "pending" and flag the awaiting state in Redis
    # (read by the /commit guard; cleared when execution (re-)launches).
    await hasura.update_session_status(session_id, "pending", pipeline_snapshot=results)
    await cache.set(f"session:{session_id}:awaiting_reorchestration", True, ttl=86400)
    await broadcast(session_id, {
        "type": "reorchestration_recommended",
        "reason": "task_failed",
        "failed_task": failure,
        "attempt": attempt,
    })
    # Planning graph emits plan_stage_* then plan_awaiting_approval with the revised plan.
    # Carry the user's own reorchestration rounds (and the plan they produced) into the
    # replan -- replanning from the bare goal would quietly undo revisions like
    # "now the ICU is at 57%" while fixing the failure.
    history = await hasura.list_reorchestration_feedback(session_id)
    await start_planning(session_id, goal, constraints, feedback=feedback, attempt=attempt,
                         feedback_history=history,
                         prior_plan=(session or {}).get("pipeline"))
    return {}


async def synthesise_node(state: dict) -> dict:
    """Terminal node: synthesise findings, persist snapshot, emit WS events."""
    session_id = state["session_id"]
    results = state.get("results", {})

    # A single TASK failed after retries -- do NOT synthesise; recommend a revised
    # plan and park for user approval (Feature B). Must be checked BEFORE _failed.
    if state.get("_task_failed"):
        return await _recommend_reorchestration(session_id, state)

    # An agent raised SessionFatalError -- session_failed already broadcast and the
    # status set to "failed". Do NOT synthesise or overwrite the status.
    if state.get("_failed"):
        return {}

    await broadcast(session_id, {"type": "synthesis_started"})

    # Agents that already emitted a mid-flow step_recommendation (and were decided)
    # are EXCLUDED from the final synthesis so items handled mid-flow are not
    # re-surfaced/double-counted. Fail-open: an empty set means no exclusion.
    midflow = await cache.get_midflow_agents(session_id)
    results_for_synth = ({k: v for k, v in results.items() if k not in midflow}
                         if midflow else results)

    rec: dict = {}
    try:
        session = await hasura.get_session(session_id)
        goal = (session.get("goal", "") if session else "") or state.get("goal", "")
        rec = await _synthesise(session_id, results_for_synth, goal) or {}
        await broadcast(session_id, {
            "type": "session_recommendation",
            "headline": rec.get("headline", ""),
            "actions":  rec.get("actions", []),
            "risk":     rec.get("risk", "low"),
            "summary":  rec.get("summary", ""),
        })
        logger.info("synthesis complete  session=%s  risk=%s", session_id, rec.get("risk"))
    except Exception:
        logger.exception("synthesis failed  session=%s -- using fallback", session_id)
        await _broadcast_fallback_recommendation(session_id, results_for_synth)

    # Persist the agent results into the snapshot so commit_session can read them.
    # The DB status enum is fixed (pending|running|completed|failed), so a partial
    # failure still persists as "completed" -- the "errors happened" signal is carried
    # out-of-band via a snapshot meta flag (ignored by commit/format) and the WS event.
    had_errors = any(isinstance(r, dict) and r.get("status") == "failed" for r in results.values())
    snapshot = {**results, "_completed_with_errors": True} if had_errors else results
    try:
        await hasura.update_session_status(session_id, "completed", pipeline_snapshot=snapshot, synthesis_result=rec or None)
    except Exception:
        logger.exception("failed to persist completed snapshot  session=%s", session_id)
    await broadcast(session_id, {"type": "session_completed", "had_errors": had_errors})

    return {"recommendation": rec}
