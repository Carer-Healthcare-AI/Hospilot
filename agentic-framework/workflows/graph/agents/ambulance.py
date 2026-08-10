"""Ambulance agent body -- approval flow (15-min window in the original).

Resume-aware HITL: fetch fleet -> Claude assignment -> create approval -> interrupt;
on resume, confirm dispatch on approval.
"""

import logging

from workflows.graph import hitl
from workflows.graph.step_rec import emit_step_recommendation
from workflows.graph.planning import should_run_task
from workflows.graph.agents._activity import run_activity

from agents.ambulance.activities import (
    get_available_ambulances, assign_ambulance_activity, create_ambulance_approval,
    confirm_ambulance_dispatch,
    AmbulanceAssignInput, AmbulanceApprovalInput, AmbulanceConfirmInput,
)

logger = logging.getLogger(__name__)


async def _ambulance_finalize(sid: str, pending: dict, decision: str) -> dict:
    assignment = pending["vars"]["assignment"]

    # Defend against a mis-delivered resume value. When this approval interrupt and a
    # sibling interrupt (e.g. patient-identification, whose resume is {"mobiles": [...]})
    # are parked on the same thread, LangGraph can deliver one thread's Command(resume=)
    # to the other. A dict here is such a stray value, not our decision -- coerce it to a
    # decision string if it carries one, else to "" (treated as not-approved below), the
    # same way the other approval agents tolerate a non-"approved" value. Prevents a
    # crash on `decision.startswith` (a bare str assumption).
    if isinstance(decision, dict):
        decision = decision.get("decision") or decision.get("action") or ""
    if not isinstance(decision, str):
        decision = ""

    # Unpack vehicle override: "approved_override:AMB-03" → swap assignment details
    if decision.startswith("approved_override:"):
        override_no = decision.split(":", 1)[1]
        fleet = pending["vars"].get("available_ambulances", [])
        override = next((a for a in fleet if a.get("vehicle_no") == override_no), None)
        if override:
            assignment = {
                **assignment,
                "assigned_vehicle_no": override["vehicle_no"],
                "vehicle_type":        override.get("vehicle_type", assignment.get("vehicle_type")),
                "driver_name":         override.get("driver_name", assignment.get("driver_name")),
                "paramedic_name":      override.get("paramedic_name", assignment.get("paramedic_name")),
                "eta_mins":            override.get("eta_mins", assignment.get("eta_mins")),
                "current_location":    override.get("current_location", assignment.get("current_location")),
                "summary":             f"Manually selected by approver: {override_no}",
            }
        decision = "approved"

    if decision != "approved":
        if decision == "timeout":
            return {"status": "timeout", "error": "Dispatch approval timed out after 15 min"}
        return {"status": "rejected", "vehicle_no": assignment.get("assigned_vehicle_no")}

    ta_results = pending["ta_results"]
    task_plan = pending["task_plan"]
    if await should_run_task("ta_confirm_ambulance_dispatch", "sa_ambulance_dispatch", ta_results, task_plan):
        await run_activity(confirm_ambulance_dispatch, AmbulanceConfirmInput(session_id=sid, assignment=assignment))

    return {
        "status": "dispatched",
        "vehicle_no": assignment.get("assigned_vehicle_no"),
        "driver": assignment.get("driver_name"),
        "paramedic": assignment.get("paramedic_name"),
        "eta_mins": assignment.get("eta_mins"),
        "escalate": assignment.get("escalate", False),
        "summary": assignment.get("summary", ""),
    }


async def run_ambulance_body(sid: str, ctx: dict) -> dict:
    base = "ambulance_agent"
    pending = await hitl.load_pending(sid, base)
    if pending is not None:
        decision = hitl.await_decision({"kind": "ambulance_approval", "session_id": sid, "agent_id": base})
        await hitl.clear_pending(sid, base)
        return await _ambulance_finalize(sid, pending, decision)

    task_plan: dict = ctx.get("_task_plan", {})
    ta_results: dict = {}
    emergency_type = ctx.get("emergency_type", "unspecified")
    request_priority = ctx.get("priority", "medium")
    patient_location = ctx.get("patient_location", "unspecified")

    ambulances_result = await run_activity(get_available_ambulances, sid)
    ta_results["ta_get_available_ambulances"] = {"ambulances": ambulances_result}
    ambulances = ambulances_result
    if not ambulances:
        return {"status": "completed", "message": "No ambulance data available"}

    if await should_run_task("ta_assign_ambulance", "sa_ambulance_dispatch", ta_results, task_plan):
        ta_results["ta_assign_ambulance"] = await run_activity(assign_ambulance_activity, AmbulanceAssignInput(
            session_id=sid, ambulances=ambulances, emergency_type=emergency_type,
            request_priority=request_priority, patient_location=patient_location))

    assignment = ta_results.get("ta_assign_ambulance", {})
    if not assignment.get("assigned_vehicle_no"):
        return {"status": "completed", "message": "No available ambulance for this request",
                "summary": assignment.get("summary", "")}

    if await should_run_task("ta_create_ambulance_approval", "sa_ambulance_dispatch", ta_results, task_plan):
        ta_results["ta_create_ambulance_approval"] = await run_activity(
            create_ambulance_approval,
            AmbulanceApprovalInput(
                session_id=sid, assignment=assignment,
                emergency_type=emergency_type, available_ambulances=ambulances,
            ),
        )

    await hitl.save_pending(sid, base, {
        "ta_results": ta_results, "task_plan": task_plan,
        "vars": {"assignment": assignment, "available_ambulances": ambulances},
    })
    await emit_step_recommendation(
        sid, agent_id=base, kind="ambulance_dispatch",
        headline=(f"Dispatch {assignment.get('assigned_vehicle_no')}"
                  f" ({assignment.get('vehicle_type')}) -- ETA {assignment.get('eta_mins')} min"),
        actions=[f"Dispatch ambulance {assignment.get('assigned_vehicle_no')} to {patient_location}"],
        rationale=assignment.get("summary") or f"{emergency_type} emergency, priority {request_priority}",
        risk="high" if assignment.get("escalate") else "medium",
        extras={"eta_mins": assignment.get("eta_mins"),
                "escalation_reason": assignment.get("escalation_reason")},
    )
    hitl.await_decision({"kind": "ambulance_approval", "session_id": sid, "agent_id": base,
                         "action_type": "ambulance_dispatch",
                         "risk": "high" if assignment.get("escalate") else "medium"})
