"""nursing task <-> FHIR Task (read-only; used by staff/discharge/notification).

Inbound from CarerOS the Task is parsed directly by `fhir.resources`; this mapper
only builds Task from local `hospilot_nursing_tasks` rows for the Hasura fallback.
"""

from fhir.resources.task import Task

from fhirgw.mappers._common import parse_dt_safe

# hospilot status (`completed` bool / status string) is collapsed: incomplete rows
# come from queries that already filter completed=false, so they're 'requested'.
_STATUS_MAP = {
    "pending": "requested", "open": "requested", "requested": "requested",
    "in_progress": "in-progress", "in-progress": "in-progress",
    "completed": "completed", "done": "completed", "cancelled": "cancelled",
}


def to_fhir(row: dict) -> Task:
    raw_status = str(row.get("status") or "").lower()
    kwargs: dict = {
        "id": str(row["id"]),
        "status": _STATUS_MAP.get(raw_status, "requested"),
        "intent": "order",
        "description": row.get("task") or row.get("description") or "Nursing task",
    }
    if row.get("admission_id"):
        kwargs["for"] = {"reference": f"Encounter/{row['admission_id']}"}
    due = parse_dt_safe(row.get("due_at") or row.get("scheduled_at"))
    if due:
        kwargs["executionPeriod"] = {"start": due}
    return Task(**kwargs)
