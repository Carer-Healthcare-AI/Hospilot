import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from temporalio import activity

from cache import redis as cache
from db.hasura import hasura
from agents.ot.service import (
    predict_ot_delays     as _svc_predict_delays,
    coordinate_ot_staff   as _svc_coordinate_staff,
    handle_ot_emergencies as _svc_handle_emergencies,
    optimise_ot_slots     as _svc_optimise_slots,
    balance_ot_load       as _svc_balance_load,
    analyze_ot_capacity   as _svc_analyze_capacity,
    is_non_elective,
)
from api.routes.ws import broadcast

logger = logging.getLogger(__name__)


# -- Input dataclasses ---------------------------------------------------------

@dataclass
class OtRoomInput:
    session_id:  str
    room_status: list = field(default_factory=list)

@dataclass
class OtInstrumentInput:
    session_id:           str
    upcoming_surgeries:   list = field(default_factory=list)
    equipment_by_surgery: dict = field(default_factory=dict)

@dataclass
class OtDelayInput:
    session_id:          str
    room_status:         list = field(default_factory=list)
    upcoming_surgeries:  list = field(default_factory=list)
    rooms_active:        list = field(default_factory=list)

@dataclass
class OtStaffInput:
    session_id:      str
    delay_risks:     list = field(default_factory=list)
    rooms_to_clean:  list = field(default_factory=list)
    instrument_gaps: list = field(default_factory=list)

@dataclass
class OtEfficiencyInput:
    session_id:        str
    maintenance_rooms: int = 0
    high_risk_delays:  int = 0
    instrument_gaps:   int = 0
    conflict_count:    int = 0

@dataclass
class OtScheduleInput:
    session_id: str
    schedule:   list = field(default_factory=list)
    rooms:      list = field(default_factory=list)

@dataclass
class OtEmergencyInput:
    session_id:      str
    emergency_cases: list = field(default_factory=list)
    rooms:           list = field(default_factory=list)

@dataclass
class OtSlotInput:
    session_id: str
    schedule:   list = field(default_factory=list)
    rooms:      list = field(default_factory=list)
    conflicts:  dict = field(default_factory=dict)

@dataclass
class OtLoadInput:
    session_id:  str
    schedule:    list = field(default_factory=list)
    rooms:       list = field(default_factory=list)
    utilisation: dict = field(default_factory=dict)

@dataclass
class OtCapacityInput:
    session_id:  str
    schedule:    list = field(default_factory=list)
    rooms:       list = field(default_factory=list)
    conflicts:   dict = field(default_factory=dict)
    emergencies: list = field(default_factory=list)
    resources:   dict = field(default_factory=dict)

@dataclass
class OtSlotSearchInput:
    session_id:       str
    rooms:            list = field(default_factory=list)
    booked_schedule:  list = field(default_factory=list)   # ALL surgeries, not today-only
    room_type:        str  = ""                            # required theatre type (== surgery_type)
    duration_minutes: int  = 60
    horizon_days:     int  = 7

@dataclass
class OtRescheduleInput:
    session_id:      str
    booked_schedule: list = field(default_factory=list)    # ALL surgeries
    rooms:           list = field(default_factory=list)
    goal:            str  = ""

@dataclass
class OtDeferInput:
    session_id:           str
    booked_schedule:      list = field(default_factory=list)   # ALL surgeries
    rooms:                list = field(default_factory=list)
    case_recommendations: list = field(default_factory=list)   # from analyze_ot_capacity


# Post-operative recovery in this model is high-acuity monitoring (ICU / HDU /
# high-dependency). There is no separate PACU ward, so a "post-op bed" is an
# available ICU or HDU bed. Tokens stay broad in case a recovery ward is added.
_POST_OP_WARD_TOKENS = ("ICU", "HDU", "HIGH DEPEND", "RECOVERY", "PACU", "POST-OP", "POSTOP", "POST OP")


# -- sa_ot_census --------------------------------------------------------------

@activity.defn
async def get_ot_census(session_id: str) -> dict:
    """Fetch today's OT schedule, theatre state, equipment, and post-op bed capacity.

    The single OT data-fetch task: surfaces rooms / room status / equipment for the
    turnaround + scheduling sub-agents AND the available post-operative beds a case
    will need on emergence (available ICU / HDU beds). Read-only.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_ot_census"})

    schedule    = await cache.get_all_ot_schedule()
    rooms       = await cache.get_all_ot_rooms()
    room_status = await cache.get_all_ot_room_status()
    surgeries   = await cache.get_all_ot_surgeries()
    beds        = await cache.get_all_beds()

    today = date.today().isoformat()
    upcoming = [
        s for s in schedule
        if s.get("scheduled_date") == today
        and (s.get("status") or "").lower() not in ("completed", "cancelled")
    ]

    equipment_by_surgery: dict = {}
    for s in upcoming:
        sid = s.get("id")
        if sid:
            equipment_by_surgery[sid] = await cache.get_ot_equipment_usage(sid)

    def _ward(b: dict) -> str:
        return (b.get("ward") or "").upper()

    post_op_beds = [
        {"bed_number": b.get("bed_number"), "ward": b.get("ward"), "room_type": b.get("room_type")}
        for b in beds
        if b.get("status") == "Available"
        and any(tok in _ward(b) for tok in _POST_OP_WARD_TOKENS)
    ]
    icu_available = sum(1 for b in post_op_beds if "ICU" in _ward(b))
    hdu_available = sum(1 for b in post_op_beds if "HDU" in _ward(b) or "HIGH DEPEND" in _ward(b))

    await broadcast(session_id, {
        "type": "sub_agent_completed", "sub_agent": "sa_ot_census",
        "result": {"total_scheduled": len(schedule), "upcoming_count": len(upcoming),
                   "rooms": len(rooms), "post_op_beds_available": len(post_op_beds)},
    })
    logger.info("OT census  session=%s  schedule=%d  upcoming=%d  rooms=%d  post_op_beds=%d",
                session_id, len(schedule), len(upcoming), len(rooms), len(post_op_beds))
    return {
        "schedule": schedule, "rooms": rooms, "room_status": room_status,
        "surgeries": surgeries, "upcoming_surgeries": upcoming,
        "equipment_by_surgery": equipment_by_surgery,
        "upcoming_count": len(upcoming),
        "case_count": len(upcoming),
        "post_op_beds": post_op_beds,
        "post_op_beds_available": len(post_op_beds),
        "icu_available": icu_available,
        "hdu_available": hdu_available,
    }


# -- sa_ot_turnaround -- pure logic ---------------------------------------------

@activity.defn
async def check_ot_room_cleaning(inp: OtRoomInput) -> dict:
    rooms_to_clean = [
        {"room_code": r.get("room_code"), "room_name": r.get("room_name"), "status": r.get("status")}
        for r in inp.room_status
        if (r.get("status") or "").lower() in ("occupied", "cleaning", "dirty")
    ]
    logger.info("OT room cleaning  session=%s  count=%d", inp.session_id, len(rooms_to_clean))
    return {"cleaning_count": len(rooms_to_clean), "rooms_to_clean": rooms_to_clean}


@activity.defn
async def check_ot_instrument_readiness(inp: OtInstrumentInput) -> dict:
    gaps, ready_count = [], 0
    for s in inp.upcoming_surgeries:
        sid   = s.get("id")
        equip = inp.equipment_by_surgery.get(sid, [])
        if equip:
            ready_count += 1
        else:
            gaps.append({
                "surgery_code": s.get("surgery_code"),
                "surgery_name": s.get("surgery_name"),
                "room_code":    s.get("room_code"),
                "start_time":   s.get("scheduled_start_time"),
            })
    logger.info("OT instruments  session=%s  ready=%d  gaps=%d", inp.session_id, ready_count, len(gaps))
    return {"gap_count": len(gaps), "ready_count": ready_count, "gaps": gaps}


@activity.defn
async def track_ot_turnaround(inp: OtRoomInput) -> dict:
    rooms_active = [
        {"room_code": r.get("room_code"), "surgery_status": r.get("surgery_status"),
         "current_case": r.get("current_surgery_name"), "scheduled_end": r.get("scheduled_end_time")}
        for r in inp.room_status
        if (r.get("surgery_status") or "").lower() in ("in progress", "in-progress", "active")
    ]
    total    = len(inp.room_status)
    active   = len(rooms_active)
    util_pct = round(active / max(total, 1) * 100)
    logger.info("OT turnaround tracking  session=%s  active=%d/%d", inp.session_id, active, total)
    return {"rooms_active": rooms_active, "active_count": active, "utilisation_pct": util_pct}


# -- sa_ot_turnaround -- Claude-backed -----------------------------------------

@activity.defn
async def predict_ot_delays(inp: OtDelayInput) -> dict:
    result = await _svc_predict_delays(inp.room_status, inp.upcoming_surgeries, inp.rooms_active)
    if result.get("high_risk_count", 0) > 0:
        for risk in result.get("delay_risks", []):
            if risk.get("risk") == "high":
                await broadcast(inp.session_id, {
                    "type": "alert", "severity": "warning",
                    "message": f"OT Delay Risk: {risk.get('room_code')} -- {risk.get('reason')}",
                })
    return result


@activity.defn
async def coordinate_ot_staff(inp: OtStaffInput) -> dict:
    result = await _svc_coordinate_staff(inp.delay_risks, inp.rooms_to_clean, inp.instrument_gaps)
    return result


# -- sa_ot_scheduling -- pure logic ---------------------------------------------

@activity.defn
async def detect_ot_conflicts(inp: OtScheduleInput) -> dict:
    def _overlap(s1, e1, s2, e2) -> bool:
        if not all([s1, e1, s2, e2]):
            return False
        if e1 <= s1 or e2 <= s2:
            return False
        return s1 < e2 and s2 < e1

    by_room: dict = defaultdict(list)
    by_surgeon: dict = defaultdict(list)
    for s in inp.schedule:
        if s.get("room_code") and s.get("scheduled_date"):
            by_room[(s["room_code"], s["scheduled_date"])].append(s)
        if s.get("surgeon_id") and s.get("scheduled_date"):
            by_surgeon[(s["surgeon_id"], s["scheduled_date"])].append(s)

    room_conflicts, surgeon_conflicts = [], []
    for cases in by_room.values():
        for i, a in enumerate(cases):
            for b in cases[i + 1:]:
                if _overlap(a.get("scheduled_start_time"), a.get("scheduled_end_time"),
                            b.get("scheduled_start_time"), b.get("scheduled_end_time")):
                    room_conflicts.append({
                        "room_code":  a.get("room_code"),
                        "surgery_a":  a.get("surgery_code"),
                        "surgery_b":  b.get("surgery_code"),
                        "date":       a.get("scheduled_date"),
                    })

    for cases in by_surgeon.values():
        for i, a in enumerate(cases):
            for b in cases[i + 1:]:
                if _overlap(a.get("scheduled_start_time"), a.get("scheduled_end_time"),
                            b.get("scheduled_start_time"), b.get("scheduled_end_time")):
                    surgeon_conflicts.append({
                        "surgeon_name": a.get("surgeon_name"),
                        "surgery_a":    a.get("surgery_code"),
                        "surgery_b":    b.get("surgery_code"),
                        "date":         a.get("scheduled_date"),
                    })

    conflict_count = len(room_conflicts) + len(surgeon_conflicts)
    logger.info("OT conflicts  session=%s  room=%d  surgeon=%d",
                inp.session_id, len(room_conflicts), len(surgeon_conflicts))
    return {
        "conflict_count":   conflict_count,
        "has_conflicts":    conflict_count > 0,
        "room_conflicts":   room_conflicts,
        "surgeon_conflicts": surgeon_conflicts,
    }


@activity.defn
async def find_ot_emergencies(inp: OtScheduleInput) -> dict:
    # Shares is_non_elective with analyze_ot_capacity so detection and
    # disposition agree on acuity: reads priority AND surgery_type and
    # matches the current Non-Elective model (plus legacy emergency/urgent).
    cases = [s for s in inp.schedule if is_non_elective(s)]
    logger.info("OT emergencies  session=%s  count=%d", inp.session_id, len(cases))
    return {"emergency_count": len(cases), "emergency_cases": cases}


@activity.defn
async def check_ot_resource_availability(inp: OtScheduleInput) -> dict:
    room_codes  = {r.get("room_code") for r in inp.rooms if (r.get("status") or "") == "Available"}
    cases_by_room: dict = defaultdict(int)
    for s in inp.schedule:
        rc = s.get("room_code")
        if rc:
            cases_by_room[rc] += 1

    available_rooms = len(room_codes)
    total_rooms     = len(inp.rooms)
    total_cases     = len(inp.schedule)
    util_pct        = round(total_cases / max(available_rooms, 1) * 100)

    under_resourced = [
        {"room_code": rc, "case_count": cnt}
        for rc, cnt in cases_by_room.items()
        if cnt > 3
    ]
    logger.info("OT resources  session=%s  available_rooms=%d  cases=%d",
                inp.session_id, available_rooms, total_cases)
    return {
        "available_rooms":  available_rooms,
        "total_rooms":      total_rooms,
        "utilisation_pct":  util_pct,
        "under_resourced":  under_resourced,
        "cases_by_room":    dict(cases_by_room),
    }


# -- sa_ot_scheduling -- Claude-backed -----------------------------------------

@activity.defn
async def handle_ot_emergencies(inp: OtEmergencyInput) -> dict:
    result = await _svc_handle_emergencies(inp.emergency_cases, inp.rooms)
    for action in result.get("emergency_actions", []):
        await broadcast(inp.session_id, {
            "type": "alert",
            "severity": "critical" if action.get("urgency") == "immediate" else "warning",
            "message": f"OT Emergency: {action.get('surgery_code')} -- {action.get('action')}",
        })
    await hasura.write_audit(
        session_id=inp.session_id, agent_id="ot_agent",
        event_type="ot_emergency_handled", payload=result,
    )
    return result


@activity.defn
async def optimise_ot_slots(inp: OtSlotInput) -> dict:
    result = await _svc_optimise_slots(inp.schedule, inp.rooms, inp.conflicts)
    return result


@activity.defn
async def balance_ot_load(inp: OtLoadInput) -> dict:
    result = await _svc_balance_load(inp.schedule, inp.rooms, inp.utilisation)
    await hasura.write_audit(
        session_id=inp.session_id, agent_id="ot_agent",
        event_type="ot_scheduling_analysed", payload=result,
    )
    return result


# -- sa_ot_analysis -- deterministic terminal synthesis ------------------------
# Runs AFTER turnaround + scheduling so it can read their real outputs
# (delay risk, instrument gaps, conflict count) rather than placeholder zeros.

@activity.defn
async def score_ot_efficiency(inp: OtEfficiencyInput) -> dict:
    score = 100
    score -= inp.maintenance_rooms * 15
    score -= min(inp.high_risk_delays * 10, 30)
    score -= min(inp.instrument_gaps * 5, 20)
    score -= min(inp.conflict_count * 8, 25)
    efficiency_score = max(0, score)
    logger.info("OT efficiency  session=%s  score=%d", inp.session_id, efficiency_score)
    return {"efficiency_score": efficiency_score}


@activity.defn
async def analyze_ot_capacity(inp: OtCapacityInput) -> dict:
    """Identify conflicts and recommend proceed/delay/escalate per scheduled case."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_ot_analysis"})

    result = await _svc_analyze_capacity(inp.schedule, inp.rooms, inp.conflicts, inp.emergencies, inp.resources)

    counts = {"proceed": 0, "delay": 0, "escalate": 0}
    for rec in result.get("case_recommendations", []):
        r = (rec.get("recommendation") or "").lower()
        if r in counts:
            counts[r] += 1
        if r == "escalate":
            await broadcast(inp.session_id, {
                "type": "alert", "severity": "warning",
                "message": f"OT capacity: {rec.get('surgery_code')} -- ESCALATE ({rec.get('reason')})",
            })

    await hasura.write_audit(
        session_id=inp.session_id, agent_id="ot_agent",
        event_type="ot_capacity_analysed", payload={**result, "counts": counts},
    )

    out = {
        **result,
        "recommendation_count": len(result.get("case_recommendations", [])),
        "proceed_count":        counts["proceed"],
        "delay_count":          counts["delay"],
        "escalate_count":       counts["escalate"],
    }
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed", "sub_agent": "sa_ot_analysis",
        "result": {"recommendation_count": out["recommendation_count"],
                   "escalate_count": counts["escalate"]},
    })
    logger.info("OT capacity analysis  session=%s  recs=%d  escalate=%d",
                inp.session_id, out["recommendation_count"], counts["escalate"])
    return out


# -- sa_ot_scheduling -- open theatre-slot search + executable reschedule (G32) -
# There is no bookable-OT-slot feed, so open theatre time is DERIVED from ot_rooms
# + the booked ot_surgery_schedule within a theatre operating window. A reschedule
# is a single UPDATE to the surgery record (new date/time/room) staged for /commit
# -- the freed original slot needs no separate write (moving the record vacates it).

_OT_DAY_START      = time(8, 0)    # theatre operating window (no per-room hours in the data)
_OT_DAY_END        = time(20, 0)
_OT_TURNAROUND_MIN = 30            # buffer between cases


def _parse_t(v) -> time | None:
    if not v:
        return None
    try:
        return time.fromisoformat(str(v))
    except Exception:
        try:
            return datetime.strptime(str(v)[:8], "%H:%M:%S").time()
        except Exception:
            return None


def _minutes_between(a: time, b: time) -> int:
    d0 = date.today()
    return int((datetime.combine(d0, b) - datetime.combine(d0, a)).total_seconds() // 60)


def _free_windows_for_room(bookings: list, day: date, need_min: int) -> list[dict]:
    """Gaps in [08:00, 20:00] not covered by bookings (+turnaround buffer), >= need_min."""
    intervals: list[tuple] = []
    for b in bookings:
        st = _parse_t(b.get("scheduled_start_time"))
        if not st:
            continue
        en = _parse_t(b.get("scheduled_end_time"))
        if not en or en <= st:
            dur = b.get("estimated_duration_minutes") or 60
            en = (datetime.combine(day, st) + timedelta(minutes=dur)).time()
        intervals.append((st, en))
    intervals.sort()
    windows: list[dict] = []
    cursor = _OT_DAY_START
    for st, en in intervals:
        gap_end = (datetime.combine(day, st) - timedelta(minutes=_OT_TURNAROUND_MIN)).time()
        if gap_end > cursor and _minutes_between(cursor, gap_end) >= need_min:
            windows.append({"start": cursor.isoformat(timespec="minutes"),
                            "end": gap_end.isoformat(timespec="minutes")})
        if en > cursor:
            cursor = (datetime.combine(day, en) + timedelta(minutes=_OT_TURNAROUND_MIN)).time()
    if _minutes_between(cursor, _OT_DAY_END) >= need_min:
        windows.append({"start": cursor.isoformat(timespec="minutes"),
                        "end": _OT_DAY_END.isoformat(timespec="minutes")})
    return windows


def _slot_dt(d, t) -> datetime:
    return datetime.combine(date.fromisoformat(str(d)), _parse_t(t) or time(0, 0))


async def _current_ot_moves(session_id: str) -> tuple[list, set, set]:
    """Existing staged OT moves so multiple move-producing tasks (reschedule / defer)
    in one session don't double-book a slot or move the same surgery twice."""
    existing = await cache.get_staged(session_id, "ot_reschedules") or []
    taken = {(p["to"]["room_code"], p["to"]["date"], p["to"]["start"])
             for p in existing if p.get("to")}
    moved = {p.get("surgery_id") for p in existing}
    return existing, taken, moved


async def _stage_ot_moves(session_id: str, new_proposals: list, existing: list) -> None:
    """Merge new moves with any already staged (new wins per surgery) and re-stage
    the full list -- cache.stage overwrites per key, so we must persist the union."""
    by_id = {p.get("surgery_id"): p for p in existing}
    for p in new_proposals:
        by_id[p.get("surgery_id")] = p
    await cache.stage(session_id, "ot_reschedules", list(by_id.values()))


def _move_proposal(s: dict, slot: dict, dur: int) -> dict:
    end_t = (_slot_dt(slot["date"], slot["start"]) + timedelta(minutes=dur)).time().isoformat(timespec="minutes")
    return {
        "surgery_id":   s.get("id"),
        "surgery_code": s.get("surgery_code"),
        "surgery_name": s.get("surgery_name"),
        "from": {"date": str(s.get("scheduled_date")), "start": s.get("scheduled_start_time"),
                 "room_code": s.get("room_code")},
        "to":   {"date": slot["date"], "start": slot["start"], "end": end_t,
                 "room_code": slot["room_code"], "ot_room_id": slot["room_id"],
                 "room_type": slot["room_type"]},
    }


def _derive_open_slots(rooms: list, booked: list, room_type: str,
                       duration_minutes: int, horizon_days: int = 7) -> list[dict]:
    """Derive candidate open theatre slots over the next horizon_days for active rooms
    of the required type. Pure logic (no I/O) so both the search task and the reschedule
    task can reuse it."""
    want = (room_type or "").strip().lower()
    usable = [r for r in rooms
              if r.get("is_active", True)
              and (r.get("status") or "").lower() != "maintenance"
              and (not want or (r.get("room_type") or "").lower() == want)]
    today = date.today()
    days = [today + timedelta(days=d) for d in range(0, max(1, horizon_days) + 1)]
    slots: list[dict] = []
    for r in usable:
        rcode = r.get("room_code")
        for day in days:
            iso = day.isoformat()
            day_booked = [b for b in booked
                          if b.get("room_code") == rcode
                          and str(b.get("scheduled_date")) == iso
                          and (b.get("status") or "").lower() not in ("cancelled", "completed")]
            for w in _free_windows_for_room(day_booked, day, duration_minutes):
                slots.append({"room_code": rcode, "room_id": r.get("id"),
                              "room_type": r.get("room_type"),
                              "date": iso, "start": w["start"], "end": w["end"]})
    slots.sort(key=lambda s: (s["date"], s["start"]))
    return slots


@activity.defn
async def find_ot_theatre_slots(inp: OtSlotSearchInput) -> dict:
    """G32: derive open theatre time-windows for surgical (re)scheduling. Read-only."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_ot_scheduling"})
    slots = _derive_open_slots(inp.rooms, inp.booked_schedule, inp.room_type,
                               inp.duration_minutes, inp.horizon_days)
    result = {"open_slots": slots, "open_slot_count": len(slots),
              "room_type": inp.room_type, "duration_minutes": inp.duration_minutes}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_ot_scheduling",
                                     "result": {"open_slot_count": len(slots)}})
    logger.info("OT theatre slots  session=%s  type=%s  open=%d",
                inp.session_id, inp.room_type or "any", len(slots))
    return result


@activity.defn
async def reschedule_ot_surgery(inp: OtRescheduleInput) -> dict:
    """G32/G33: stage an EXECUTABLE reschedule for cancelled surgeries to the earliest
    derived open theatre slot of the matching room type. Staged under 'ot_reschedules'
    for /commit (which POSTs to Fabric -> surgery_reschedule -> ot_surgeries UPDATE).
    Approval gates: session commit (Hospilot) + fabric_approval_queue (CarerOS)."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_ot_scheduling"})
    sched = inp.booked_schedule
    targets = [s for s in sched if (s.get("status") or "").lower() == "cancelled"]
    if not targets and inp.goal:
        g = inp.goal.lower()
        targets = [s for s in sched
                   if ((s.get("surgery_name") or "").lower() and (s.get("surgery_name") or "").lower() in g)
                   or ((s.get("surgery_type") or "").lower() and (s.get("surgery_type") or "").lower() in g)]

    existing, taken, moved = await _current_ot_moves(inp.session_id)
    used = set(taken)
    proposals: list[dict] = []
    for s in targets:
        if s.get("id") in moved:
            continue
        dur = s.get("estimated_duration_minutes") or 60
        rtype = s.get("room_type") or s.get("surgery_type") or ""
        candidates = _derive_open_slots(inp.rooms, sched, rtype, dur)
        slot = next((sl for sl in candidates
                     if (sl["room_code"], sl["date"], sl["start"]) not in used), None)
        if not slot:
            continue
        used.add((slot["room_code"], slot["date"], slot["start"]))
        proposals.append(_move_proposal(s, slot, dur))

    if not proposals:
        await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_ot_scheduling",
                                         "result": {"rescheduled": 0}})
        logger.info("OT reschedule  session=%s  no target/slot", inp.session_id)
        return {"rescheduled": 0, "proposals": [], "status": "no_target_or_slot"}

    await _stage_ot_moves(inp.session_id, proposals, existing)
    await hasura.write_audit(inp.session_id, "ot_agent", "ot_reschedules_staged",
                             {"count": len(proposals),
                              "surgery_ids": [p["surgery_id"] for p in proposals]})
    for p in proposals:
        await broadcast(inp.session_id, {"type": "alert", "severity": "info",
            "message": f"Reschedule staged: {p.get('surgery_name') or p.get('surgery_code')} -> "
                       f"{p['to']['room_code']} {p['to']['date']} {p['to']['start']}."})
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_ot_scheduling",
                                     "result": {"rescheduled": len(proposals)}})
    logger.info("OT reschedule staged  session=%s  count=%d", inp.session_id, len(proposals))
    return {"rescheduled": len(proposals), "proposals": proposals, "status": "staged"}


@activity.defn
async def defer_ot_electives(inp: OtDeferInput) -> dict:
    """OT reprioritisation (executable): move the electives that capacity analysis flagged
    'delay' (lower-acuity cases yielding to a non-elective/emergency or a conflict) to a
    LATER open theatre slot. Rides the same 'ot_reschedules' move channel -> ot_surgeries
    UPDATE on commit. Makes the compare-and-defer recommendation actually happen."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_ot_analysis"})
    defer_codes = {r.get("surgery_code") for r in inp.case_recommendations
                   if (r.get("recommendation") or "").lower() == "delay"}
    if not defer_codes:
        await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_ot_analysis",
                                         "result": {"deferred": 0}})
        return {"deferred": 0, "proposals": [], "status": "nothing_to_defer"}

    by_code = {s.get("surgery_code"): s for s in inp.booked_schedule if s.get("surgery_code")}
    existing, taken, moved = await _current_ot_moves(inp.session_id)
    used = set(taken)
    proposals: list[dict] = []
    for code in defer_codes:
        s = by_code.get(code)
        if not s or s.get("id") in moved:
            continue
        dur = s.get("estimated_duration_minutes") or 60
        rtype = s.get("room_type") or s.get("surgery_type") or ""
        candidates = _derive_open_slots(inp.rooms, inp.booked_schedule, rtype, dur)
        # a DEFERRAL must land strictly AFTER the case's current start
        slot = next((sl for sl in candidates
                     if (sl["room_code"], sl["date"], sl["start"]) not in used
                     and _slot_dt(sl["date"], sl["start"])
                         > _slot_dt(s.get("scheduled_date"), s.get("scheduled_start_time"))), None)
        if not slot:
            continue
        used.add((slot["room_code"], slot["date"], slot["start"]))
        proposals.append(_move_proposal(s, slot, dur))

    if not proposals:
        await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_ot_analysis",
                                         "result": {"deferred": 0}})
        logger.info("OT defer  session=%s  nothing movable", inp.session_id)
        return {"deferred": 0, "proposals": [], "status": "no_slot"}

    await _stage_ot_moves(inp.session_id, proposals, existing)
    await hasura.write_audit(inp.session_id, "ot_agent", "ot_electives_deferred",
                             {"count": len(proposals),
                              "surgery_ids": [p["surgery_id"] for p in proposals]})
    for p in proposals:
        await broadcast(inp.session_id, {"type": "alert", "severity": "info",
            "message": f"Deferral staged: {p.get('surgery_name') or p.get('surgery_code')} -> "
                       f"{p['to']['room_code']} {p['to']['date']} {p['to']['start']}."})
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_ot_analysis",
                                     "result": {"deferred": len(proposals)}})
    logger.info("OT electives deferred  session=%s  count=%d", inp.session_id, len(proposals))
    return {"deferred": len(proposals), "proposals": proposals, "status": "staged"}


# -- sa_ot_utilization ---------------------------------------------------------

# -- sa_ot_turnaround_forecast -------------------------------------------------

# -- sa_ot_surgery_duration ----------------------------------------------------

# -- sa_ot_emergency_demand ----------------------------------------------------

# -- sa_ot_equipment_utilization -----------------------------------------------

# No OT equipment inventory/status source exists (only per-surgery usage via
# cache.get_ot_equipment_usage). total_equipment is a documented placeholder so the
# utilization denominator is plausible; reserved/maintenance/out-of-order are 0.
_ASSUMED_TOTAL_EQUIPMENT = 40


# -- sa_ot_surgery_volume ------------------------------------------------------
