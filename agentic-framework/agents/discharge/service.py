import logging

from workflows.unified_executor import execute

logger = logging.getLogger("discharge")

_VITALS_EXCLUDE = {"is_critical", "id", "patient_token", "recorded_at"}

# -- assess_discharge ----------------------------------------------------------

_ASSESS_TASK_ID = "exec__assess_discharge"
_ASSESS_SCHEMA = {
    "pending_task_count": "int — number of incomplete nursing tasks",
    "vitals": "dict or null — keys: temperature (float °C), pulse (int bpm), spo2 (float %), bp_systolic (int mmHg), respiratory_rate (int /min)",
}
_ASSESS_OUTPUT = ["discharge_ready", "blocked_reason", "assessment"]
_ASSESS_DESC = (
    "Assess whether a hospital patient is clinically ready for discharge. "
    "NOT ready if pending_task_count > 0 → blocked_reason='pending_tasks'. "
    "NOT ready if vitals missing → blocked_reason='needs_review'. "
    "NOT ready if temperature >38 or <36°C, pulse <50 or >120, spo2 <94%, "
    "bp_systolic <90 or >160, respiratory_rate <10 or >25 → blocked_reason='vitals_unstable'. "
    "Otherwise discharge_ready=True. "
    "assessment is one concise clinical sentence."
)


async def assess_discharge(
    admission: dict,
    pending_tasks: list[dict],
    vitals: dict | None,
) -> dict:
    vitals_clean = (
        {k: v for k, v in vitals.items() if k not in _VITALS_EXCLUDE}
        if vitals else None
    )
    result = await execute(
        task_id=_ASSESS_TASK_ID,
        description=_ASSESS_DESC,
        input_schema=_ASSESS_SCHEMA,
        output_fields=_ASSESS_OUTPUT,
        input_data={
            "pending_task_count": len(pending_tasks),
            "vitals": vitals_clean,
        },
    )
    logger.info(
        "discharge assessment  admission=%s  ready=%s  reason=%s",
        admission.get("id", "")[:8],
        result.get("discharge_ready"),
        result.get("blocked_reason"),
    )
    return result


# -- generate_discharge_summary ------------------------------------------------

def generate_discharge_summary(
    admission: dict,
    vitals: dict | None,
    completed_task_count: int,
) -> str:
    admitted  = admission.get("admitted_at") or "unknown"
    expected  = admission.get("expected_discharge_at") or "not set"
    status    = admission.get("status") or "admitted"

    if vitals:
        v = {k: v for k, v in vitals.items() if k not in _VITALS_EXCLUDE}
        vitals_line = (
            f"Vitals at discharge: temperature {v.get('temperature', 'N/A')}°C, "
            f"pulse {v.get('pulse', 'N/A')} bpm, "
            f"SpO2 {v.get('spo2', 'N/A')}%, "
            f"BP {v.get('bp_systolic', 'N/A')} mmHg systolic, "
            f"respiratory rate {v.get('respiratory_rate', 'N/A')}/min."
        )
    else:
        vitals_line = "Vitals unavailable at time of discharge assessment."

    note = (
        f"The patient was admitted on {admitted} with an expected discharge of {expected}. "
        f"Current admission status: {status}. "
        f"{completed_task_count} nursing task(s) completed during admission. "
        f"{vitals_line}"
    )
    logger.info(
        "discharge summary generated  admission=%s  chars=%d",
        admission.get("id", "")[:8], len(note),
    )
    return note
