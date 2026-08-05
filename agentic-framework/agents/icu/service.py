import logging

from fhirgw.mappers import observation as obs_map, location as loc_map
from fhirgw.mappers._common import ref_id

from workflows.unified_executor import execute

logger = logging.getLogger("icu")

# --- FHIR accessors -----------------------------------------------------------
# This service is FHIR-native: it receives canonical FHIR R5 resources and reads
# the values it needs through the shared fhirgw extractors.
#   patient "bundle" shape: {"encounter": Encounter, "vitals": [Observation], "bed": Location|None}

def _vview(observations) -> dict:
    """Flat vitals view {spo2, pulse, bp_systolic, ...} from a set of Observations."""
    return obs_map.vitals_to_internal(observations or [])


def _bview(location) -> dict:
    """Flat bed view {ward, ventilation, ...} from a Location (or {} if None)."""
    return loc_map.to_internal(location) if location is not None else {}


_ANALYZE_TASK_ID = "exec__analyze_icu"
_ANALYZE_SCHEMA = {
    "available_beds": "list of dicts — {id, ward, ventilation ('full_ventilator'|'non_invasive'|null)}",
    "icu_patients": "list of dicts — {admission_id, patient_token, ward, ventilation, vitals: {spo2, pulse, bp_systolic, resp_rate, temp}}",
    "escalation_candidates": "list of dicts — {admission_id, patient_token, ward, vitals: {spo2, pulse, bp_systolic, resp_rate, temp}}",
}
_ANALYZE_OUTPUT = ["step_down_candidates", "escalation_candidates", "summary"]
_ANALYZE_DESC = (
    "ICU patient flow recommendations. "
    "step_down_candidates: ICU patients safe to move to ward — only if all vitals stable AND ventilation != 'full_ventilator'. "
    "escalation_candidates: non-ICU patients needing ICU — only if critical vitals. "
    "Urgency: immediate (life-threatening) > urgent (deteriorating) > watch. "
    "step_down_candidates is list of {admission_id, reason, confidence ('high'|'medium'|'low')}. "
    "escalation_candidates is list of {admission_id, reason, urgency ('immediate'|'urgent'|'watch')}. "
    "summary is 2-3 sentences."
)

_RANK_TASK_ID = "exec__rank_icu_admissions"
_RANK_SCHEMA = {
    "patients": "list of dicts — {patient_token, triage_score (int 1-5), chief_complaint (str), vitals: {spo2, pulse, bp_systolic, resp_rate}}",
    "icu_available": "int — total available ICU beds",
    "vent_available": "int — ventilator-capable beds available",
}
_RANK_OUTPUT = ["ranked_requests"]
_RANK_DESC = (
    "Rank incoming ICU admission requests by clinical urgency. "
    "rank 1 = most urgent. "
    "ventilator_dependent: true if spo2 < 88 or severe respiratory distress. "
    "deterioration_risk_high: true if vitals show rapid deterioration (spo2 <90, pulse <40 or >150, bp_systolic <80). "
    "ranked_requests is list of {patient_token, rank, ventilator_dependent (bool), deterioration_risk_high (bool), reason}."
)


def _critical_values(v: dict) -> bool:
    spo2    = v.get("spo2")
    pulse   = v.get("pulse")
    sys_bp  = v.get("bp_systolic")
    rr      = v.get("respiratory_rate")
    temp    = v.get("temperature")
    if spo2  is not None and spo2  < 90:  return True
    if pulse is not None and (pulse < 40 or pulse > 150): return True
    if sys_bp is not None and (sys_bp < 80 or sys_bp > 180): return True
    if rr    is not None and (rr < 8 or rr > 35): return True
    if temp  is not None and (float(temp) > 40 or float(temp) < 34): return True
    return False


def _stable_values(v: dict) -> bool:
    spo2    = v.get("spo2")
    pulse   = v.get("pulse")
    sys_bp  = v.get("bp_systolic")
    rr      = v.get("respiratory_rate")
    temp    = v.get("temperature")
    if spo2  is None or spo2  < 95:  return False
    if pulse is None or not (50 <= pulse <= 120): return False
    if sys_bp is None or not (90 <= sys_bp <= 160): return False
    if rr    is None or not (10 <= rr <= 25): return False
    if temp  is None or not (36.0 <= float(temp) <= 38.5): return False
    return True


def is_critical(observations) -> bool:
    """Rule-based pre-filter on a set of FHIR Observations -- worth sending to Claude."""
    return _critical_values(_vview(observations))


def is_stable(observations) -> bool:
    """All key vitals within step-down-safe ranges (FHIR Observations)."""
    return _stable_values(_vview(observations))


async def rank_icu_admissions(patients: list[dict], available_beds: list) -> dict:
    vent_available = sum(1 for b in available_beds if _bview(b).get("ventilation"))

    def _fmt(p: dict) -> dict:
        v = p.get("vitals") or {}
        return {
            "patient_token":   p.get("patient_token"),
            "triage_score":    p.get("triage_score"),
            "chief_complaint": p.get("chief_complaint", "unknown"),
            "vitals": {
                "spo2":        v.get("spo2"),
                "pulse":       v.get("pulse"),
                "bp_systolic": v.get("bp_systolic"),
                "resp_rate":   v.get("respiratory_rate") or v.get("resp_rate"),
            },
        }

    result = await execute(
        task_id=_RANK_TASK_ID,
        description=_RANK_DESC,
        input_schema=_RANK_SCHEMA,
        output_fields=_RANK_OUTPUT,
        input_data={
            "patients": [_fmt(p) for p in patients],
            "icu_available": len(available_beds),
            "vent_available": vent_available,
        },
    )
    ranked = result.get("ranked_requests", [])
    logger.info(
        "rank_icu_admissions  patients=%d  ventilator_dependent=%d  deterioration_high=%d",
        len(ranked),
        sum(1 for r in ranked if r.get("ventilator_dependent")),
        sum(1 for r in ranked if r.get("deterioration_risk_high")),
    )
    return result


async def analyze_icu(
    icu_patients: list[dict],          # [{encounter, vitals:[Observation], bed:Location}, ...]
    escalation_candidates: list[dict],  # same shape, pre-filtered critical
    available_beds: list,               # [Location, ...]
) -> dict:
    step_down_eligible = [
        p for p in icu_patients
        if is_stable(p.get("vitals"))
        and _bview(p.get("bed")).get("ventilation") != "full_ventilator"
    ]

    def _fmt_patient(p: dict) -> dict:
        enc = p["encounter"]
        v   = _vview(p.get("vitals"))
        bv  = _bview(p.get("bed"))
        token = ref_id(getattr(enc, "subject", None)) or ""
        period = getattr(enc, "actualPeriod", None)
        start = getattr(period, "start", None) if period else None
        return {
            "admission_id":  (enc.id or "")[:8],
            "patient_token": token[:8],
            "ward":          bv.get("ward"),
            "ventilation":   bv.get("ventilation"),
            "admitted_at":   str(start) if start else None,
            "vitals": {
                "spo2":        v.get("spo2"),
                "pulse":       v.get("pulse"),
                "bp_systolic": v.get("bp_systolic"),
                "resp_rate":   v.get("respiratory_rate"),
                "temp":        v.get("temperature"),
            },
        }

    ventilated_count = sum(
        1 for b in available_beds if _bview(b).get("ventilation") == "full_ventilator"
    )

    result = await execute(
        task_id=_ANALYZE_TASK_ID,
        description=_ANALYZE_DESC,
        input_schema=_ANALYZE_SCHEMA,
        output_fields=_ANALYZE_OUTPUT,
        input_data={
            "available_beds": [
                {"id": (b.id or "")[:8], "ward": _bview(b).get("ward"), "ventilation": _bview(b).get("ventilation")}
                for b in available_beds
            ],
            "icu_patients": [_fmt_patient(p) for p in step_down_eligible] or [_fmt_patient(p) for p in icu_patients[:10]],
            "escalation_candidates": [_fmt_patient(p) for p in escalation_candidates[:10]],
        },
    )

    # A patient can't be both step-down and escalation -- escalation takes priority
    escalation_tokens = {c.get("patient_token") for c in result.get("escalation_candidates", []) if c.get("patient_token")}
    escalation_ids    = {c.get("admission_id")   for c in result.get("escalation_candidates", [])}
    result["step_down_candidates"] = [
        c for c in result.get("step_down_candidates", [])
        if c.get("patient_token") not in escalation_tokens
        and c.get("admission_id") not in escalation_ids
    ]

    logger.info(
        "ICU analysis  step_down=%d  escalations=%d",
        len(result.get("step_down_candidates", [])),
        len(result.get("escalation_candidates", [])),
    )
    return result
