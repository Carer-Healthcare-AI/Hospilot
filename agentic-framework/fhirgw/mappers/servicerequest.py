"""lab order -> FHIR ServiceRequest (read-only; used by lab_agent).

Inbound from CarerOS, ServiceRequest is parsed directly by `fhir.resources`; this
mapper only builds one from a local `hospilot_lab_orders` row for the Hasura
fallback. R5 `ServiceRequest.code` is a CodeableReference (`concept`).
"""

from fhir.resources.servicerequest import ServiceRequest

from fhirgw import terminology as T
from fhirgw.mappers._common import parse_dt_safe

_PRIORITY_MAP = {"STAT": "stat", "Urgent": "urgent", "Routine": "routine"}
_STATUS_MAP = {"Pending": "active", "In Progress": "on-hold", "Completed": "completed"}


def to_fhir(row: dict) -> ServiceRequest:
    kwargs: dict = {
        "id": str(row["id"]),
        "status": _STATUS_MAP.get(row.get("status"), "active"),
        "intent": "order",
        "category": [{"coding": [{"system": T.SYS_OBS_CATEGORY, "code": "laboratory"}]}],
        "code": {"concept": {"text": row.get("test_name") or "Lab order"}},
        # R5 ServiceRequest.subject is required
        "subject": {"reference": f"Patient/{row.get('patient_token') or 'unknown'}"},
    }
    if row.get("priority"):
        kwargs["priority"] = _PRIORITY_MAP.get(row["priority"], "routine")
    authored = parse_dt_safe(row.get("ordered_at") or row.get("created_at"))
    if authored:
        kwargs["authoredOn"] = authored
    return ServiceRequest(**kwargs)
