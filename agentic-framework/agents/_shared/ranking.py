import logging

from workflows.unified_executor import execute

logger = logging.getLogger("ranking")

_TASK_ID = "exec__rank_beds"
_SCHEMA = {
    "patient_context": "dict — keys: acuity (int 1-5), required_bed_type (str), current_location (str)",
    "candidate_beds": "list of dicts — each has: id (str), bed_number (str), ward (str), room_type (str), status (str), features (str), ventilation (str), noise_level (str), proximity (str), room_sharing (str)",
}
_OUTPUT_FIELDS = ["ranked_beds", "recommendation"]
_DESCRIPTION = (
    "Rank candidate hospital beds for a patient. "
    "Prioritise clinical match: bed_type should match required_bed_type. "
    "Prefer beds in same ward as current_location. "
    "Deprioritise beds with maintenance_flag=True or is_isolation=True unless clinically required. "
    "ranked_beds is a list of dicts: {bed_id (copy of the bed's id field), score (int 0-100), reason (str)}. "
    "recommendation is one sentence describing the top choice."
)


async def rank_beds(patient_context: dict, candidate_beds: list[dict]) -> dict:
    logger.info(
        "ranking %d candidates  acuity=%s  type=%s",
        len(candidate_beds),
        patient_context.get("acuity", "?"),
        patient_context.get("required_bed_type", "?"),
    )
    result = await execute(
        task_id=_TASK_ID,
        description=_DESCRIPTION,
        input_schema=_SCHEMA,
        output_fields=_OUTPUT_FIELDS,
        input_data={
            "patient_context": patient_context,
            "candidate_beds": candidate_beds,
        },
    )
    ranked = result.get("ranked_beds", [])
    if ranked:
        top = ranked[0]
        logger.info("top bed=%s  score=%s", top.get("bed_id"), top.get("score"))
    else:
        logger.warning("ranking returned no beds")
    return result
