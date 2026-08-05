"""Shared helper: start the EscalatingApprovalWorkflow for a newly created approval."""

import logging

from config import settings

logger = logging.getLogger(__name__)


async def start_escalating_approval(
    session_id: str,
    approval_id: str,
    agent_id: str,
    action_type: str,
    payload: dict,
) -> None:
    from temporalio.client import RPCError
    from temporalio.service import RPCStatusCode
    from workflows.temporal.client import get_temporal_client
    from workflows.temporal.workflow.escalating_approval_workflow import (
        EscalatingApprovalInput,
        EscalatingApprovalWorkflow,
    )

    wf_id = f"approval-escalation:{session_id}:{agent_id}:{action_type}"
    client = await get_temporal_client()
    try:
        await client.start_workflow(
            EscalatingApprovalWorkflow.run,
            EscalatingApprovalInput(
                session_id=session_id,
                agent_id=agent_id,
                action_type=action_type,
                payload=payload,
                approval_id=approval_id,
            ),
            id=wf_id,
            task_queue=settings.temporal_task_queue,
        )
        logger.info("escalation workflow started  wf_id=%s", wf_id)
    except RPCError as e:
        if e.status == RPCStatusCode.ALREADY_EXISTS:
            logger.info("escalation workflow already running (idempotent)  wf_id=%s", wf_id)
        else:
            raise


async def signal_escalation_decided(
    session_id: str,
    agent_id: str,
    action_type: str,
) -> None:
    """Signal a running EscalatingApprovalWorkflow that its approval was decided, so it
    stops its timer (no further escalation / auto-reject). Best-effort: swallows every
    error (Temporal down, workflow already finished / never started) -- the decision is
    already applied to the LangGraph flow regardless. Shared by the human decide endpoint
    and the Phase 5 policy auto-approve path."""
    if not (agent_id and action_type):
        return
    try:
        from workflows.temporal.client import get_temporal_client
        from workflows.temporal.workflow.escalating_approval_workflow import (
            EscalatingApprovalWorkflow,
        )

        wf_id = f"approval-escalation:{session_id}:{agent_id}:{action_type}"
        client = await get_temporal_client()
        handle = client.get_workflow_handle(wf_id)
        await handle.signal(EscalatingApprovalWorkflow.decide)
        logger.info("escalation workflow signalled  wf_id=%s", wf_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not signal escalation workflow: %s", exc)
