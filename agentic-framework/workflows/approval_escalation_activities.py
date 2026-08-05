import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura
from workflows.temporal.workflow.escalating_approval_workflow import AutoRejectInput, EscalateApprovalInput

logger = logging.getLogger(__name__)


async def _session_org(session_id: str) -> str | None:
    """Multi-tenant routing for Temporal activities: they run on the worker with
    no exec-context, so resolve the session's org explicitly (best-effort --
    None falls back to the default source / Carer)."""
    try:
        from workflows.graph.runner import org_of_session  # late import (cycle)
        return await org_of_session(session_id) or None
    except Exception:  # noqa: BLE001
        logger.warning("could not resolve org for session %s", session_id, exc_info=True)
        return None


@activity.defn
async def escalate_approval_activity(inp: EscalateApprovalInput) -> dict:
    approval = await hasura.create_approval_task(
        session_id=inp.session_id,
        agent_id=inp.agent_id,
        action_type=inp.action_type,
        payload=inp.payload,
        escalation_level=inp.escalation_level,
        org_id=await _session_org(inp.session_id),
    )
    await broadcast(inp.session_id, {
        "type": "approval_escalated",
        "approval_id": approval["id"],
        "previous_approval_id": inp.previous_approval_id,
        "level": inp.escalation_level,
        "action": inp.action_type,
    })
    logger.info("approval escalated  session=%s  level=%d  approval=%s",
                inp.session_id, inp.escalation_level, approval["id"])
    return {"approval_id": approval["id"]}


@activity.defn
async def auto_reject_approval_activity(inp: AutoRejectInput) -> None:
    await hasura.decide_approval(inp.approval_id, "rejected", "system",
                                 org_id=await _session_org(inp.session_id))
    await broadcast(inp.session_id, {
        "type": "approval_auto_rejected",
        "approval_id": inp.approval_id,
        "action": inp.action_type,
        "reason": "max_escalation_reached",
    })
    from workflows.graph.runner import resume_session
    await resume_session(inp.session_id, "rejected")
    logger.info("approval auto-rejected  session=%s  approval=%s", inp.session_id, inp.approval_id)
