"""Agent metadata endpoints.

The inter-agent orchestration that used to live here (the run_agent dispatcher,
A2A HTTP handoff, parallel-group Redis barriers, conditional-edge evaluation, and
session synthesis) has moved to the LangGraph runtime under src/graph/:
  - dispatch / nodes        -> graph.nodes + graph.agents.*
  - conditional edges/skips -> graph.conditions
  - synthesis               -> graph.synthesis
  - execution driver        -> graph.runner (invoked from api.sessions)

This module now only serves the agent cards + the DB registry passthrough.
"""

import logging

from fastapi import APIRouter, HTTPException, Depends

from api.routes.auth import require_active_user
from config import settings
from db.hasura import hasura

logger = logging.getLogger(__name__)
router = APIRouter()


def _base_id(agent_id: str) -> str:
    """Strip instance suffix: 'bed_agent:after_icu' -> 'bed_agent'."""
    return agent_id.split(":")[0]


# -- Agent Cards ---------------------------------------------------------------

_AGENT_CARDS = {
    "bed_agent": {
        "name": "Bed Agent",
        "description": "Finds available beds, ranks by patient acuity, and reserves with human approval.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "bed_reservation", "name": "Bed Reservation"}],
    },
    "er_agent": {
        "name": "ER Agent",
        "description": "Monitors ER queue and checks for boarding/SLA breaches.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "er_triage", "name": "ER Triage Monitor"}],
    },
    "icu_agent": {
        "name": "ICU Agent",
        "description": "Checks ICU census and step-down candidates.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "icu_census", "name": "ICU Census Check"}],
    },
    "staff_agent": {
        "name": "Staff Agent",
        "description": "Monitors nurse-patient ratios and float pool availability.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "staff_ratio", "name": "Staff Ratio Monitor"}],
    },
    "discharge_agent": {
        "name": "Discharge Agent",
        "description": "Identifies discharge-ready patients and flags barriers.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "discharge_readiness", "name": "Discharge Readiness Check"}],
    },
    "pharmacy_agent": {
        "name": "Pharmacy Agent",
        "description": "Checks drug inventory for critical shortages.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "stock_monitor", "name": "Drug Stock Monitor"}],
    },
    "ot_agent": {
        "name": "OT Scheduling Agent",
        "description": "Reviews scheduled OT cases against post-op bed availability and flags conflicts.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "ot_schedule", "name": "OT Schedule Review"}],
    },
    "bed_prediction_agent": {
        "name": "Bed Prediction Agent",
        "description": "Forecasts near-term bed availability using discharge horizon, ER pressure, and ICU saturation.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "capacity_forecast", "name": "Capacity Forecast"}],
    },
    "housekeeping_agent": {
        "name": "Housekeeping Agent",
        "description": "Identifies vacated beds needing cleaning and dispatches housekeeping staff.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "bed_turnover", "name": "Bed Turnover"}],
    },
    "revenue_agent": {
        "name": "Revenue Agent",
        "description": "Reviews billing gaps and revenue leakage, package/department profitability, and predicts & prevents insurance claim denial risk.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [
            {"id": "revenue_review",     "name": "Revenue Risk Review"},
            {"id": "denial_prevention",  "name": "Denial Risk Prediction & Prevention"},
        ],
    },
    "billing_agent": {
        "name": "Billing Agent",
        "description": "Validates claims for discrepancies/eligibility/compliance, tracks overdue payments, looks up a patient's invoices, and generates bills.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [
            {"id": "claim_validation",       "name": "Claim Validation"},
            {"id": "billing_optimization",   "name": "Billing Optimization"},
            {"id": "patient_billing",        "name": "Patient Invoice Lookup"},
            {"id": "initiate_billing",       "name": "Bill Generation"},
        ],
    },
    "ambulance_agent": {
        "name": "Ambulance Agent",
        "description": "Assigns the best available ambulance, surfaces ETA and crew, and flags emergency escalation.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [{"id": "ambulance_dispatch", "name": "Ambulance Dispatch"}],
    },
    "appointment_agent": {
        "name": "Appointment Agent",
        "description": "Schedules OPD appointments, sends reminders, and prevents no-shows.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [
            {"id": "scheduling", "name": "Scheduling"},
            {"id": "reminder",   "name": "Reminders"},
            {"id": "no_show",    "name": "No-Show Prevention"},
        ],
    },
    "lab_agent": {
        "name": "Lab Agent",
        "description": "Manages lab operations: sample tracking, TAT optimisation, critical result escalation, analyzer utilisation, QC compliance, test recommendations, and capacity forecasting.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [
            {"id": "sample_tracking",     "name": "Sample Tracking"},
            {"id": "tat_optimization",    "name": "TAT Optimisation"},
            {"id": "critical_escalation", "name": "Critical Result Escalation"},
        ],
    },
    "pharmacy_agent": {
        "name": "Pharmacy Agent",
        "description": "Manages pharmacy operations: dispensing queue, STAT order prioritisation, drug interactions, substitution management, controlled drug tracking, and capacity forecasting.",
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [
            {"id": "dispensing_queue",    "name": "Dispensing Queue"},
            {"id": "drug_interactions",   "name": "Drug Interactions"},
            {"id": "controlled_drugs",    "name": "Controlled Drug Tracking"},
        ],
    },
}


# -- Endpoints -----------------------------------------------------------------

@router.get("/agents/registry", dependencies=[Depends(require_active_user)])
async def get_agent_registry():
    """Return the full agent / sub-agent / task catalog from DB."""
    return await hasura.fetch_agent_registry()


@router.get("/agents/{agent_id}/card", dependencies=[Depends(require_active_user)])
async def get_agent_card(agent_id: str):
    card = _AGENT_CARDS.get(_base_id(agent_id))
    if not card:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_id}")
    return {**card, "url": f"{settings.app_base_url}/agents/{agent_id}"}
