import logging

from fastapi import APIRouter, HTTPException, Depends

from schemas.models import ApproveRequest
from db.hasura import hasura
from api.routes.auth import AuthContext, require_active_user, require_role
from api.routes._authz import authorized_session
from api.routes.ws import broadcast

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/approvals/{approval_id}/decide")
async def decide_approval(
    approval_id: str,
    body: ApproveRequest,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_role("approver", "admin")),
):
    # The approver identity comes from the verified JWT -- body.approver_id is
    # deprecated and ignored. The decide is routed at the caller's tenant
    # source and lands only on still-pending rows, so another org's approval
    # (or a double decide) is a plain 404.
    result = await hasura.decide_approval(
        approval_id=approval_id,
        decision=body.decision,
        approver_id=ctx.user_id,
        org_id=(org_id if ctx.is_super() else ctx.org_id),
    )
    if not result:
        raise HTTPException(status_code=404, detail="Approval task not found")

    session_id = result.get("session_id")
    agent_id = result.get("agent_id")
    action_type = result.get("action_type")

    if session_id:
        # Signal the escalation workflow to stop its timer.
        from workflows.temporal.workflow._escalation import signal_escalation_decided
        await signal_escalation_decided(session_id, agent_id, action_type)

        # Resume the parked LangGraph session with the decision.
        # Encode vehicle override into the decision string so the agent can unpack it
        # without requiring a schema change on resume_session.
        decision_payload = body.decision
        if body.override_vehicle_no and body.decision == "approved":
            decision_payload = f"approved_override:{body.override_vehicle_no}"
        try:
            from workflows.graph.runner import resume_session
            await resume_session(session_id, decision_payload)
            logger.info("[ok] graph resumed  session=%s  decision=%s", session_id, decision_payload)
        except Exception as exc:
            logger.warning("Could not resume session graph: %s", exc)

        await broadcast(session_id, {
            "type": "approval_decided",
            "approval_id": approval_id,
            "decision": body.decision,
            "approver_id": ctx.user_id,
        })

    logger.info("[ok] approval decided  id=%s  decision=%s  by=%s",
                approval_id, body.decision, ctx.username)
    return {"approval_id": approval_id, "decision": body.decision}


@router.get("/approvals/pending")
async def list_all_pending_approvals(
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_role("approver", "admin")),
):
    try:
        if ctx.is_super() and not org_id:
            rows = await hasura.list_pending_approvals_all_orgs()
            # Rows span orgs -- enrich display names per source org.
            by_org: dict[str | None, list[dict]] = {}
            for r in rows:
                by_org.setdefault(r.get("org_id"), []).append(r)
            for row_org, org_rows in by_org.items():
                user_map = await hasura.get_sessions_user_info(
                    list({r["session_id"] for r in org_rows}), org_id=row_org)
                for row in org_rows:
                    row["user_display_name"] = user_map.get(row["session_id"], "—")
        else:
            scope = org_id if ctx.is_super() else ctx.org_id
            rows = await hasura.list_pending_approvals(org_id=scope)
            session_ids = list({r["session_id"] for r in rows})
            user_map = await hasura.get_sessions_user_info(session_ids, org_id=scope)
            for row in rows:
                row["user_display_name"] = user_map.get(row["session_id"], "—")

        return {"approvals": rows}
    except Exception as exc:
        logger.error("[list_all_pending_approvals] error: %s", exc)
        return {"approvals": []}


@router.get("/approvals/{session_id}")
async def list_approvals(
    session_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    org = org_id if ctx.is_super() else ctx.org_id
    await authorized_session(session_id, ctx, org_id_hint=org)
    approvals = await hasura.query(
        """
        query GetApprovals($session_id: uuid!) {
          hospilot_app_approval_tasks: {P}hospilot_app_approval_tasks(
            where: {session_id: {_eq: $session_id}}
          ) {
            id
            agent_id
            action_type
            payload
            status
            created_at
            decided_at
            approver_id
            decision
          }
        }
        """,
        {"session_id": session_id},
        org_id=org,
    )
    return {"approvals": approvals.get("hospilot_app_approval_tasks", [])}
