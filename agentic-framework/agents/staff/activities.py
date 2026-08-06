import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from temporalio import activity

from cache import redis as cache
from db.hasura import hasura
from util.idem import make_idem_key
from workflows.temporal.workflow._escalation import start_escalating_approval
from fhirgw import repository as repo
from fhirgw.mappers._common import ref_id
from agents.staff.service import analyze_staffing
from api.routes.ws import broadcast

logger = logging.getLogger(__name__)


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
