"""Escalating approval workflow.

Runs as a Temporal workflow per approval so durable sleep handles escalation
timing instead of the external reaper.

Ladder (level increments on each timeout):
  0 → created by the originating activity
  1, 2, 3 → escalated; a new approval row is created at each level
  > MAX_LEVEL → auto-rejected; graph resumed with "rejected"

The `decide` signal is sent by POST /api/approvals/{id}/decide. On signal the
workflow exits -- the API handler already resumed the graph. On auto-reject the
workflow calls auto_reject_approval_activity which resumes the graph itself.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

ESCALATION_TIMEOUT_SECS = 300
MAX_LEVEL = 3


@dataclass
class EscalatingApprovalInput:
    session_id: str
    agent_id: str
    action_type: str
    payload: dict
    approval_id: str


@dataclass
class EscalateApprovalInput:
    session_id: str
    previous_approval_id: str
    agent_id: str
    action_type: str
    payload: dict
    escalation_level: int


@dataclass
class AutoRejectInput:
    session_id: str
    approval_id: str
    action_type: str


_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1))
_ACTIVITY_TIMEOUT = timedelta(seconds=30)


@workflow.defn
class EscalatingApprovalWorkflow:
    def __init__(self) -> None:
        self._decided = False

    @workflow.signal
    def decide(self) -> None:
        self._decided = True

    @workflow.run
    async def run(self, inp: EscalatingApprovalInput) -> dict:
        level = 0
        approval_id = inp.approval_id

        while True:
            try:
                await workflow.wait_condition(
                    lambda: self._decided,
                    timeout=timedelta(seconds=ESCALATION_TIMEOUT_SECS),
                )
                # User submitted a decision -- API already resumed the graph.
                return {"decided_by_user": True, "level": level}
            except asyncio.TimeoutError:
                level += 1
                if level > MAX_LEVEL:
                    await workflow.execute_activity(
                        "auto_reject_approval_activity",
                        args=[AutoRejectInput(
                            session_id=inp.session_id,
                            approval_id=approval_id,
                            action_type=inp.action_type,
                        )],
                        start_to_close_timeout=_ACTIVITY_TIMEOUT,
                        retry_policy=_ACTIVITY_RETRY,
                    )
                    return {"decided_by_user": False, "level": level, "auto_rejected": True}

                result = await workflow.execute_activity(
                    "escalate_approval_activity",
                    args=[EscalateApprovalInput(
                        session_id=inp.session_id,
                        previous_approval_id=approval_id,
                        agent_id=inp.agent_id,
                        action_type=inp.action_type,
                        payload=inp.payload,
                        escalation_level=level,
                    )],
                    start_to_close_timeout=_ACTIVITY_TIMEOUT,
                    retry_policy=_ACTIVITY_RETRY,
                )
                approval_id = result["approval_id"]
