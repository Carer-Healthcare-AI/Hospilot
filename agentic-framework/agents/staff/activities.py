import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from temporalio import activity

from cache import redis as cache
from db.hasura import hasura
from util.idem import make_idem_key
from util.forecast_client import forecast
from workflows.temporal.workflow._escalation import start_escalating_approval
from fhirgw import repository as repo
from fhirgw.mappers._common import ref_id
from agents.staff.service import analyze_staffing
from api.routes.ws import broadcast

logger = logging.getLogger(__name__)


def _clamp(value: float, lo: float, hi: float) -> float:
    """Keep a value inside the forecast API's documented input range."""
    return max(lo, min(hi, value))


def _horizon_from_goal(goal: str) -> str:
    """Map the request goal to a /staffing/nurse-demand forecast_period (6h|12h|24h|3d|7d)."""
    g = (goal or "").lower()
    if "week" in g or "7 day" in g or "7d" in g:
        return "7d"
    if "3 day" in g or "3d" in g or "72h" in g:
        return "3d"
    if "12h" in g or "12 hour" in g:
        return "12h"
    if "6h" in g or "6 hour" in g or "3h" in g or "shift" in g:
        return "6h"
    return "24h"   # "today" / "tomorrow" / unspecified


def _enc_bed_id(enc) -> str | None:
    locs = getattr(enc, "location", None)
    return ref_id(locs[0].location) if locs else None


def _due_before(period, now) -> bool:
    """True if a Task.executionPeriod.start is in the past."""
    start = getattr(period, "start", None) if period else None
    if start is None:
        return False
    try:
        d = start if not isinstance(start, str) else datetime.fromisoformat(start.replace("Z", "+00:00"))
        if getattr(d, "tzinfo", None) is None:
            d = d.replace(tzinfo=timezone.utc)
        return d < now
    except Exception:
        return False


def _period_hour(period) -> int | None:
    """Hour-of-day (0-23, UTC) of a Task.executionPeriod.start, or None."""
    start = getattr(period, "start", None) if period else None
    if start is None:
        return None
    try:
        d = start if not isinstance(start, str) else datetime.fromisoformat(start.replace("Z", "+00:00"))
        if getattr(d, "tzinfo", None) is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.hour
    except Exception:
        return None


@dataclass
class StaffAnalysisInput:
    session_id: str
    ward_workload: list


@dataclass
class StaffApprovalInput:
    session_id: str
    recommendations: list
    high_pressure_wards: list
    summary: str


@dataclass
class StaffConfirmInput:
    session_id: str
    analysis: dict


@dataclass
class AreaStaffingInput:
    session_id: str
    areas: list = field(default_factory=list)   # area keys to scope to; empty = all areas


# G11/G20/G24/G28/lab-staff: map goal phrasing -> staff-area keys (must match
# hospilot.staff_roster.area). Lets "check front-desk / phlebotomy / OT / recovery /
# lab staffing" target the right area instead of only inpatient nursing wards.
_AREA_KEYWORDS: dict[str, list[str]] = {
    "front_desk":        ["front desk", "front-desk", "reception", "registration", "front office"],
    "opd":               ["opd", "outpatient", "clinic staff"],
    "phlebotomy":        ["phlebotomy", "phlebotomist", "sample collection", "blood draw", "blood collection"],
    "ot":                ["operating theatre", "operating room", "ot staff", "theatre staff",
                          "surgical staff", "ot nurse", "ot and recovery", "ot/recovery"],
    "recovery":          ["recovery", "pacu", "post-op", "post op", "recovery bay", "recovery-bay", "recovery nurse"],
    "lab":               ["lab staff", "laboratory staff", "lab technician", "lab tech", "lab staffing"],
    "inpatient_nursing": ["nursing staff", "ward staff", "inpatient nursing", "bedside nurse"],
}


def requested_staff_areas(goal: str) -> list[str]:
    """Area keys explicitly referenced by the goal (empty -> assess all areas)."""
    g = (goal or "").lower()
    return [area for area, kws in _AREA_KEYWORDS.items() if any(k in g for k in kws)]


def _current_shift(now: datetime | None = None) -> str:
    h = (now or datetime.now(timezone.utc)).hour
    if 7 <= h < 15:
        return "day"
    if 15 <= h < 23:
        return "evening"
    return "night"


@activity.defn
async def get_ward_workload(session_id: str) -> list:
    """Aggregate patients and task load per ward."""
    await broadcast(session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_staff_census",
    })

    # FHIR-native: admissions as Encounters (+ bed->ward map), tasks as FHIR Task.
    encounters, ward_by_bed = await repo.all_admissions()
    all_tasks = await repo.incomplete_tasks()

    now = datetime.now(timezone.utc)

    # Build admission(encounter id) -> ward mapping
    admission_ward: dict[str, str] = {}
    ward_patients: dict[str, int] = defaultdict(int)
    for enc in encounters:
        ward = ward_by_bed.get(_enc_bed_id(enc)) or "Unknown"
        admission_ward[enc.id] = ward
        ward_patients[ward] += 1

    # Aggregate tasks per ward (Task.for -> Encounter)
    ward_tasks:   dict[str, int] = defaultdict(int)
    ward_overdue: dict[str, int] = defaultdict(int)
    for t in all_tasks:
        enc_id = ref_id(getattr(t, "for_fhir", None))
        ward = admission_ward.get(enc_id, "Unknown")
        ward_tasks[ward] += 1
        if _due_before(t.executionPeriod, now):
            ward_overdue[ward] += 1

    all_wards = set(ward_patients) | set(ward_tasks)
    workload = [
        {
            "ward":             w,
            "patients":         ward_patients.get(w, 0),
            "incomplete_tasks": ward_tasks.get(w, 0),
            "overdue_tasks":    ward_overdue.get(w, 0),
            "tasks_per_patient": round(
                ward_tasks.get(w, 0) / max(ward_patients.get(w, 1), 1), 1
            ),
        }
        for w in sorted(all_wards)
    ]

    await broadcast(session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_staff_census",
        "result": {
            "wards":            len(workload),
            "total_patients":   sum(w["patients"] for w in workload),
            "total_tasks":      sum(w["incomplete_tasks"] for w in workload),
            "overdue_tasks":    sum(w["overdue_tasks"] for w in workload),
        },
    })
    logger.info("ward workload  session=%s  wards=%d", session_id, len(workload))
    return workload


@activity.defn
async def get_hourly_workload(session_id: str) -> dict:
    """G15: bucket incomplete clinical-task load by hour-of-day so "peak
    understaffed hours" can be computed (Q3). Per-hour task count = demand; per-hour
    overdue count = the existing staff couldn't keep up at that hour (understaffing
    proxy, since no shift roster is modelled). Emits:
      by_hour            {hour: {tasks, overdue}} (non-empty hours only)
      peak_hours         top-3 hours by task volume
      understaffed_hours top-3 hours by overdue count (falls back to peak_hours)
    Downstream (appointment reschedule, G14) reads understaffed_hours via ctx."""
    all_tasks = await repo.incomplete_tasks()
    now = datetime.now(timezone.utc)

    by_hour: dict[int, dict] = {h: {"tasks": 0, "overdue": 0} for h in range(24)}
    for t in all_tasks:
        h = _period_hour(getattr(t, "executionPeriod", None))
        if h is None:
            continue
        by_hour[h]["tasks"] += 1
        if _due_before(getattr(t, "executionPeriod", None), now):
            by_hour[h]["overdue"] += 1

    peak_hours = sorted((h for h, v in by_hour.items() if v["tasks"] > 0),
                        key=lambda h: by_hour[h]["tasks"], reverse=True)[:3]
    overdue_hours = sorted((h for h, v in by_hour.items() if v["overdue"] > 0),
                           key=lambda h: by_hour[h]["overdue"], reverse=True)[:3]
    understaffed_hours = overdue_hours or peak_hours

    result = {
        "by_hour":            {str(h): v for h, v in by_hour.items() if v["tasks"] > 0},
        "peak_hours":         peak_hours,
        "understaffed_hours": understaffed_hours,
        "total_tasks":        sum(v["tasks"] for v in by_hour.values()),
    }
    await broadcast(session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_ratio_monitor",
        "result": {"peak_hours": peak_hours, "understaffed_hours": understaffed_hours},
    })
    logger.info("hourly workload  session=%s  peak=%s  understaffed=%s",
                session_id, peak_hours, understaffed_hours)
    return result


# G37: documentation-type nursing tasks (care notes / charting / signatures /
# shift records). Incomplete or overdue ones are staffing documentation gaps.
_DOC_KEYWORDS = ("note", "chart", "document", "sign", "signature", "record",
                 "assessment", "handover", "intake", "consent", "log")


def _is_doc_task(desc: str) -> bool:
    d = (desc or "").lower()
    return any(k in d for k in _DOC_KEYWORDS)


@activity.defn
async def get_documentation_gaps(session_id: str) -> dict:
    """G37: detect staffing documentation gaps -- incomplete / overdue care notes,
    charting, unsigned orders and shift records -- grouped by ward. Derived from the
    same nursing-task + admission data as ward workload (no new source). staff_agent
    previously measured only nurse-to-patient ratios, never documentation completeness."""
    encounters, ward_by_bed = await repo.all_admissions()
    all_tasks = await repo.incomplete_tasks()
    now = datetime.now(timezone.utc)

    admission_ward: dict[str, str] = {}
    for enc in encounters:
        admission_ward[enc.id] = ward_by_bed.get(_enc_bed_id(enc)) or "Unknown"

    by_ward: dict[str, dict] = defaultdict(lambda: {"pending": 0, "overdue": 0})
    total_pending = total_overdue = 0
    for t in all_tasks:
        if not _is_doc_task(getattr(t, "description", "")):
            continue
        ward = admission_ward.get(ref_id(getattr(t, "for_fhir", None)), "Unknown")
        by_ward[ward]["pending"] += 1
        total_pending += 1
        if _due_before(getattr(t, "executionPeriod", None), now):
            by_ward[ward]["overdue"] += 1
            total_overdue += 1

    wards_out = [{"ward": w, **v} for w, v in sorted(by_ward.items())]
    flagged = [w["ward"] for w in wards_out if w["overdue"] > 0]
    result = {
        "documentation_tasks_pending": total_pending,
        "documentation_tasks_overdue": total_overdue,
        "by_ward":       wards_out,
        "flagged_wards": flagged,
        "has_gaps":      total_pending > 0,
    }
    await broadcast(session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_ratio_monitor",
        "result": {"documentation_tasks_pending": total_pending,
                   "documentation_tasks_overdue": total_overdue, "flagged_wards": flagged},
    })
    logger.info("documentation gaps  session=%s  pending=%d  overdue=%d  flagged=%s",
                session_id, total_pending, total_overdue, flagged)
    return result


@activity.defn
async def get_area_staffing(inp: AreaStaffingInput) -> dict:
    """G11/G20/G24/G28/lab-staff: assess staffing for non-inpatient-nursing areas
    (front desk, OPD, phlebotomy, OT, recovery/PACU, lab) the ward model can't see.

    Reads the staff roster (Redis, Hasura fallback), scopes to the requested areas
    (empty = all) and the current shift, and per area computes capacity = sum(headcount
    * load_per_staff) vs assigned_load -> utilization + understaffed flag + recommended
    additional staff. So "check X staffing" finally targets the right area."""
    roster = await cache.get_all_staff_roster()
    if not roster:
        try:
            roster = await hasura.staff_list_roster(inp.areas or None)
        except Exception:
            roster = []

    shift = _current_shift()
    rows = [r for r in roster if (r.get("shift") or "").lower() == shift] or roster
    wanted = {a.lower() for a in (inp.areas or [])}

    agg: dict[str, dict] = {}
    for r in rows:
        area = (r.get("area") or "").lower()
        if not area or (wanted and area not in wanted):
            continue
        a = agg.setdefault(area, {"area": area, "area_label": r.get("area_label") or area,
                                  "headcount": 0, "assigned_load": 0, "capacity": 0})
        hc = int(r.get("headcount") or 0)
        a["headcount"]     += hc
        a["assigned_load"] += int(r.get("assigned_load") or 0)
        a["capacity"]      += hc * int(r.get("load_per_staff") or 0)

    areas_out: list[dict] = []
    understaffed: list[str] = []
    for area, a in sorted(agg.items()):
        cap, load, hc = a["capacity"], a["assigned_load"], a["headcount"]
        a["utilization"] = round(load / cap, 2) if cap else None
        is_under = hc == 0 or (cap and load > cap) or (cap == 0 and load > 0)
        a["understaffed"] = bool(is_under)
        # staff needed to bring load within capacity, given this area's avg load_per_staff
        rec_add = 0
        if is_under and load > 0:
            avg_ratio = (cap / hc) if hc else 0
            needed = math.ceil(load / avg_ratio) if avg_ratio else hc + 1
            rec_add = max(0, needed - hc)
        a["recommended_additional_staff"] = rec_add
        areas_out.append(a)
        if is_under:
            understaffed.append(area)

    result = {
        "shift":            shift,
        "areas_assessed":   len(areas_out),
        "areas":            areas_out,
        "understaffed_areas": understaffed,
    }
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_ratio_monitor",
        "result": {"shift": shift, "understaffed_areas": understaffed,
                   "areas_assessed": len(areas_out)},
    })
    logger.info("area staffing  session=%s  shift=%s  understaffed=%s",
                inp.session_id, shift, understaffed)
    return result


@activity.defn
async def analyze_staff_workload(inp: StaffAnalysisInput) -> dict:
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_staff_analysis",
    })

    result = await analyze_staffing(inp.ward_workload)

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_staff_analysis",
        "result": {
            "recommendations":    len(result.get("recommendations", [])),
            "high_pressure_wards": result.get("high_pressure_wards", []),
        },
    })
    return result


@activity.defn
async def create_staff_approval(inp: StaffApprovalInput) -> dict:
    approval = await hasura.create_approval_task(
        session_id=inp.session_id,
        agent_id="staff_agent",
        action_type="staff_reallocation",
        payload={
            "recommendations":    inp.recommendations,
            "high_pressure_wards": inp.high_pressure_wards,
            "summary":            inp.summary,
        },
        idempotency_key=make_idem_key(
            "staff_reallocation", inp.session_id, inp.recommendations),
    )
    await broadcast(inp.session_id, {
        "type": "approval_required",
        "approval_id":         approval["id"],
        "action":              "staff_reallocation",
        "recommendation_count": len(inp.recommendations),
        "high_pressure_wards": inp.high_pressure_wards,
        "summary":             inp.summary,
    })
    logger.info("staff approval created  session=%s  approval=%s  recs=%d",
                inp.session_id, approval["id"], len(inp.recommendations))
    await start_escalating_approval(
        session_id=inp.session_id,
        approval_id=approval["id"],
        agent_id="staff_agent",
        action_type="staff_reallocation",
        payload={"recommendations": inp.recommendations,
                 "high_pressure_wards": inp.high_pressure_wards, "summary": inp.summary},
    )
    return {"approval_id": approval["id"]}


@activity.defn
async def confirm_staff_recommendation(inp: StaffConfirmInput) -> dict:
    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="staff_agent",
        event_type="staff_reallocation_approved",
        payload=inp.analysis,
    )
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_staff_confirm",
        "result": {
            "recommendations": len(inp.analysis.get("recommendations", [])),
            "summary": inp.analysis.get("summary", ""),
        },
    })
    logger.info("staff recommendation confirmed  session=%s", inp.session_id)
    return {"status": "confirmed", "recommendations": inp.analysis.get("recommendations", [])}


# -- sa_nurse_demand -----------------------------------------------------------

@activity.defn
async def forecast_nurse_demand(session_id: str, goal: str = "") -> dict:
    """Forecast required nurses over a horizon via the ML service
    (/staffing/nurse-demand), with the staffing gap and patient-care risk.

    Real signals: census (occupied/total beds), current nurses on duty (staff
    roster, current shift), ER/ICU load and discharge outlook. patient_acuity_index
    is derived as the ICU + critical-vitals share of the census (no acuity field is
    tracked). Absenteeism / agency / seasonal indices fall to model defaults.
    Degrades to forecast_available: 0 when the service is unconfigured/down or there
    is no bed data.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_nurse_demand"})

    beds = await hasura.get_beds_summary() or {}
    total = int(beds.get("total_beds") or 0)
    if total <= 0:
        result = {"forecast_available": 0, "reason": "no bed data"}
        logger.info("forecast_nurse_demand  session=%s  no bed data", session_id)
        return result

    occupied = int(beds.get("occupied_beds") or 0)
    icu = int(beds.get("icu_occupied") or 0)
    er_visits = await hasura.get_er_visits() or []
    discharges = await hasura.get_discharge_horizon(24)
    critical = int((await hasura.get_critical_escalation_backlog()) or 0)
    # Surgeries under way (concurrent cases), split by OT priority: anything not
    # tagged 'elective' counts as emergency; untagged defaults to elective (an
    # emergency is always flagged, a routine case may not be).
    ot_surgeries = await hasura.carerOS_get_ot_surgeries() or []
    emergency_ops = sum(1 for s in ot_surgeries
                        if (s.get("priority") or "elective").strip().lower() != "elective")
    elective_ops = len(ot_surgeries) - emergency_ops

    roster = await cache.get_all_staff_roster()
    if not roster:
        try:
            roster = await hasura.staff_list_roster(None)
        except Exception:  # noqa: BLE001 -- roster is best-effort; fall back to empty
            roster = []
    shift = _current_shift()
    rows = [r for r in roster if (r.get("shift") or "").lower() == shift] or roster
    nurses = sum(int(r.get("headcount") or 0) for r in rows
                 if "nurse" in (f"{r.get('role') or ''} {r.get('area') or ''}").lower())
    if nurses == 0:   # roster roles not labelled -- the model here is nursing-centric
        nurses = sum(int(r.get("headcount") or 0) for r in rows)

    acuity = round(_clamp((icu + critical) / max(occupied, 1), 0.25, 0.95), 2) if occupied else 0.5
    nurses_c = int(_clamp(nurses, 0, 5000))
    horizon = _horizon_from_goal(goal)
    payload = {
        "forecast_period":        horizon,
        "occupied_beds":          int(_clamp(occupied, 0, 5000)),
        "total_beds":             int(_clamp(total, 1, 5000)),
        "patient_acuity_index":   acuity,
        "current_nurses_on_duty": nurses_c,
        "er_patient_count":       int(_clamp(len(er_visits), 0, 1100)),
        "icu_patient_count":      int(_clamp(icu, 0, 550)),
        "expected_discharges":    int(_clamp(int(discharges or 0), 0, 1100)),
        "scheduled_admissions":   int(_clamp(int(discharges or 0), 0, 1100)),  # balanced-flow proxy (no admit-rate feed)
        "elective_surgeries":     int(_clamp(elective_ops, 0, 100)),
        "emergency_surgeries":    int(_clamp(emergency_ops, 0, 29)),
        "nurse_absenteeism_rate": 0.05,   # no roster-absence feed; typical default
        "holiday_flag":           0,      # no holiday calendar wired
    }

    forecast_resp = await forecast("/staffing/nurse-demand", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_nurse_demand", "result": result})
        logger.info("forecast_nurse_demand  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    # The model returns predicted_required_nurses + thresholds; the gap, ratio,
    # status and care-risk are derived here (the endpoint returns only the count).
    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    th = (forecast_resp.get("thresholds_applied") or {}) if isinstance(forecast_resp, dict) else {}
    required = next((pred[k] for k in ("predicted_required_nurses", "required_nurses", "value") if k in pred), None)

    if isinstance(required, (int, float)):
        required = int(round(required))
        gap = required - nurses_c                       # signed: +ve means short
        additional = max(gap, 0)
        ratio = round(occupied / required, 2) if required > 0 else None
        if gap >= th.get("care_risk_high", 20):
            care_risk, status = "high", "critical"
        elif gap >= th.get("care_risk_medium", 8):
            care_risk, status = "medium", "understaffed"
        else:
            care_risk, status = "low", "adequate"
    else:
        gap = additional = ratio = None
        care_risk, status = "unknown", "unknown"

    result = {
        "forecast_available":            1,
        "horizon":                       horizon,
        "predicted_required_nurses":     required,
        "additional_nurses_required":    additional,
        "predicted_nurse_patient_ratio": ratio,
        "staffing_gap":                  gap,
        "staffing_status":               status,
        "patient_care_risk":             care_risk,
        "recommended_action":            pred.get("recommended_action") or pred.get("action") or "",
        "current_nurses_on_duty":        nurses_c,
        "patient_acuity_index":          acuity,
        "fallback_used":                 bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }

    if str(care_risk).lower() in ("medium", "high"):
        severity = "critical" if str(care_risk).lower() == "high" else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"Nurse-demand forecast ({horizon}): ~{required} nurses needed "
                        f"(+{result['additional_nurses_required']} gap), care_risk={care_risk} — "
                        f"{result['recommended_action']}"),
        })

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_nurse_demand", "result": result})
    logger.info("forecast_nurse_demand  session=%s  horizon=%s  required=%s  care_risk=%s",
                session_id, horizon, required, care_risk)
    return result


# -- sa_doctor_demand ----------------------------------------------------------

@activity.defn
async def forecast_doctor_demand(session_id: str, goal: str = "") -> dict:
    """Forecast required physicians over a horizon via the ML service
    (/staffing/doctor-demand), with the staffing gap and clinical-capacity risk.

    Real signals: census (occupied beds), ER/ICU load, critical cases and the
    discharge outlook. doctors_on_duty is a proxy -- the staff roster is nursing-
    centric, so we fall back to the count of registered doctor users
    (count_users_by_role), which is not strictly shift-scoped. patient_acuity is
    the derived ICU + critical-vitals share of census. Degrades to
    forecast_available: 0 when the service is unconfigured/down or there is no bed data.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_doctor_demand"})

    beds = await hasura.get_beds_summary() or {}
    total = int(beds.get("total_beds") or 0)
    if total <= 0:
        result = {"forecast_available": 0, "reason": "no bed data"}
        logger.info("forecast_doctor_demand  session=%s  no bed data", session_id)
        return result

    occupied = int(beds.get("occupied_beds") or 0)
    icu = int(beds.get("icu_occupied") or 0)
    er_visits = await hasura.get_er_visits() or []
    discharges = await hasura.get_discharge_horizon(24)
    critical = int((await hasura.get_critical_escalation_backlog()) or 0)

    roster = await cache.get_all_staff_roster()
    if not roster:
        try:
            roster = await hasura.staff_list_roster(None)
        except Exception:  # noqa: BLE001 -- roster is best-effort
            roster = []
    shift = _current_shift()
    rows = [r for r in roster if (r.get("shift") or "").lower() == shift] or roster
    doctors = sum(int(r.get("headcount") or 0) for r in rows
                  if any(t in (f"{r.get('role') or ''} {r.get('area') or ''}").lower()
                         for t in ("doctor", "physician", "consultant", "surgeon")))
    if doctors == 0:   # roster is nursing-centric -> proxy with registered doctor users
        try:
            doctors = int(await hasura.count_users_by_role("doctor") or 0)
        except Exception:  # noqa: BLE001
            doctors = 0

    acuity = round(_clamp((icu + critical) / max(occupied, 1), 0.05, 1.0), 2) if occupied else 0.5
    horizon = _horizon_from_goal(goal)
    payload = {
        "forecast_period":  horizon,
        "occupied_beds":    int(_clamp(occupied, 0, 100000)),
        "patient_acuity":   acuity,
        "doctors_on_duty":  int(_clamp(doctors, 0, 100000)),
        "er_patients":      int(_clamp(len(er_visits), 0, 100000)),
        "icu_patients":     int(_clamp(icu, 0, 100000)),
        "critical_cases":   int(_clamp(critical, 0, 100000)),
        "expected_discharges": int(_clamp(int(discharges or 0), 0, 100000)),
    }

    forecast_resp = await forecast("/staffing/doctor-demand", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_doctor_demand", "result": result})
        logger.info("forecast_doctor_demand  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    # Envelope: flat dict (per the sample) or {"prediction": [{...}]}.
    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    required = next((pred[k] for k in ("predicted_required_doctors", "required_doctors", "value") if k in pred), None)
    capacity_risk = pred.get("clinical_capacity_risk") or pred.get("capacity_risk") or pred.get("risk") or "unknown"
    result = {
        "forecast_available":          1,
        "horizon":                     horizon,
        "predicted_required_doctors":  required,
        "additional_doctors_required": pred.get("additional_doctors_required"),
        "predicted_doctor_patient_ratio": pred.get("predicted_doctor_patient_ratio"),
        "staffing_gap":                pred.get("staffing_gap"),
        "staffing_status":             pred.get("staffing_status") or pred.get("status"),
        "clinical_capacity_risk":      capacity_risk,
        "expected_range":              pred.get("expected_range"),
        "recommended_action":          pred.get("recommended_action") or pred.get("action") or "",
        "doctors_on_duty":             payload["doctors_on_duty"],
        "patient_acuity":              acuity,
        "fallback_used":               bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }

    if str(capacity_risk).lower() in ("medium", "high"):
        severity = "critical" if str(capacity_risk).lower() == "high" else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"Doctor-demand forecast ({horizon}): ~{required} physicians needed "
                        f"(+{result['additional_doctors_required']} gap), clinical_capacity_risk={capacity_risk} — "
                        f"{result['recommended_action']}"),
        })

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_doctor_demand", "result": result})
    logger.info("forecast_doctor_demand  session=%s  horizon=%s  required=%s  risk=%s",
                session_id, horizon, required, capacity_risk)
    return result


# -- shared staffing-forecast context ------------------------------------------

_SHIFT_TYPE = {"day": "Morning", "evening": "Evening", "night": "Night"}


def _unwrap(forecast_resp):
    """(prediction-dict, thresholds) from a StandardResponse envelope."""
    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    th = (forecast_resp.get("thresholds_applied") or {}) if isinstance(forecast_resp, dict) else {}
    return pred, th


async def _staffing_context() -> dict | None:
    """Shared census + roster snapshot for the staffing forecasts (shift-coverage,
    overtime, absenteeism, workforce-utilization). Returns None when there is no bed
    data. average_patient_acuity is the derived ICU+critical share of census rescaled
    to the model's 1-5 range; shift_type is the capitalized enum (Morning/Evening/Night).
    """
    beds = await hasura.get_beds_summary() or {}
    total = int(beds.get("total_beds") or 0)
    if total <= 0:
        return None
    occupied = int(beds.get("occupied_beds") or 0)
    icu = int(beds.get("icu_occupied") or 0)
    er = len(await hasura.get_er_visits() or [])
    critical = int((await hasura.get_critical_escalation_backlog()) or 0)
    discharges = int(await hasura.get_discharge_horizon(24) or 0)
    ot = await hasura.carerOS_get_ot_surgeries() or []
    scheduled_ops = [s for s in ot if (s.get("status") or "").lower() != "completed"]

    roster = await cache.get_all_staff_roster()
    if not roster:
        try:
            roster = await hasura.staff_list_roster(None)
        except Exception:  # noqa: BLE001 -- roster is best-effort
            roster = []
    shift = _current_shift()
    on_rows = [r for r in roster if (r.get("shift") or "").lower() == shift] or roster

    def _count(rows, tokens):
        return sum(int(r.get("headcount") or 0) for r in rows
                   if any(t in (f"{r.get('role') or ''} {r.get('area') or ''}").lower() for t in tokens))

    nurses = _count(on_rows, ("nurse",))
    doctors = _count(on_rows, ("doctor", "physician", "consultant", "surgeon"))
    if doctors == 0:   # roster is nursing-centric -> proxy with registered doctor users
        try:
            doctors = int(await hasura.count_users_by_role("doctor") or 0)
        except Exception:  # noqa: BLE001
            doctors = 0
    support = _count(on_rows, ("porter", "hca", "technician", "housekeep", "aide", "attendant", "support"))
    on_duty = sum(int(r.get("headcount") or 0) for r in on_rows)
    total_staff = sum(int(r.get("headcount") or 0) for r in roster) or on_duty
    acuity = round(_clamp(1 + 4 * (icu + critical) / max(occupied, 1), 1, 5), 1)

    return {
        "total_beds": total, "occupied": occupied, "icu": icu, "er": er,
        "discharges": discharges, "scheduled_surgeries": len(scheduled_ops),
        "nurses": nurses, "doctors": doctors, "support": support,
        "on_duty": on_duty, "total_staff": total_staff, "acuity": acuity,
        "shift_type": _SHIFT_TYPE.get(shift, "Morning"),
    }


# -- sa_shift_coverage ---------------------------------------------------------

@activity.defn
async def forecast_shift_coverage(session_id: str, goal: str = "") -> dict:
    """Forecast the total staff (nursing, medical, support) needed to safely cover the
    shift over a horizon via the ML service (/staffing/shift-coverage).

    Sourced from the shared staffing context (census + current-shift roster). Leave /
    planned-absence / previous-shift-overtime / holiday have no data source and fall to
    documented defaults (0). Degrades to forecast_available: 0 when there is no bed data
    or the service is down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_shift_coverage"})
    ctx = await _staffing_context()
    if ctx is None:
        result = {"forecast_available": 0, "reason": "no bed data"}
        logger.info("forecast_shift_coverage  session=%s  no bed data", session_id)
        return result

    horizon = _horizon_from_goal(goal)
    payload = {
        "forecast_period":              horizon,
        "current_inpatients":           int(_clamp(ctx["occupied"], 0, 5000)),
        "occupied_beds":                int(_clamp(ctx["occupied"], 0, 5000)),
        "nurses_scheduled":             int(_clamp(ctx["nurses"], 0, 10000)),
        "doctors_scheduled":            int(_clamp(ctx["doctors"], 0, 5000)),
        "total_beds":                   int(_clamp(ctx["total_beds"], 0, 5000)),
        "icu_patients":                 int(_clamp(ctx["icu"], 0, 1000)),
        "er_patient_volume":            int(_clamp(ctx["er"], 0, 2000)),
        "scheduled_surgeries":          int(_clamp(ctx["scheduled_surgeries"], 0, 500)),
        "support_staff_scheduled":      int(_clamp(ctx["support"], 0, 10000)),
        "average_patient_acuity":       ctx["acuity"],
        "predicted_admissions":         int(_clamp(ctx["discharges"], 0, 2000)),   # balanced-flow proxy
        "predicted_discharges":         int(_clamp(ctx["discharges"], 0, 2000)),
        "shift_type":                   ctx["shift_type"],
        "staff_on_leave":               0,   # no leave-tracking source
        "planned_absences":             0,   # no planned-absence source
        "overtime_hours_previous_shift": 0,  # no shift-overtime history
        "holiday_flag":                 0,   # no holiday calendar
    }

    forecast_resp = await forecast("/staffing/shift-coverage", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_shift_coverage", "result": result})
        logger.info("forecast_shift_coverage  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    pred, _ = _unwrap(forecast_resp)
    required = next((pred[k] for k in ("predicted_staff_required", "required_staff", "value") if k in pred), None)
    on_now = ctx["nurses"] + ctx["doctors"] + ctx["support"]
    gap = int(round(required)) - on_now if isinstance(required, (int, float)) else None
    result = {
        "forecast_available":     1,
        "horizon":                horizon,
        "predicted_staff_required": required,
        "current_staff_on_duty":  on_now,
        "additional_staff_required": max(gap, 0) if isinstance(gap, int) else None,
        "staffing_gap":           gap,
        "recommended_action":     pred.get("recommended_action") or pred.get("action") or "",
        "fallback_used":          bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }
    if isinstance(gap, int) and gap > 0:
        severity = "critical" if gap >= 10 else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"Shift-coverage forecast ({horizon}): ~{required} staff needed vs {on_now} on duty "
                        f"(+{gap} short) — {result['recommended_action']}"),
        })
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_shift_coverage", "result": result})
    logger.info("forecast_shift_coverage  session=%s  horizon=%s  required=%s  gap=%s",
                session_id, horizon, required, gap)
    return result


# -- sa_overtime_forecast ------------------------------------------------------

@activity.defn
async def forecast_overtime(session_id: str, goal: str = "") -> dict:
    """Forecast staff overtime hours over a horizon via the ML service
    (/staffing/overtime-forecast). Sourced from the shared staffing context;
    previous_shift_overtime / leave / absences / holiday have no source (defaults 0).
    Degrades to forecast_available: 0 when there is no bed data or the service is down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_overtime_forecast"})
    ctx = await _staffing_context()
    if ctx is None:
        result = {"forecast_available": 0, "reason": "no bed data"}
        logger.info("forecast_overtime  session=%s  no bed data", session_id)
        return result

    horizon = _horizon_from_goal(goal)
    payload = {
        "forecast_period":         horizon,
        "current_inpatients":      int(_clamp(ctx["occupied"], 0, 5000)),
        "occupied_beds":           int(_clamp(ctx["occupied"], 0, 5000)),
        "nurses_scheduled":        int(_clamp(ctx["nurses"], 0, 5000)),
        "doctors_scheduled":       int(_clamp(ctx["doctors"], 0, 2000)),
        "previous_shift_overtime": 0,   # no shift-overtime history
        "total_beds":              int(_clamp(ctx["total_beds"], 0, 5000)),
        "icu_patients":            int(_clamp(ctx["icu"], 0, 1000)),
        "er_patient_volume":       int(_clamp(ctx["er"], 0, 2000)),
        "scheduled_surgeries":     int(_clamp(ctx["scheduled_surgeries"], 0, 500)),
        "support_staff_scheduled": int(_clamp(ctx["support"], 0, 5000)),
        "staff_on_leave":          0,
        "planned_absences":        0,
        "average_patient_acuity":  ctx["acuity"],
        "predicted_admissions":    int(_clamp(ctx["discharges"], 0, 2000)),
        "predicted_discharges":    int(_clamp(ctx["discharges"], 0, 2000)),
        "shift_type":              ctx["shift_type"],
        "holiday_flag":            0,
    }

    forecast_resp = await forecast("/staffing/overtime-forecast", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_overtime_forecast", "result": result})
        logger.info("forecast_overtime  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    pred, th = _unwrap(forecast_resp)
    hours = next((pred[k] for k in ("predicted_overtime_hours", "overtime_hours", "value") if k in pred), None)
    result = {
        "forecast_available":       1,
        "horizon":                  horizon,
        "predicted_overtime_hours": hours,
        "current_staff_on_duty":    ctx["on_duty"],
        "recommended_action":       pred.get("recommended_action") or pred.get("action") or "",
        "fallback_used":            bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }
    high = th.get("overtime_high")
    if isinstance(hours, (int, float)) and isinstance(high, (int, float)) and hours >= high:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": (f"Overtime forecast ({horizon}): ~{hours} OT hours expected — "
                        f"{result['recommended_action']}"),
        })
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_overtime_forecast", "result": result})
    logger.info("forecast_overtime  session=%s  horizon=%s  hours=%s", session_id, horizon, hours)
    return result


# -- sa_absenteeism_forecast ---------------------------------------------------

@activity.defn
async def forecast_absenteeism(session_id: str, goal: str = "") -> dict:
    """Forecast the number of staff on unplanned absence over a horizon via the ML
    service (/staffing/absenteeism-forecast). Sourced from the shared staffing context;
    staff_on_leave / recent_absenteeism_rate / previous_shift_overtime have no source
    (defaults). Degrades to forecast_available: 0 when there is no bed data or the
    service is down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_absenteeism_forecast"})
    ctx = await _staffing_context()
    if ctx is None:
        result = {"forecast_available": 0, "reason": "no bed data"}
        logger.info("forecast_absenteeism  session=%s  no bed data", session_id)
        return result

    horizon = _horizon_from_goal(goal)
    payload = {
        "forecast_period":         horizon,
        "total_staff_scheduled":   int(_clamp(ctx["total_staff"], 1, 20000)),
        "nurses_scheduled":        int(_clamp(ctx["nurses"], 0, 10000)),
        "doctors_scheduled":       int(_clamp(ctx["doctors"], 0, 5000)),
        "staff_on_leave":          0,   # no leave-tracking source
        "previous_shift_overtime": 0,
        "occupied_beds":           int(_clamp(ctx["occupied"], 0, 5000)),
        "icu_patients":            int(_clamp(ctx["icu"], 0, 1000)),
        "er_patient_volume":       int(_clamp(ctx["er"], 0, 2000)),
        "average_patient_acuity":  ctx["acuity"],
        "support_staff_scheduled": int(_clamp(ctx["support"], 0, 10000)),
        "planned_absences":        0,
        "current_inpatients":      int(_clamp(ctx["occupied"], 0, 5000)),
        "total_beds":              int(_clamp(ctx["total_beds"], 0, 5000)),
        "scheduled_surgeries":     int(_clamp(ctx["scheduled_surgeries"], 0, 500)),
        "predicted_admissions":    int(_clamp(ctx["discharges"], 0, 2000)),
        "predicted_discharges":    int(_clamp(ctx["discharges"], 0, 2000)),
        "shift_type":              ctx["shift_type"],
        "holiday_flag":            0,
    }

    forecast_resp = await forecast("/staffing/absenteeism-forecast", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_absenteeism_forecast", "result": result})
        logger.info("forecast_absenteeism  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    pred, _ = _unwrap(forecast_resp)
    absent = next((pred[k] for k in ("predicted_absent_staff", "absent_staff", "value") if k in pred), None)
    absent_pct = round(absent / ctx["total_staff"] * 100, 1) if (isinstance(absent, (int, float)) and ctx["total_staff"]) else None
    result = {
        "forecast_available":     1,
        "horizon":                horizon,
        "predicted_absent_staff": absent,
        "absent_share_pct":       absent_pct,
        "total_staff_scheduled":  payload["total_staff_scheduled"],
        "recommended_action":     pred.get("recommended_action") or pred.get("action") or "",
        "fallback_used":          bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }
    if isinstance(absent_pct, (int, float)) and absent_pct >= 10:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": (f"Absenteeism forecast ({horizon}): ~{absent} staff absent (~{absent_pct}%) — "
                        f"{result['recommended_action']}"),
        })
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_absenteeism_forecast", "result": result})
    logger.info("forecast_absenteeism  session=%s  horizon=%s  absent=%s", session_id, horizon, absent)
    return result


# -- sa_workforce_utilization --------------------------------------------------

@activity.defn
async def forecast_workforce_utilization(session_id: str, goal: str = "") -> dict:
    """Forecast the worst-hour percent of deployable workforce capacity consumed over a
    horizon (plus hours in the critical band) via the ML service
    (/staffing/workforce-utilization). Sourced from the shared staffing context;
    staff_on_leave / staff_in_training / department_count have no source (defaults 0).
    Degrades to forecast_available: 0 when there is no bed data or the service is down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_workforce_utilization"})
    ctx = await _staffing_context()
    if ctx is None:
        result = {"forecast_available": 0, "reason": "no bed data"}
        logger.info("forecast_workforce_utilization  session=%s  no bed data", session_id)
        return result

    horizon = _horizon_from_goal(goal)
    payload = {
        "forecast_period":        horizon,
        "total_staff_available":  int(_clamp(ctx["total_staff"], 1, 20000)),
        "staff_currently_on_duty": int(_clamp(ctx["on_duty"], 0, 20000)),
        "occupied_beds":          int(_clamp(ctx["occupied"], 0, 5000)),
        "staff_on_leave":         0,
        "staff_in_training":      0,
        "department_count":       0,   # model derives from bed base
        "icu_patients":           int(_clamp(ctx["icu"], 0, 1000)),
        "er_patient_volume":      int(_clamp(ctx["er"], 0, 2000)),
        "average_patient_acuity": ctx["acuity"],
        "total_beds":             int(_clamp(ctx["total_beds"], 0, 5000)),
        "scheduled_surgeries":    int(_clamp(ctx["scheduled_surgeries"], 0, 500)),
        "current_inpatients":     int(_clamp(ctx["occupied"], 0, 5000)),
        "nurses_on_duty":         int(_clamp(ctx["nurses"], 0, 10000)),
        "doctors_on_duty":        int(_clamp(ctx["doctors"], 0, 5000)),
        "support_staff_on_duty":  int(_clamp(ctx["support"], 0, 10000)),
        "predicted_admissions":   int(_clamp(ctx["discharges"], 0, 2000)),
        "predicted_discharges":   int(_clamp(ctx["discharges"], 0, 2000)),
        "shift_type":             ctx["shift_type"],
        "holiday_flag":           0,
    }

    forecast_resp = await forecast("/staffing/workforce-utilization", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_workforce_utilization", "result": result})
        logger.info("forecast_workforce_utilization  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    pred, _ = _unwrap(forecast_resp)
    peak = next((pred[k] for k in ("predicted_peak_workforce_utilization", "predicted_peak_utilization", "value") if k in pred), None)
    peak_pct = round(peak * 100, 1) if isinstance(peak, (int, float)) and peak <= 1 else peak
    critical_hours = pred.get("predicted_critical_hours")
    result = {
        "forecast_available":                   1,
        "horizon":                              horizon,
        "predicted_peak_workforce_utilization": peak_pct,
        "predicted_critical_hours":             critical_hours,
        "staff_currently_on_duty":              payload["staff_currently_on_duty"],
        "recommended_action":                   pred.get("recommended_action") or pred.get("action") or "",
        "fallback_used":                        bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }
    if (isinstance(peak_pct, (int, float)) and peak_pct >= 85) or (isinstance(critical_hours, (int, float)) and critical_hours > 0):
        severity = "critical" if (isinstance(peak_pct, (int, float)) and peak_pct >= 95) else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"Workforce-utilization forecast ({horizon}): peak ~{peak_pct}% consumed, "
                        f"{critical_hours} critical hour(s) — {result['recommended_action']}"),
        })
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_workforce_utilization", "result": result})
    logger.info("forecast_workforce_utilization  session=%s  horizon=%s  peak=%s  crit=%s",
                session_id, horizon, peak_pct, critical_hours)
    return result


# -- sa_skill_mix --------------------------------------------------------------

def _adm_ventilated(a) -> bool:
    """True if an ICU admission's bed carries an active ventilation status."""
    b = a.get("bed")
    if isinstance(b, list):
        b = b[0] if b else {}
    return isinstance(b, dict) and str((b or {}).get("ventilation") or "").lower() in ("full_ventilator", "bipap")


@activity.defn
async def forecast_skill_mix(session_id: str, goal: str = "") -> dict:
    """Forecast WHICH clinical skills will be needed (concurrent headcount per specialty:
    ICU nurses, ER nurses, anesthesiologists, respiratory therapists, critical-care
    physicians, OR staff) over a horizon via the ML service (/staffing/skill-mix).

    Sourced from the shared staffing context plus ventilated-patient count (occupied ICU
    beds with an active ventilation status), ICU beds and operating rooms. The 6
    per-specialty CURRENT-available counts default to 0 (the roster lacks specialty
    granularity) -- the model predicts REQUIRED skills from the census/ICU/ventilator/
    surgery load, which we do source. ed_bays / respiratory_patients / leave / holiday
    fall to model defaults. Degrades to forecast_available: 0 when there is no bed data
    or the service is down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_skill_mix"})
    sctx = await _staffing_context()
    if sctx is None:
        result = {"forecast_available": 0, "reason": "no bed data"}
        logger.info("forecast_skill_mix  session=%s  no bed data", session_id)
        return result

    beds = await hasura.get_beds_summary() or {}
    icu_beds = int(beds.get("icu_total") or 0)
    icu_adm = await hasura.get_icu_admissions() or []
    ventilated = sum(1 for a in icu_adm if _adm_ventilated(a))
    ot_rooms = len(await cache.get_all_ot_rooms() or [])

    horizon = _horizon_from_goal(goal)
    payload = {
        "forecast_period":        horizon,
        "occupied_beds":          int(_clamp(sctx["occupied"], 0, 5000)),
        "icu_patients":           int(_clamp(sctx["icu"], 0, 1000)),
        "er_patient_volume":      int(_clamp(sctx["er"], 0, 2000)),
        "scheduled_surgeries":    int(_clamp(sctx["scheduled_surgeries"], 0, 500)),
        "average_patient_acuity": sctx["acuity"],
        "ventilated_patients":    int(_clamp(ventilated, 0, 1000)),
        "total_beds":             int(_clamp(sctx["total_beds"], 0, 5000)),
        "icu_beds":               int(_clamp(icu_beds, 0, 1000)),
        "operating_rooms":        int(_clamp(ot_rooms, 0, 200)),
        "current_inpatients":     int(_clamp(sctx["occupied"], 0, 5000)),
        "predicted_admissions":   int(_clamp(sctx["discharges"], 0, 2000)),
        "predicted_discharges":   int(_clamp(sctx["discharges"], 0, 2000)),
        "shift_type":             sctx["shift_type"],
        # Roster lacks specialty granularity -> current-available per specialty default 0.
        "icu_nurses_available":             0,
        "er_nurses_available":              0,
        "anesthesiologists_available":      0,
        "respiratory_therapists_available": 0,
        "critical_care_physicians_available": 0,
        "operating_room_staff_available":   0,
        "staff_on_leave":         0,
        "holiday_flag":           0,
    }

    forecast_resp = await forecast("/staffing/skill-mix", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_skill_mix", "result": result})
        logger.info("forecast_skill_mix  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    pred, _ = _unwrap(forecast_resp)
    skills = pred.get("predicted_skill_requirements") or pred.get("skill_requirements") or {}
    result = {
        "forecast_available":          1,
        "horizon":                     horizon,
        "predicted_skill_requirements": skills,
        "ventilated_patients":         payload["ventilated_patients"],
        "recommended_action":          pred.get("recommended_action") or pred.get("action") or "",
        "fallback_used":               bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_skill_mix", "result": result})
    logger.info("forecast_skill_mix  session=%s  horizon=%s  skills=%s", session_id, horizon, skills)
    return result
