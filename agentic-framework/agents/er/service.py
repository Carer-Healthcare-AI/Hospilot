import logging

from fhirgw.mappers import observation as obs_map
from fhirgw.mappers._common import reason_text

logger = logging.getLogger("er_triage")


# -- NEWS2 parameter scoring --------------------------------------------------

def _score_resp_rate(rr):
    if rr is None: return 0
    if rr <= 8:    return 3
    if rr <= 11:   return 1
    if rr <= 20:   return 0
    if rr <= 24:   return 2
    return 3

def _score_spo2(spo2):
    if spo2 is None: return 0
    if spo2 <= 91:   return 3
    if spo2 <= 93:   return 2
    if spo2 <= 95:   return 1
    return 0

def _score_systolic_bp(sbp):
    if sbp is None: return 0
    if sbp <= 90:   return 3
    if sbp <= 100:  return 2
    if sbp <= 110:  return 1
    if sbp <= 219:  return 0
    return 3

def _score_pulse(hr):
    if hr is None: return 0
    if hr <= 40:   return 3
    if hr <= 50:   return 1
    if hr <= 90:   return 0
    if hr <= 110:  return 1
    if hr <= 130:  return 2
    return 3

def _score_temp(temp):
    if temp is None: return 0
    if temp <= 35.0: return 3
    if temp <= 36.0: return 1
    if temp <= 38.0: return 0
    if temp <= 39.0: return 1
    return 2


def _news2(vitals: dict | None) -> tuple[int, list[str]]:
    """Return (total_news2_score, [reason_strings])."""
    v = vitals or {}
    checks = [
        (_score_resp_rate(v.get("resp_rate")),     v.get("resp_rate"),     "RR",    ""),
        (_score_spo2(v.get("spo2")),               v.get("spo2"),          "SpO2",  "%"),
        (_score_systolic_bp(v.get("bp_systolic")), v.get("bp_systolic"),   "SBP",   ""),
        (_score_pulse(v.get("pulse")),             v.get("pulse"),         "HR",    ""),
        (_score_temp(v.get("temp")),               v.get("temp"),          "Temp",  "°C"),
    ]
    total = 0
    reasons = []
    for pts, val, label, unit in checks:
        total += pts
        if pts > 0:
            reasons.append(f"{label} {val}{unit} (+{pts})")
    return total, reasons


def _news2_to_ctas(news2: int, any_param_is_3: bool) -> int:
    """Map NEWS2 total to CTAS 1-5.

    NEWS2 clinical risk bands → CTAS:
      0      → 5 (Non-Urgent)
      1-2    → 4 (Less Urgent)
      3-4    → 3 (Urgent)
      5-6    → 2 (Emergent)
      ≥7     → 1 (Resuscitation)
    Any single parameter = 3 escalates to at least CTAS 2.
    """
    if news2 >= 7:                    return 1
    if news2 >= 5 or any_param_is_3:  return 2
    if news2 >= 3:                    return 3
    if news2 >= 1:                    return 4
    return 5


# -- Public interface ---------------------------------------------------------

def _triage_one(patient: dict) -> dict:
    visit_id        = patient["visit_id"]
    chief_complaint = patient.get("chief_complaint") or "unknown"
    vitals          = patient.get("vitals")
    v               = vitals or {}

    news2, reasons = _news2(vitals)

    spo2             = v.get("spo2")
    pulse            = v.get("pulse")
    spo2_critical    = spo2 is not None and spo2 < 90

    any_param_3 = any([
        _score_resp_rate(v.get("resp_rate")) == 3,
        _score_spo2(spo2) == 3,
        _score_systolic_bp(v.get("bp_systolic")) == 3,
        _score_pulse(pulse) == 3,
        _score_temp(v.get("temp")) == 3,
    ])

    ctas   = _news2_to_ctas(news2, any_param_3)
    reason = (", ".join(reasons) if reasons else "vitals within normal limits") + f" → NEWS2 {news2}"

    return {
        "visit_id":                visit_id,
        "score":                   ctas,
        "news2_score":             news2,
        "reason":                  reason,
        "needs_vitals":            vitals is None,
        "cardiac_arrest_suspected": spo2_critical and (pulse is None or pulse == 0),
        "spo2_critical":           spo2_critical,
        "protocol":                None,
        "needs_specialist":        ctas <= 2,
    }


async def triage_er_visits(visits_with_vitals: list[dict]) -> list[dict]:
    """FHIR-native: each item is {"encounter": Encounter(EMER), "vitals": [Observation], "wait_minutes": int}."""
    results = []
    for p in visits_with_vitals:
        enc = p["encounter"]
        v   = obs_map.vitals_to_internal(p.get("vitals") or [])
        patient = {
            "visit_id":        enc.id,
            "chief_complaint": reason_text(enc.reason) or "unknown",
            "wait_minutes":    p.get("wait_minutes", 0),
            "vitals": {
                "spo2":        v.get("spo2"),
                "pulse":       v.get("pulse"),
                "bp_systolic": v.get("bp_systolic"),
                "resp_rate":   v.get("respiratory_rate"),
                "temp":        v.get("temperature"),
            } if v else None,
        }
        results.append(_triage_one(patient))

    critical = [t for t in results if t["score"] <= 2]
    logger.info(
        "ER triage  total=%d  ctas1=%d  ctas2=%d",
        len(results),
        sum(1 for t in results if t["score"] == 1),
        sum(1 for t in results if t["score"] == 2),
    )
    if critical:
        logger.warning("CRITICAL ER patients: %s", [t["visit_id"] for t in critical])

    return results
