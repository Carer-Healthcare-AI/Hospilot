import logging

from workflows.unified_executor import execute

logger = logging.getLogger("ambulance")

_TASK_ID = "exec__assign_ambulance"

_SCHEMA = {
    "ambulances": "list of dicts — each has: vehicle_no (str), vehicle_type ('ALS'|'BLS'), status ('Available'|'Busy'|'Offline'), available_since (ISO-8601 str or null — when unit last became Available; null means time unknown), fuel_level (float 0-100), driver_name (str), paramedic_name (str), current_location (str), eta_mins (int)",
    "emergency_type": "str — e.g. 'cardiac arrest', 'trauma', 'stroke', 'respiratory arrest', 'fracture'",
    "request_priority": "str — 'low'|'medium'|'high'",
    "patient_location": "str — ward or address",
}

_OUTPUT_FIELDS = [
    "assigned_vehicle_no",
    "driver_name",
    "paramedic_name",
    "vehicle_type",
    "eta_mins",
    "current_location",
    "escalate",
    "escalation_reason",
    "summary",
]

_DESCRIPTION = (
    "Assign the best available ambulance using type-filtered FIFO dispatch. "
    "Step 1 — filter: only consider units where status=='Available'. "
    "Step 2 — type match: for cardiac/trauma/stroke/respiratory arrest prefer ALS; for all other emergencies prefer BLS. "
    "Step 3 — FIFO within matched type: pick the unit with the earliest available_since (longest waiting). "
    "  Treat null available_since as the most recent (put those last). "
    "Step 4 — fallback: if no unit of the preferred type exists, pick the earliest available_since across all Available units and set escalate=True with escalation_reason explaining the type mismatch. "
    "Always set escalate=True for cardiac, trauma, stroke, or respiratory arrest regardless of unit type. "
    "Return null for assigned_vehicle_no if no Available units exist. "
    "summary must read as one flowing paragraph in plain clinical language for a human "
    "approver — never a field-by-field dump. Example: 'An Advanced Life Support ambulance "
    "has been assigned to the cardiac arrest in Ward A. The team includes Driver Ravi Shankar "
    "and Paramedic Dr. Sanjay Gupta. This case has been marked as high priority and escalated "
    "for immediate attention. The ambulance is currently being dispatched.' "
    "If a value like eta_mins or current_location is null or unknown, omit that detail from "
    "the sentence gracefully rather than writing 'NULL' or 'None'."
)


async def assign_ambulance(
    ambulances: list[dict],
    emergency_type: str,
    request_priority: str,
    patient_location: str,
) -> dict:
    result = await execute(
        task_id=_TASK_ID,
        description=_DESCRIPTION,
        input_schema=_SCHEMA,
        output_fields=_OUTPUT_FIELDS,
        input_data={
            "ambulances": ambulances,
            "emergency_type": emergency_type or "unspecified",
            "request_priority": request_priority or "medium",
            "patient_location": patient_location or "unspecified",
        },
    )
    logger.info(
        "ambulance assignment  available=%d  assigned=%s  escalate=%s",
        sum(1 for a in ambulances if a.get("status") == "Available"),
        result.get("assigned_vehicle_no"),
        result.get("escalate"),
    )
    return result
