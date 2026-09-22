import logging

from workflows.unified_executor import execute

logger = logging.getLogger("bed_prediction")

_TASK_ID = "exec__forecast_capacity"
_SCHEMA = {
    "total_beds": "int",
    "occupied_beds": "int",
    "available_beds": "int",
    "occupancy_pct": "float — percentage 0-100",
    "icu_occupied": "int",
    "icu_total": "int",
    "icu_pct": "float — ICU occupancy percentage 0-100",
    "ventilated_available": "int — ICU beds with ventilator available",
    "discharge_ready_now": "int — patients clinically ready to discharge now",
    "discharge_4h": "int — expected discharges within 4 hours",
    "discharge_24h": "int — expected discharges within 24 hours",
    "ctas_1_2": "int — ER patients CTAS 1-2 (almost certainly admitted)",
    "ctas_3": "int — ER patients CTAS 3 (likely admitted)",
    "est_admissions": "int — total estimated incoming admissions",
    "critical_backlog": "int — non-ICU patients with critical vitals needing ICU",
}
_OUTPUT_FIELDS = [
    "beds_freeing_4h",
    "beds_freeing_24h",
    "beds_needed",
    "icu_saturation_pct",
    "overflow_risk",
    "icu_risk",
    "forecast",
    "recommended_actions",
]
_DESCRIPTION = (
    "Hospital capacity forecast. "
    "beds_needed = est_admissions + critical_backlog. "
    "beds_freeing_4h = discharge_ready_now + discharge_4h. "
    "beds_freeing_24h = discharge_24h. "
    "icu_saturation_pct = round(icu_occupied / max(icu_total, 1) * 100). "
    "overflow_risk: 'high' if beds_needed > beds_freeing_4h AND icu_pct > 85; "
    "'medium' if beds_needed >= beds_freeing_4h OR icu_pct > 75; 'low' otherwise. "
    "icu_risk: 'high' if icu_pct > 90; 'medium' if icu_pct > 75; 'low' otherwise. "
    "forecast is a 2-3 sentence plain-language summary. "
    "recommended_actions is a list of 1-3 specific action strings."
)


async def forecast_capacity(snapshot: dict) -> dict:
    result = await execute(
        task_id=_TASK_ID,
        description=_DESCRIPTION,
        input_schema=_SCHEMA,
        output_fields=_OUTPUT_FIELDS,
        input_data=snapshot,
    )
    logger.info(
        "capacity forecast  overflow=%s  icu=%s  beds_needed=%d  freeing_4h=%d",
        result.get("overflow_risk"),
        result.get("icu_risk"),
        result.get("beds_needed", 0),
        result.get("beds_freeing_4h", 0),
    )
    return result
