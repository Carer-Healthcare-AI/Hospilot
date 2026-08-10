import logging
from dataclasses import dataclass

from temporalio import activity

from api.routes.ws import broadcast
from cache import redis as cache
from db.hasura import hasura
from agents.ambulance.service import assign_ambulance
from workflows.temporal.workflow._escalation import start_escalating_approval
from util.idem import make_idem_key

logger = logging.getLogger(__name__)


@dataclass
class AmbulanceAssignInput:
    session_id: str
    ambulances: list
    emergency_type: str
    request_priority: str
    patient_location: str


@dataclass
class AmbulanceApprovalInput:
    session_id: str
    assignment: dict
    emergency_type: str
    available_ambulances: list


@dataclass
class AmbulanceConfirmInput:
    session_id: str
    assignment: dict


@activity.defn
async def get_available_ambulances(session_id: str) -> list:
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_ambulance_census"})

    ambulances = await cache.get_all_ambulances()

    available_count = sum(1 for a in ambulances if a.get("status") == "Available")
    await broadcast(session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_ambulance_census",
        "result": {"total": len(ambulances), "available": available_count},
    })
    logger.info("ambulance fleet  session=%s  total=%d  available=%d", session_id, len(ambulances), available_count)
    return ambulances


@activity.defn
async def assign_ambulance_activity(inp: AmbulanceAssignInput) -> dict:
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_ambulance_assign"})

    result = await assign_ambulance(
        ambulances=inp.ambulances,
        emergency_type=inp.emergency_type,
        request_priority=inp.request_priority,
        patient_location=inp.patient_location,
    )

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_ambulance_assign",
        "result": {
            "assigned": result.get("assigned_vehicle_no"),
            "eta_mins": result.get("eta_mins"),
            "escalate": result.get("escalate", False),
        },
    })
    return result


@activity.defn
async def create_ambulance_approval(inp: AmbulanceApprovalInput) -> dict:
    # Only surface Available units to the approver — no point showing Busy/Offline.
    available = [a for a in inp.available_ambulances if a.get("status") == "Available"]
    approval = await hasura.create_approval_task(
        session_id=inp.session_id,
        agent_id="ambulance_agent",
        action_type="ambulance_dispatch",
        payload={
            "assignment": inp.assignment,
            "emergency_type": inp.emergency_type,
            "available_ambulances": available,
        },
        idempotency_key=make_idem_key(
            "ambulance_dispatch", inp.session_id,
            (inp.assignment or {}).get("assigned_vehicle_no")),
    )
    await broadcast(inp.session_id, {
        "type": "approval_required",
        "approval_id": approval["id"],
        "action": "ambulance_dispatch",
        "assigned_vehicle": inp.assignment.get("assigned_vehicle_no"),
        "eta_mins": inp.assignment.get("eta_mins"),
        "escalate": inp.assignment.get("escalate", False),
        "escalation_reason": inp.assignment.get("escalation_reason"),
        "summary": inp.assignment.get("summary", ""),
    })
    logger.info("ambulance approval created  session=%s  approval=%s  vehicle=%s",
                inp.session_id, approval["id"], inp.assignment.get("assigned_vehicle_no"))
    await start_escalating_approval(
        session_id=inp.session_id,
        approval_id=approval["id"],
        agent_id="ambulance_agent",
        action_type="ambulance_dispatch",
        payload={"assignment": inp.assignment, "emergency_type": inp.emergency_type},
    )
    return {"approval_id": approval["id"]}


@activity.defn
async def confirm_ambulance_dispatch(inp: AmbulanceConfirmInput) -> dict:
    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="ambulance_agent",
        event_type="ambulance_dispatched",
        payload=inp.assignment,
    )
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_ambulance_confirm",
        "result": {
            "vehicle": inp.assignment.get("assigned_vehicle_no"),
            "eta_mins": inp.assignment.get("eta_mins"),
            "summary": inp.assignment.get("summary", ""),
        },
    })
    logger.info("ambulance dispatched  session=%s  vehicle=%s", inp.session_id, inp.assignment.get("assigned_vehicle_no"))
    return {"status": "dispatched", "vehicle_no": inp.assignment.get("assigned_vehicle_no")}


# -- sa_ambulance_response -----------------------------------------------------

# -- sa_ambulance_fleet_utilization --------------------------------------------

# -- sa_ambulance_availability -------------------------------------------------
