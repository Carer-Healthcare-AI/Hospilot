import logging

from workflows.unified_executor import execute

logger = logging.getLogger("staff")

_TASK_ID = "exec__analyze_staffing"
_SCHEMA = {
    "wards": "list of dicts — each has: ward_name (str), ward_type ('ICU'|'ward'|'HDU'), patients (int), incomplete_tasks (int), overdue_tasks (int)",
    "total_patients": "int",
    "total_tasks": "int",
    "total_overdue": "int",
}
_OUTPUT_FIELDS = ["recommendations", "high_pressure_wards", "summary"]
_DESCRIPTION = (
    "Hospital staffing reallocation analysis. "
    "Flag a ward as high-pressure if overdue_tasks > 5. "
    "Recommend staff moves only between wards of the same type (ICU↔ICU, ward↔ward). "
    "Never recommend reducing ICU staff below 2 nurses. "
    "recommendations is a list of dicts: {from_ward, to_ward, reason, priority ('high'|'medium'|'low')}. "
    "high_pressure_wards is a list of ward_name strings. "
    "summary is a 2-3 sentence staffing assessment."
)


async def analyze_staffing(ward_workload: list[dict]) -> dict:
    result = await execute(
        task_id=_TASK_ID,
        description=_DESCRIPTION,
        input_schema=_SCHEMA,
        output_fields=_OUTPUT_FIELDS,
        input_data={
            "wards": ward_workload,
            "total_patients": sum(w.get("patients", 0) for w in ward_workload),
            "total_tasks":    sum(w.get("incomplete_tasks", 0) for w in ward_workload),
            "total_overdue":  sum(w.get("overdue_tasks", 0) for w in ward_workload),
        },
    )
    logger.info(
        "staffing analysis  wards=%d  recommendations=%d  high_pressure=%s",
        len(ward_workload),
        len(result.get("recommendations", [])),
        result.get("high_pressure_wards", []),
    )
    return result
