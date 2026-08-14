"""
Appointment Agent -- built-in task functions.

Each task is a plain async function with the uniform signature
    async def ta_xxx(session_id, ta_results, ctx) -> dict
and is dispatched by run_builtin_task (see builtin_tasks.py) from the generic
RegistryAgentWorkflow. Outputs match the task catalog in the registry / planner
SUB_AGENTS so typed conditions resolve correctly.

Data: hospilot-owned tables `hospilot.appointments` (flat/denormalised -- patient
name/phone/email + specialty inlined; linked to hospilot.visits visit_type='OPD'
via appointment_id) and `hospilot.doctor_slots`. The agent reads AND writes here
(no public-schema dependency). "Send / engage / escalate" actions are simulated
(WebSocket broadcast + audit) -- no SMS/WhatsApp infra exists.
"""

import logging
import re
from datetime import datetime, timezone, timedelta

from cache import redis as cache
from db.fabric import fpost, fpatch
from db.hasura import hasura
from workflows.temporal.workflow._escalation import start_escalating_approval
from api.routes.ws import broadcast
from util.idem import make_idem_key

logger = logging.getLogger("appointment_tasks")


_REMINDER_WINDOW_H = 48          # remind for appointments within the next 48h
_CHRONIC_THRESHOLD = 3           # >= 3 prior no-shows = chronic
_HIGH_RISK_PRIORS  = 2           # >= 2 prior no-show/cancel = high no-show risk
_BOOKING_BATCH_CAP = 10          # max patient<->slot pairs booked in one approval


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _name(appt: dict) -> str:
    return (appt.get("patient_name") or "").strip() or "Unknown patient"


def _goal_window(goal: str) -> tuple[datetime | None, datetime | None]:
    """Parse a coarse date window from the goal text so date-scoped goals
    ("tomorrow's appointments", "today", "this/next week", "next N days") are
    honoured. Returns (start, end) UTC bounds; (None, None) means no temporal
    scope -> all upcoming appointments (the pre-G8 behaviour)."""
    g = (goal or "").lower()
    now = _now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _eod(d: datetime) -> datetime:
        return d.replace(hour=23, minute=59, second=59, microsecond=0)

    if "tomorrow" in g:
        start = today + timedelta(days=1)
        return start, _eod(start)
    if "today" in g or "rest of the day" in g or "tonight" in g:
        return now, _eod(today)
    if "next week" in g:
        start = today + timedelta(days=(7 - today.weekday()))  # next Monday
        return start, _eod(start + timedelta(days=6))
    if "this week" in g:
        start = today - timedelta(days=today.weekday())          # this Monday
        return start, _eod(start + timedelta(days=6))
    m = re.search(r"next (\d+)\s*day", g)
    if m:
        return now, _eod(today + timedelta(days=int(m.group(1))))
    return None, None


async def _emit(sid: str, task_id: str, result: dict) -> dict:
    await broadcast(sid, {"type": "sub_agent_completed", "sub_agent": task_id, "result": result})
    return result


async def _alert(sid: str, severity: str, message: str) -> None:
    await broadcast(sid, {"type": "alert", "severity": severity, "message": message})


def _staff_workload_ok(ctx: dict) -> bool | None:
    """G5: read the staffing agent's workload gate from ctx (threaded in by the
    registry body via build_ctx). Returns None when no staffing signal is present
    so callers FAIL OPEN -- a booking is only blocked when staffing explicitly
    reports nurses are over workload (workload_ok is False)."""
    staff = ctx.get("staff_agent")
    if not isinstance(staff, dict):
        return None
    return staff.get("workload_ok")


# -- sa_appt_scheduling --------------------------------------------------------

def _norm_patient(p: dict) -> dict:
    """Normalize a patient record from any source (waitlist match, cancelled
    appointment, upstream-agent cohort) to the booking fields."""
    return {
        "patient_id":   p.get("patient_id") or p.get("patient_token") or p.get("token"),
        "patient_name": (p.get("patient_name") or p.get("name") or "").strip() or "Waitlisted patient",
        "phone":        p.get("phone"),
        "email":        p.get("email"),
    }


def _slot_time(slot: dict) -> str:
    return f"{slot.get('slot_date')}T{slot.get('slot_start')}"


def _upstream_cohort(ctx: dict) -> list[dict]:
    """G10: patients an upstream agent identified as needing an appointment booked
    (e.g. ER critical patients flagged for urgent specialist follow-up). Read from
    ctx (threaded in by the registry body via build_ctx), normalized and deduped."""
    out, seen = [], set()
    er = ctx.get("er_agent") or {}
    for p in (er.get("critical_patients") or []):
        np = _norm_patient(p)
        key = np["patient_id"] or np["patient_name"]
        if key and key not in seen:
            seen.add(key)
            out.append(np)
    return out


def _booking_pairs(ta_results: dict, ctx: dict, slots: list, appts: list) -> list[dict]:
    """G4/G10: resolve the (patient, slot) pairs to book, in priority order:
      1. waitlist matches (G2 ta_appt_match_waitlist) -- already paired to slots
      2. an upstream-identified cohort (ctx) paired to the earliest open slots
      3. the cancellation pool paired to open slots (legacy fallback, now multi-pair)
    Returns up to _BOOKING_BATCH_CAP pairs, each {"patient": ..., "slot": ...}.
    Replaces the old "book the first cancelled (else first) patient into slots[0]"."""
    matches = (ta_results.get("ta_appt_match_waitlist") or {}).get("matches") or []
    if matches:
        slot_by_id = {s.get("id"): s for s in slots}
        pairs = []
        for i, m in enumerate(matches[:_BOOKING_BATCH_CAP]):
            slot = slot_by_id.get(m.get("slot_id")) or (slots[i] if i < len(slots) else None)
            if slot:
                pairs.append({"patient": _norm_patient(m), "slot": slot})
        if pairs:
            return pairs

    cohort = _upstream_cohort(ctx)
    if not cohort:
        cancelled = [a for a in appts if (a.get("status") or "").lower() == "cancelled"]
        cohort = [_norm_patient(a) for a in (cancelled or appts[:1])]
    n = min(len(cohort), len(slots), _BOOKING_BATCH_CAP)
    return [{"patient": cohort[i], "slot": slots[i]} for i in range(n)]


async def ta_appt_find_available_slots(session_id, ta_results, ctx) -> dict:
    all_slots = await cache.get_all_doctor_slots()
    slots = [s for s in all_slots if (s.get("booked_count") or 0) < (s.get("max_patients") or 1)]
    # slots = await hasura.appt_available_slots()
    by_spec: dict[str, int] = {}
    for s in slots:
        spec = s.get("specialization") or "General"
        by_spec[spec] = by_spec.get(spec, 0) + 1
    earliest = slots[0] if slots else None
    result = {
        "available_slot_count": len(slots),
        "earliest_slot": (f"{earliest['slot_date']} {earliest['slot_start']}" if earliest else None),
        "slots_by_specialty": by_spec,
        "_slots": slots[:50],
    }
    logger.info("appt slots  session=%s  available=%d", session_id, len(slots))
    return await _emit(session_id, "ta_appt_find_available_slots", result)


async def ta_appt_match_specialty(session_id, ta_results, ctx) -> dict:
    goal = (ctx.get("_goal") or "").lower()
    all_slots = await cache.get_all_doctor_slots()
    bookable = [s for s in all_slots if (s.get("booked_count") or 0) < (s.get("max_patients") or 1)]
    slots = (ta_results.get("ta_appt_find_available_slots") or {}).get("_slots") or bookable
    # slots = (ta_results.get("ta_appt_find_available_slots") or {}).get("_slots") \
    #     or await hasura.appt_available_slots()
    specialties = sorted({s.get("specialization") or "General" for s in slots})
    matched = [sp for sp in specialties if sp.lower() in goal or any(w in sp.lower() for w in goal.split())]
    result = {
        "specialty_matched": bool(matched),
        "matched_specialties": matched,
        "provider_count": len({s.get("provider_id") for s in slots if (not matched) or (s.get("specialization") in matched)}),
    }
    return await _emit(session_id, "ta_appt_match_specialty", result)


def _spec_match(name: str | None, targets: set) -> bool:
    """Loose specialty match: empty target set matches anything; otherwise match
    on equality or substring either direction ('cardio' ~ 'Cardiology')."""
    n = (name or "").lower().strip()
    if not targets:
        return True
    if n in targets:
        return True
    return any(t and (t in n or n in t) for t in targets)


_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}

# G16: appointment types/specialties that must NOT be moved. Anything not matching
# is treated as non-urgent / movable (Q3: "reschedule non-urgent appointments").
_URGENT_TYPES = (
    "emergency", "urgent", "stat", "procedure", "surgery", "surgical", "operation",
    "pre-op", "preop", "post-op", "postop", "oncology", "chemo", "chemotherapy",
    "dialysis", "radiation", "transplant", "biopsy", "infusion",
)


def _is_urgent_appt(a: dict) -> bool:
    blob = f"{a.get('type') or ''} {a.get('specialization') or ''}".lower()
    return any(k in blob for k in _URGENT_TYPES)


def _appt_hour(a: dict) -> int | None:
    t = _parse(a.get("appointment_time") or a.get("time"))
    return t.hour if t else None


def _slot_hour(s: dict) -> int | None:
    try:
        return int(str(s.get("slot_start") or "").split(":")[0])
    except (ValueError, AttributeError):
        return None


def _avoid_hours(ctx: dict, movable: list) -> tuple[set, str]:
    """G14/G10: the hours to move appointments AWAY from. Prefers the staffing
    agent's peak understaffed hours (G15) threaded in via ctx; falls back to the
    busiest scheduled-appointment hours when no staffing signal is present."""
    staff = ctx.get("staff_agent")
    if isinstance(staff, dict):
        hrs = staff.get("peak_understaffed_hours")
        if hrs:
            return {int(h) for h in hrs}, "staff_understaffed_hours"
    from collections import Counter
    counts: Counter = Counter()
    for a in movable:
        h = _appt_hour(a)
        if h is not None:
            counts[h] += 1
    return {h for h, _ in counts.most_common(3)}, "appointment_volume"


async def ta_appt_match_waitlist(session_id, ta_results, ctx) -> dict:
    """G2: pair waitlisted patients to open OPD slots.

    Reads the real waitlist (G1, hospilot.waitlist via cache, Hasura fallback),
    scopes to the goal's requested specialty, orders patients by priority then
    request age, and assigns each to the earliest open same-specialty slot. Emits
    `matches` (patient<->slot pairs) which ta_appt_reserve_slot consumes (G4) to
    book each patient -- replacing the old "book one first-found patient"."""
    waitlist = await cache.get_all_waitlist()
    if not waitlist:
        try:
            waitlist = await hasura.appt_list_waitlist()
        except Exception:
            waitlist = []
    waitlist = [w for w in waitlist if (w.get("status") or "waitlisted").lower() == "waitlisted"]

    slots = (ta_results.get("ta_appt_find_available_slots") or {}).get("_slots")
    if slots is None:
        all_slots = await cache.get_all_doctor_slots()
        slots = [s for s in all_slots if (s.get("booked_count") or 0) < (s.get("max_patients") or 1)]
    open_slots = sorted(slots, key=lambda s: (str(s.get("slot_date")), str(s.get("slot_start"))))

    # Specialty scope: prefer specialties matched upstream; else infer from goal tokens.
    scope = {s.lower() for s in ((ta_results.get("ta_appt_match_specialty") or {}).get("matched_specialties") or [])}
    if not scope:
        goal = (ctx.get("_goal") or "").lower()
        scope = {(w.get("specialization") or "").lower() for w in waitlist
                 if _spec_match(w.get("specialization"), {tok for tok in goal.split() if len(tok) > 3})}
        scope.discard("")

    relevant = sorted(
        [w for w in waitlist if _spec_match(w.get("specialization"), scope)],
        key=lambda w: (_PRIORITY_RANK.get((w.get("priority") or "low").lower(), 3),
                       str(w.get("requested_date") or w.get("created_at") or "")),
    )

    matches: list[dict] = []
    used: set = set()
    for w in relevant:
        wspec = (w.get("specialization") or "").lower()
        slot = next((s for s in open_slots
                     if s["id"] not in used and _spec_match(s.get("specialization"), {wspec} if wspec else set())),
                    None)
        if not slot:
            continue
        used.add(slot["id"])
        matches.append({
            "waitlist_id":    w.get("id"),
            "patient_id":     w.get("patient_id"),
            "patient_name":   w.get("patient_name"),
            "phone":          w.get("phone"),
            "email":          w.get("email"),
            "specialization": slot.get("specialization") or w.get("specialization"),
            "priority":       w.get("priority"),
            "slot_id":        slot["id"],
            "provider_id":    slot.get("provider_id"),
            "appt_time":      _slot_time(slot),
        })

    if matches:
        await _alert(session_id, "info",
                     f"Matched {len(matches)} waitlisted patient(s) to open slots "
                     f"({len(relevant) - len(matches)} still waiting).")
    result = {
        "waitlist_count":  len(relevant),
        "matched_count":   len(matches),
        "unmatched_count": len(relevant) - len(matches),
        "matches":         matches,
    }
    logger.info("waitlist match  session=%s  waitlist=%d  matched=%d", session_id, len(relevant), len(matches))
    return await _emit(session_id, "ta_appt_match_waitlist", result)


async def ta_appt_reserve_slot(session_id, ta_results, ctx) -> dict:
    """Create a human-approval task for the chosen slot. The workflow waits for the
    decision and then runs ta_appt_confirm_booking -- booking is NOT done here."""
    all_slots = await cache.get_all_doctor_slots()
    bookable = [s for s in all_slots if (s.get("booked_count") or 0) < (s.get("max_patients") or 1)]
    slots = (ta_results.get("ta_appt_find_available_slots") or {}).get("_slots") or bookable
    # slots = (ta_results.get("ta_appt_find_available_slots") or {}).get("_slots") \
    #     or await hasura.appt_available_slots()
    if not slots:
        return await _emit(session_id, "ta_appt_reserve_slot", {"slot_reserved": 0, "appointment_id": None})

    # G5: "confirm only if the assigned clinic nurses aren't already over their
    # workload." Block the booking BEFORE requesting approval when the staffing
    # agent reports nurses are over workload. Fail open if no staffing signal.
    if _staff_workload_ok(ctx) is False:
        await _alert(session_id, "warning",
                     "Booking held: assigned clinic nurses are over their workload -- "
                     "rebalance staffing before confirming bookings.")
        return await _emit(session_id, "ta_appt_reserve_slot",
                           {"slot_reserved": 0, "appointment_id": None,
                            "status": "blocked_over_workload", "_needs_approval": False})

    appts = await cache.get_all_appointments()
    # appts = await hasura.appt_list_appointments()

    # G4/G10: book each patient<->slot pair for the resolved cohort (waitlist matches,
    # else an upstream-identified cohort, else the cancellation pool) -- not just the
    # first-found patient into the first slot.
    pairs = _booking_pairs(ta_results, ctx, slots, appts)
    if not pairs:
        return await _emit(session_id, "ta_appt_reserve_slot", {"slot_reserved": 0, "appointment_id": None})

    bookings = [{
        "slot_id":        pr["slot"]["id"],
        "patient_id":     pr["patient"]["patient_id"],
        "provider_id":    pr["slot"].get("provider_id"),
        "appt_time":      _slot_time(pr["slot"]),
        "specialization": pr["slot"].get("specialization") or "OPD",
        "patient_name":   pr["patient"]["patient_name"],
        "phone":          pr["patient"].get("phone"),
        "email":          pr["patient"].get("email"),
    } for pr in pairs]

    assignments = [{
        "patient": {"name": b["patient_name"], "phone": b["phone"]},
        "patient_name": b["patient_name"],
        "slot": {"time": b["appt_time"], "specialty": b["specialization"]},
    } for b in bookings]

    approval = await hasura.create_approval_task(
        session_id=session_id, agent_id="appointment_agent",
        action_type="appointment_booking",
        payload={"bookings": bookings},
        idempotency_key=make_idem_key("appointment_booking", session_id,
                                      sorted(b["slot_id"] for b in bookings)),
    )
    approval_id = approval["id"]

    await broadcast(session_id, {
        "type": "approval_required",
        "approval_id": approval_id,
        "action": "appointment_booking",
        "appointment_time": bookings[0]["appt_time"],
        "specialization": bookings[0]["specialization"],
        "assignments": assignments,
    })
    summary = (f"{len(bookings)} appointment(s)" if len(bookings) > 1
               else f"{bookings[0]['specialization']} slot at {bookings[0]['appt_time']} "
                    f"for {bookings[0]['patient_name']}")
    await _alert(session_id, "info", f"Approval requested: book {summary}.")
    await start_escalating_approval(
        session_id=session_id,
        approval_id=approval_id,
        agent_id="appointment_agent",
        action_type="appointment_booking",
        payload={"bookings": bookings},
    )

    first = bookings[0]
    result = {
        "_needs_approval": True, "approval_id": approval_id,
        "confirm_task": "ta_appt_confirm_booking",
        "bookings": bookings, "matched_count": len(bookings), "slot_reserved": 0,
        # flat first-booking fields kept for backward-compatible consumers
        "slot_id": first["slot_id"], "patient_id": first["patient_id"],
        "provider_id": first["provider_id"], "appt_time": first["appt_time"],
        "specialization": first["specialization"],
        "patient_name": first["patient_name"], "phone": first["phone"], "email": first["email"],
    }
    return await _emit(session_id, "ta_appt_reserve_slot", result)


async def ta_appt_confirm_booking(session_id, ta_results, ctx) -> dict:
    """Runs ONLY after approval (dispatched by the workflow). Stages the booking for
    /commit -- does NOT write to Fabric. Not a registry task -- never planned in the normal loop."""
    r = ta_results.get("ta_appt_reserve_slot") or {}
    if r.get("approval_decision") and r.get("approval_decision") != "approved":
        return await _emit(session_id, "ta_appt_confirm_booking", {"slot_reserved": 0, "status": "rejected"})
    # G5: re-check the staffing gate at confirm time (covers the case where staffing
    # resolved after the slot was reserved). Never book over an explicit overload.
    if _staff_workload_ok(ctx) is False:
        await _alert(session_id, "warning",
                     "Booking not confirmed: clinic nurses are over workload -- staffing must be rebalanced first.")
        return await _emit(session_id, "ta_appt_confirm_booking",
                           {"slot_reserved": 0, "status": "blocked_over_workload"})
    # G4: stage every booking in the approved batch (waitlist matches / cohort),
    # falling back to the flat single-booking fields for older reserve results.
    bookings = r.get("bookings")
    if not bookings and r.get("slot_id"):
        bookings = [{
            "slot_id": r.get("slot_id"), "patient_id": r.get("patient_id"),
            "provider_id": r.get("provider_id"), "appt_time": r.get("appt_time"),
            "patient_name": r.get("patient_name"), "phone": r.get("phone"),
            "email": r.get("email"), "specialization": r.get("specialization"),
        }]
    if not bookings:
        return await _emit(session_id, "ta_appt_confirm_booking", {"slot_reserved": 0, "appointment_id": None})

    # Stage the FULL batch as a list under one key (cache.stage overwrites per key,
    # so a per-booking loop would keep only the last). Commit iterates this list.
    staged_bookings = [{
        "slot_id":        b["slot_id"],
        "patient_id":     b.get("patient_id"),
        "provider_id":    b.get("provider_id"),
        "appt_time":      b.get("appt_time"),
        "patient_name":   b.get("patient_name"),
        "phone":          b.get("phone"),
        "email":          b.get("email"),
        "specialization": b.get("specialization"),
    } for b in bookings if b.get("slot_id")]
    from cache import redis as cache
    await cache.stage(session_id, "appointments", staged_bookings)
    staged = len(staged_bookings)
    await hasura.write_audit(session_id, "appointment_agent", "appointment_staged",
                             {"count": staged, "slot_ids": [b["slot_id"] for b in staged_bookings]})
    await _alert(session_id, "info",
                 f"{staged} appointment(s) staged for commit.")
    return await _emit(session_id, "ta_appt_confirm_booking",
                       {"slot_reserved": staged, "appointment_id": None, "staged": True,
                        "booked_count": staged})


async def ta_appt_prioritize_urgent(session_id, ta_results, ctx) -> dict:
    all_slots = await cache.get_all_doctor_slots()
    bookable = [s for s in all_slots if (s.get("booked_count") or 0) < (s.get("max_patients") or 1)]
    slots = (ta_results.get("ta_appt_find_available_slots") or {}).get("_slots") or bookable
    # slots = (ta_results.get("ta_appt_find_available_slots") or {}).get("_slots") \
    #     or await hasura.appt_available_slots()
    today = _now().date().isoformat()
    same_day = [s for s in slots if str(s.get("slot_date")) == today]
    result = {"same_day_available": len(same_day), "urgent_slot_found": bool(same_day or slots)}
    if not same_day and slots:
        await _alert(session_id, "warning", "No same-day slot free -- earliest available offered as urgent fallback.")
    return await _emit(session_id, "ta_appt_prioritize_urgent", result)


async def ta_appt_coordinate_multispecialty(session_id, ta_results, ctx) -> dict:
    all_slots = await cache.get_all_doctor_slots()
    bookable = [s for s in all_slots if (s.get("booked_count") or 0) < (s.get("max_patients") or 1)]
    slots = (ta_results.get("ta_appt_find_available_slots") or {}).get("_slots") or bookable
    # slots = (ta_results.get("ta_appt_find_available_slots") or {}).get("_slots") \
    #     or await hasura.appt_available_slots()
    by_start: dict[str, set] = {}
    for s in slots:
        spec = s.get("specialization") or "General"
        by_start.setdefault(str(s.get("slot_start")), set()).add(spec)
    common = {t: sorted(sp) for t, sp in by_start.items() if len(sp) >= 2}
    result = {
        "specialties_available": len({s.get("specialization") for s in slots}),
        "common_window_found": bool(common),
        "common_windows": common,
    }
    return await _emit(session_id, "ta_appt_coordinate_multispecialty", result)


async def ta_appt_fill_cancellation(session_id, ta_results, ctx) -> dict:
    appts = await cache.get_all_appointments()
    # appts = await hasura.appt_list_appointments()
    cancelled = [a for a in appts if (a.get("status") or "").lower() == "cancelled"]
    all_slots = await cache.get_all_doctor_slots()
    bookable = [s for s in all_slots if (s.get("booked_count") or 0) < (s.get("max_patients") or 1)]
    slots = (ta_results.get("ta_appt_find_available_slots") or {}).get("_slots") or bookable
    # slots = (ta_results.get("ta_appt_find_available_slots") or {}).get("_slots") \
    #     or await hasura.appt_available_slots()
    fillable = min(len(cancelled), len(slots))
    if fillable:
        await _alert(session_id, "info", f"{fillable} cancelled slot(s) can be filled from available capacity.")
    result = {"cancelled_count": len(cancelled), "slots_fillable": fillable}
    return await _emit(session_id, "ta_appt_fill_cancellation", result)


def _upcoming_scheduled(appts: list[dict]) -> list[dict]:
    return [a for a in appts
            if (a.get("status") or "").lower() == "scheduled"
            and (lambda t: t and t >= _now())(_parse(a.get("appointment_time")))]


def _movable_record(a: dict) -> dict:
    return {"id": a.get("id"), "patient_id": a.get("patient_id"), "name": _name(a),
            "type": a.get("type"), "specialization": a.get("specialization"),
            "appointment_time": a.get("appointment_time"),
            "phone": a.get("phone"), "email": a.get("email")}


async def ta_appt_classify_movable(session_id, ta_results, ctx) -> dict:
    """G16: classify upcoming scheduled appointments as urgent (must stay) vs
    non-urgent / movable, so the reschedule task knows which can be moved off
    peak understaffed hours (Q3)."""
    appts = await cache.get_all_appointments()
    scheduled = _upcoming_scheduled(appts)
    movable = [_movable_record(a) for a in scheduled if not _is_urgent_appt(a)]
    urgent_count = len(scheduled) - len(movable)
    result = {"assessed": len(scheduled), "movable_count": len(movable),
              "urgent_count": urgent_count, "movable": movable}
    logger.info("appt movability  session=%s  assessed=%d  movable=%d  urgent=%d",
                session_id, len(scheduled), len(movable), urgent_count)
    return await _emit(session_id, "ta_appt_classify_movable", result)


async def ta_appt_reschedule(session_id, ta_results, ctx) -> dict:
    """G14: move non-urgent appointments away from peak understaffed hours to the
    earliest open off-peak slot. Consumes movable appts (G16) + the staffing agent's
    peak hours (G15) via ctx (G10). Creates a batch approval; the off-peak moves are
    staged on confirm (ta_appt_confirm_reschedule)."""
    movable = (ta_results.get("ta_appt_classify_movable") or {}).get("movable")
    if movable is None:
        appts = await cache.get_all_appointments()
        movable = [_movable_record(a) for a in _upcoming_scheduled(appts) if not _is_urgent_appt(a)]

    avoid_hours, source = _avoid_hours(ctx, movable)
    if not avoid_hours:
        return await _emit(session_id, "ta_appt_reschedule",
                           {"rescheduled": 0, "proposed_count": 0, "avoid_hours": [], "avoid_source": source})

    all_slots = await cache.get_all_doctor_slots()
    bookable = [s for s in all_slots if (s.get("booked_count") or 0) < (s.get("max_patients") or 1)]
    slots = (ta_results.get("ta_appt_find_available_slots") or {}).get("_slots") or bookable
    off_peak = sorted([s for s in slots if _slot_hour(s) not in avoid_hours],
                      key=lambda s: (str(s.get("slot_date")), str(s.get("slot_start"))))

    targets = [a for a in movable if _appt_hour(a) in avoid_hours]
    proposals: list[dict] = []
    used: set = set()
    for a in targets:
        spec = (a.get("specialization") or "").lower()
        slot = next((s for s in off_peak if s["id"] not in used
                     and _spec_match(s.get("specialization"), {spec} if spec else set())), None)
        if not slot:
            continue
        used.add(slot["id"])
        proposals.append({
            "appointment_id": a.get("id"), "patient_id": a.get("patient_id"),
            "patient_name": a.get("name") or a.get("patient_name"),
            "phone": a.get("phone"), "email": a.get("email"),
            "from_time": a.get("appointment_time") or a.get("time"),
            "to_slot_id": slot["id"], "to_provider_id": slot.get("provider_id"),
            "to_time": _slot_time(slot),
            "specialization": slot.get("specialization") or a.get("specialization"),
        })

    if not proposals:
        return await _emit(session_id, "ta_appt_reschedule",
                           {"rescheduled": 0, "proposed_count": 0,
                            "avoid_hours": sorted(avoid_hours), "avoid_source": source})

    approval = await hasura.create_approval_task(
        session_id=session_id, agent_id="appointment_agent",
        action_type="appointment_reschedule",
        payload={"reschedules": proposals},
        idempotency_key=make_idem_key("appointment_reschedule", session_id,
                                      sorted(p["appointment_id"] for p in proposals if p.get("appointment_id"))),
    )
    approval_id = approval["id"]
    await broadcast(session_id, {
        "type": "approval_required", "approval_id": approval_id,
        "action": "appointment_reschedule",
        "assignments": [{
            "patient_name": p["patient_name"],
            "from": p["from_time"], "to": p["to_time"], "specialty": p["specialization"],
        } for p in proposals],
    })
    await _alert(session_id, "info",
                 f"Approval requested: reschedule {len(proposals)} non-urgent appointment(s) "
                 f"away from peak hours {sorted(avoid_hours)} to off-peak slots.")
    await start_escalating_approval(
        session_id=session_id, approval_id=approval_id,
        agent_id="appointment_agent", action_type="appointment_reschedule",
        payload={"reschedules": proposals},
    )
    result = {
        "_needs_approval": True, "approval_id": approval_id,
        "confirm_task": "ta_appt_confirm_reschedule",
        "reschedule_proposals": proposals, "proposed_count": len(proposals),
        "avoid_hours": sorted(avoid_hours), "avoid_source": source, "rescheduled": 0,
    }
    return await _emit(session_id, "ta_appt_reschedule", result)


async def ta_appt_confirm_reschedule(session_id, ta_results, ctx) -> dict:
    """Runs ONLY after approval (dispatched by the workflow). Stages the off-peak
    moves for /commit. Not a registry task -- never planned in the normal loop."""
    r = ta_results.get("ta_appt_reschedule") or {}
    if r.get("approval_decision") and r.get("approval_decision") != "approved":
        return await _emit(session_id, "ta_appt_confirm_reschedule", {"rescheduled": 0, "status": "rejected"})
    proposals = r.get("reschedule_proposals") or []
    proposals = [p for p in proposals if p.get("to_slot_id")]
    if not proposals:
        return await _emit(session_id, "ta_appt_confirm_reschedule", {"rescheduled": 0})

    from cache import redis as cache
    await cache.stage(session_id, "appointment_reschedules", proposals)
    await hasura.write_audit(session_id, "appointment_agent", "appointments_rescheduled",
                             {"count": len(proposals),
                              "appointment_ids": [p.get("appointment_id") for p in proposals]})
    await _alert(session_id, "info",
                 f"{len(proposals)} appointment(s) staged for reschedule to off-peak slots.")
    return await _emit(session_id, "ta_appt_confirm_reschedule",
                       {"rescheduled": len(proposals), "staged": True})


# -- Non-OPD slot types (sample collection / pharmacy pickup) -- G23 / G39 ------

_SERVICE_SLOT_KEYWORDS = {
    "sample_collection": ["sample collection", "sample-collection", "phlebotomy", "blood draw",
                          "blood collection", "specimen", "lab sample"],
    "pharmacy_pickup":   ["pickup", "pick-up", "pick up", "pharmacy pickup", "dispens",
                          "collect medication", "medication pickup", "prescription pickup"],
}


def _service_slot_type(goal: str) -> str | None:
    g = (goal or "").lower()
    for st, kws in _SERVICE_SLOT_KEYWORDS.items():
        if any(k in g for k in kws):
            return st
    return None


def _open_service_slots(all_slots: list, slot_type: str | None) -> list:
    out = [s for s in all_slots
           if (s.get("status") or "open").lower() == "open"
           and (s.get("booked_count") or 0) < (s.get("max_patients") or 1)
           and (not slot_type or (s.get("slot_type") or "").lower() == slot_type)]
    return sorted(out, key=lambda s: (str(s.get("slot_date")), str(s.get("slot_start"))))


async def ta_appt_find_service_slots(session_id, ta_results, ctx) -> dict:
    """G23/G39: find open non-OPD slots (sample_collection / pharmacy_pickup),
    scoped to the type implied by the goal."""
    slot_type = _service_slot_type(ctx.get("_goal", ""))
    all_slots = await cache.get_all_service_slots()
    if not all_slots:
        try:
            all_slots = await hasura.appt_list_service_slots(slot_type)
        except Exception:
            all_slots = []
    open_slots = _open_service_slots(all_slots, slot_type)
    by_type: dict[str, int] = {}
    for s in open_slots:
        by_type[s.get("slot_type")] = by_type.get(s.get("slot_type"), 0) + 1
    earliest = open_slots[0] if open_slots else None
    result = {
        "slot_type": slot_type,
        "available_slot_count": len(open_slots),
        "slots_by_type": by_type,
        "earliest_slot": (f"{earliest['slot_date']} {earliest['slot_start']}" if earliest else None),
        "_service_slots": open_slots[:50],
    }
    logger.info("service slots  session=%s  type=%s  available=%d", session_id, slot_type, len(open_slots))
    return await _emit(session_id, "ta_appt_find_service_slots", result)


async def ta_appt_book_service_slot(session_id, ta_results, ctx) -> dict:
    """G23/G39: book a cohort into non-OPD slots (sample collection / pharmacy pickup)
    and notify them of the window. Reuses the cohort resolution (upstream cohort ->
    cancellation pool) and the approval->confirm pattern."""
    find = ta_results.get("ta_appt_find_service_slots") or {}
    slot_type = find.get("slot_type") or _service_slot_type(ctx.get("_goal", ""))
    slots = find.get("_service_slots")
    if slots is None:
        slots = _open_service_slots(await cache.get_all_service_slots(), slot_type)
    if not slots:
        return await _emit(session_id, "ta_appt_book_service_slot",
                           {"slot_reserved": 0, "booked_count": 0, "slot_type": slot_type})

    appts = await cache.get_all_appointments()
    pairs = _booking_pairs(ta_results, ctx, slots, appts)
    if not pairs:
        return await _emit(session_id, "ta_appt_book_service_slot",
                           {"slot_reserved": 0, "booked_count": 0, "slot_type": slot_type})

    bookings = [{
        "slot_id":        pr["slot"]["id"],
        "slot_type":      pr["slot"].get("slot_type") or slot_type,
        "patient_id":     pr["patient"]["patient_id"],
        "patient_name":   pr["patient"]["patient_name"],
        "phone":          pr["patient"].get("phone"),
        "email":          pr["patient"].get("email"),
        "appt_time":      _slot_time(pr["slot"]),
        "location":       pr["slot"].get("location"),
        "specialization": pr["slot"].get("specialization"),
    } for pr in pairs]

    label = "pickup" if slot_type == "pharmacy_pickup" else "collection"
    approval = await hasura.create_approval_task(
        session_id=session_id, agent_id="appointment_agent",
        action_type="service_booking",
        payload={"bookings": bookings, "slot_type": slot_type},
        idempotency_key=make_idem_key("service_booking", session_id, slot_type,
                                      sorted(b["slot_id"] for b in bookings)),
    )
    approval_id = approval["id"]
    await broadcast(session_id, {
        "type": "approval_required", "approval_id": approval_id,
        "action": "service_booking", "slot_type": slot_type,
        "assignments": [{
            "patient_name": b["patient_name"],
            "slot": {"time": b["appt_time"], "location": b["location"], "type": b["slot_type"]},
        } for b in bookings],
    })
    await _alert(session_id, "info",
                 f"Approval requested: schedule {len(bookings)} {label} window(s) "
                 f"({slot_type or 'service'}).")
    await start_escalating_approval(
        session_id=session_id, approval_id=approval_id,
        agent_id="appointment_agent", action_type="service_booking",
        payload={"bookings": bookings, "slot_type": slot_type},
    )
    result = {
        "_needs_approval": True, "approval_id": approval_id,
        "confirm_task": "ta_appt_confirm_service_booking",
        "bookings": bookings, "slot_type": slot_type,
        "matched_count": len(bookings), "slot_reserved": 0,
        "windows": [{"patient_name": b["patient_name"], "time": b["appt_time"],
                     "location": b["location"]} for b in bookings],
    }
    return await _emit(session_id, "ta_appt_book_service_slot", result)


async def ta_appt_confirm_service_booking(session_id, ta_results, ctx) -> dict:
    """Runs ONLY after approval. Stages the non-OPD bookings for /commit and notifies
    patients of their window. Not a registry task -- never planned in the normal loop."""
    r = ta_results.get("ta_appt_book_service_slot") or {}
    if r.get("approval_decision") and r.get("approval_decision") != "approved":
        return await _emit(session_id, "ta_appt_confirm_service_booking", {"slot_reserved": 0, "status": "rejected"})
    bookings = [b for b in (r.get("bookings") or []) if b.get("slot_id")]
    if not bookings:
        return await _emit(session_id, "ta_appt_confirm_service_booking", {"slot_reserved": 0})

    from cache import redis as cache
    await cache.stage(session_id, "service_bookings", bookings)
    for b in bookings:
        await broadcast(session_id, {"type": "alert", "severity": "info",
                                     "message": f"{b['patient_name']}: {b.get('slot_type')} window "
                                                f"{b.get('appt_time')} at {b.get('location')}."})
    await hasura.write_audit(session_id, "appointment_agent", "service_slots_staged",
                             {"count": len(bookings), "slot_type": r.get("slot_type"),
                              "slot_ids": [b["slot_id"] for b in bookings]})
    return await _emit(session_id, "ta_appt_confirm_service_booking",
                       {"slot_reserved": len(bookings), "booked_count": len(bookings),
                        "slot_type": r.get("slot_type"), "staged": True})


# -- sa_appt_reminder ----------------------------------------------------------

def _due_appointments(appts: list[dict]) -> list[dict]:
    now, horizon = _now(), _now() + timedelta(hours=_REMINDER_WINDOW_H)
    out = []
    for a in appts:
        if (a.get("status") or "").lower() != "scheduled":
            continue
        t = _parse(a.get("appointment_time"))
        if t and now <= t <= horizon:
            out.append(a)
    return out


async def ta_appt_get_due_reminders(session_id, ta_results, ctx) -> dict:
    appts = await cache.get_all_appointments()
    # appts = await hasura.appt_list_appointments()
    due = _due_appointments(appts)
    result = {
        "due_count": len(due),
        "appointments_due": [
            {"id": a["id"], "patient_id": a.get("patient_id"), "name": _name(a),
             "phone": a.get("phone"),
             "email": a.get("email"),
             "type": a.get("type"), "time": a.get("appointment_time")}
            for a in due
        ],
    }
    logger.info("appt reminders due  session=%s  count=%d", session_id, len(due))
    return await _emit(session_id, "ta_appt_get_due_reminders", result)


async def ta_appt_resolve_channel(session_id, ta_results, ctx) -> dict:
    due = (ta_results.get("ta_appt_get_due_reminders") or {}).get("appointments_due") or []
    sms = sum(1 for d in due if d.get("phone"))
    email = sum(1 for d in due if d.get("email"))
    result = {"channel_resolved": sum(1 for d in due if d.get("phone") or d.get("email")),
              "sms_count": sms, "email_count": email}
    return await _emit(session_id, "ta_appt_resolve_channel", result)


async def ta_appt_send_reminders(session_id, ta_results, ctx) -> dict:
    due = (ta_results.get("ta_appt_get_due_reminders") or {}).get("appointments_due") or []
    sent = 0
    for d in due:
        channel = "SMS" if d.get("phone") else ("Email" if d.get("email") else None)
        if not channel:
            continue
        await broadcast(session_id, {"type": "alert", "severity": "info",
                                     "message": f"Reminder ({channel}) sent to {d['name']} for {d.get('type')} on {d.get('time')}."})
        sent += 1
    await hasura.write_audit(session_id, "appointment_agent", "reminders_sent", {"count": sent})
    return await _emit(session_id, "ta_appt_send_reminders", {"reminders_sent": sent, "delivery_failed": len(due) - sent})


async def ta_appt_prep_instructions(session_id, ta_results, ctx) -> dict:
    due = (ta_results.get("ta_appt_get_due_reminders") or {}).get("appointments_due") or []
    needs_prep = [d for d in due if any(k in (d.get("type") or "").lower() for k in ("lab", "diagnostic", "scan", "review"))]
    for d in needs_prep:
        await broadcast(session_id, {"type": "alert", "severity": "info",
                                     "message": f"Pre-visit prep sent to {d['name']} (fasting / preparation for {d.get('type')})."})
    return await _emit(session_id, "ta_appt_prep_instructions",
                       {"prep_sent": len(needs_prep), "patients_with_prep": len(needs_prep)})


async def ta_appt_followup_reminders(session_id, ta_results, ctx) -> dict:
    appts = await cache.get_all_appointments()
    # appts = await hasura.appt_list_appointments()
    now, horizon = _now(), _now() + timedelta(hours=_REMINDER_WINDOW_H)
    followups = [a for a in appts
                 if (a.get("type") or "").lower() == "follow-up"
                 and (a.get("status") or "").lower() == "scheduled"
                 and (lambda t: t and now <= t <= horizon)(_parse(a.get("appointment_time")))]
    for a in followups:
        await broadcast(session_id, {"type": "alert", "severity": "info",
                                     "message": f"Follow-up reminder sent to {_name(a)} for {a.get('appointment_time')}."})
    return await _emit(session_id, "ta_appt_followup_reminders",
                       {"followup_count": len(followups), "followup_sent": len(followups)})


async def ta_appt_escalate_reminder(session_id, ta_results, ctx) -> dict:
    appts = await cache.get_all_appointments()
    # appts = await hasura.appt_list_appointments()
    risky = _high_risk_patient_ids(appts)
    due = (ta_results.get("ta_appt_get_due_reminders") or {}).get("appointments_due") or []
    escalated = [d for d in due if d.get("patient_id") in risky]
    for d in escalated:
        await _alert(session_id, "warning",
                     f"High-risk patient {d['name']} unconfirmed -- escalating to voice call / care coordinator.")
    return await _emit(session_id, "ta_appt_escalate_reminder", {"escalated_count": len(escalated)})


# -- sa_appt_noshow ------------------------------------------------------------

def _history_counts(appts: list[dict]) -> dict:
    """patient_id -> {'no_show': n, 'cancelled': n}"""
    counts: dict[str, dict] = {}
    for a in appts:
        pid = a.get("patient_id")
        if not pid:
            continue
        st = (a.get("status") or "").lower()
        c = counts.setdefault(pid, {"no_show": 0, "cancelled": 0})
        if st == "no show":
            c["no_show"] += 1
        elif st == "cancelled":
            c["cancelled"] += 1
    return counts


def _high_risk_patient_ids(appts: list[dict]) -> set:
    counts = _history_counts(appts)
    return {pid for pid, c in counts.items() if (c["no_show"] + c["cancelled"]) >= _HIGH_RISK_PRIORS}


async def ta_appt_predict_noshow(session_id, ta_results, ctx) -> dict:
    appts = await cache.get_all_appointments()
    # appts = await hasura.appt_list_appointments()
    counts = _history_counts(appts)
    start, end = _goal_window(ctx.get("_goal", ""))

    def _in_window(t: datetime | None) -> bool:
        if not t:
            return False
        lo = start or _now()          # never predict for past appointments
        if t < lo:
            return False
        return end is None or t <= end

    upcoming = [a for a in appts if (a.get("status") or "").lower() == "scheduled"
                and _in_window(_parse(a.get("appointment_time")))]
    predictions = []
    for a in upcoming:
        pid = a.get("patient_id")
        priors = counts.get(pid, {"no_show": 0, "cancelled": 0})
        score = priors["no_show"] + priors["cancelled"]
        predictions.append({"appointment_id": a["id"], "patient_id": pid, "name": _name(a),
                            "risk": "high" if score >= _HIGH_RISK_PRIORS else ("medium" if score == 1 else "low"),
                            "prior_no_shows": priors["no_show"]})
    high = sum(1 for p in predictions if p["risk"] == "high")
    result = {"assessed": len(predictions), "high_risk_count": high, "predictions": predictions,
              "window": {"start": start.isoformat() if start else None,
                         "end": end.isoformat() if end else None}}
    logger.info("noshow predict  session=%s  assessed=%d  high=%d  window=%s..%s",
                session_id, len(predictions), high,
                start.isoformat() if start else "now", end.isoformat() if end else "open")
    return await _emit(session_id, "ta_appt_predict_noshow", result)


async def ta_appt_flag_high_risk(session_id, ta_results, ctx) -> dict:
    preds = (ta_results.get("ta_appt_predict_noshow") or {}).get("predictions") or []
    high = [p for p in preds if p["risk"] == "high"]
    for p in high:
        await _alert(session_id, "warning", f"High no-show risk: {p['name']} ({p['prior_no_shows']} prior no-shows) -- intervention triggered.")
    return await _emit(session_id, "ta_appt_flag_high_risk", {"flagged": len(high)})


async def ta_appt_proactive_engagement(session_id, ta_results, ctx) -> dict:
    preds = (ta_results.get("ta_appt_predict_noshow") or {}).get("predictions") or []
    targets = [p for p in preds if p["risk"] in ("high", "medium")]
    for p in targets:
        await broadcast(session_id, {"type": "alert", "severity": "info",
                                     "message": f"Proactive outreach to {p['name']} -- confirm attendance / offer assistance."})
    return await _emit(session_id, "ta_appt_proactive_engagement", {"engaged": len(targets)})


async def ta_appt_waitlist_replacement(session_id, ta_results, ctx) -> dict:
    high = (ta_results.get("ta_appt_predict_noshow") or {}).get("high_risk_count", 0)
    appts = await cache.get_all_appointments()
    # appts = await hasura.appt_list_appointments()
    pool = [a for a in appts if (a.get("status") or "").lower() == "cancelled"]
    prepared = min(high, len(pool))
    if prepared:
        await _alert(session_id, "info", f"{prepared} replacement candidate(s) prepared from cancellation pool for likely no-show slots.")
    return await _emit(session_id, "ta_appt_waitlist_replacement", {"replacements_prepared": prepared})


async def ta_appt_chronic_noshow(session_id, ta_results, ctx) -> dict:
    appts = await cache.get_all_appointments()
    # appts = await hasura.appt_list_appointments()
    counts = _history_counts(appts)
    name_by_pid = {a.get("patient_id"): _name(a) for a in appts}
    chronic = [{"patient_id": pid, "name": name_by_pid.get(pid, "Unknown"), "no_shows": c["no_show"]}
               for pid, c in counts.items() if c["no_show"] >= _CHRONIC_THRESHOLD]
    for c in chronic:
        await _alert(session_id, "warning", f"Chronic no-show flagged: {c['name']} ({c['no_shows']} no-shows) -- barrier review + care-management outreach.")
    return await _emit(session_id, "ta_appt_chronic_noshow",
                       {"chronic_count": len(chronic), "chronic_patients": chronic})


async def ta_appt_flag_preop_noshows(session_id, ta_results, ctx) -> dict:
    """G31: identify tomorrow's OT cases at risk of proceeding without a completed pre-op.

    There is no 'Pre-op' appointment type or appointment->surgery link in the data, so we
    APPROXIMATE 'lost their pre-op' as: a surgery scheduled tomorrow whose patient has one
    or more no-show appointments on record. Read-only -- flags the cases and alerts the OT
    team to verify/expedite the pre-op assessment (or reschedule) before the surgery."""
    appts     = await cache.get_all_appointments()
    surgeries = await cache.get_all_ot_schedule()
    tomorrow  = (_now() + timedelta(days=1)).date().isoformat()

    tom_cases = [s for s in surgeries
                 if str(s.get("scheduled_date")) == tomorrow
                 and (s.get("status") or "").lower() not in ("cancelled", "completed")]

    noshow_counts: dict = {}
    for a in appts:
        if (a.get("status") or "").lower() == "no show" and a.get("patient_id"):
            noshow_counts[a["patient_id"]] = noshow_counts.get(a["patient_id"], 0) + 1

    at_risk = []
    for s in tom_cases:
        n = noshow_counts.get(s.get("patient_id"), 0)
        if n > 0:
            at_risk.append({
                "surgery_id":   s.get("id"), "surgery_code": s.get("surgery_code"),
                "surgery_name": s.get("surgery_name"),
                "patient_id":   s.get("patient_id"), "patient_name": s.get("patient_name"),
                "scheduled_start_time": s.get("scheduled_start_time"),
                "room_code":    s.get("room_code"),
                "no_show_count": n,
            })

    for c in at_risk:
        await _alert(session_id, "warning",
                     f"Pre-op risk: {c['surgery_name'] or c['surgery_code']} (tomorrow {c.get('scheduled_start_time') or ''}, "
                     f"{c.get('room_code') or 'OT'}) -- patient has {c['no_show_count']} no-show appointment(s); "
                     f"verify the pre-op assessment before proceeding.")

    result = {"at_risk_count": len(at_risk), "tomorrow_case_count": len(tom_cases),
              "at_risk_cases": at_risk}
    logger.info("preop no-show flag  session=%s  tomorrow_cases=%d  at_risk=%d",
                session_id, len(tom_cases), len(at_risk))
    return await _emit(session_id, "ta_appt_flag_preop_noshows", result)


APPOINTMENT_TASKS = {
    "ta_appt_find_available_slots":      ta_appt_find_available_slots,
    "ta_appt_match_specialty":           ta_appt_match_specialty,
    "ta_appt_match_waitlist":            ta_appt_match_waitlist,
    "ta_appt_reserve_slot":              ta_appt_reserve_slot,
    "ta_appt_confirm_booking":           ta_appt_confirm_booking,  # post-approval only (not in registry)
    "ta_appt_prioritize_urgent":         ta_appt_prioritize_urgent,
    "ta_appt_coordinate_multispecialty": ta_appt_coordinate_multispecialty,
    "ta_appt_fill_cancellation":         ta_appt_fill_cancellation,
    "ta_appt_classify_movable":          ta_appt_classify_movable,
    "ta_appt_reschedule":                ta_appt_reschedule,
    "ta_appt_confirm_reschedule":        ta_appt_confirm_reschedule,  # post-approval only (not in registry)
    "ta_appt_find_service_slots":        ta_appt_find_service_slots,
    "ta_appt_book_service_slot":         ta_appt_book_service_slot,
    "ta_appt_confirm_service_booking":   ta_appt_confirm_service_booking,  # post-approval only (not in registry)
    "ta_appt_get_due_reminders":         ta_appt_get_due_reminders,
    "ta_appt_resolve_channel":           ta_appt_resolve_channel,
    "ta_appt_send_reminders":            ta_appt_send_reminders,
    "ta_appt_prep_instructions":         ta_appt_prep_instructions,
    "ta_appt_followup_reminders":        ta_appt_followup_reminders,
    "ta_appt_escalate_reminder":         ta_appt_escalate_reminder,
    "ta_appt_predict_noshow":            ta_appt_predict_noshow,
    "ta_appt_flag_high_risk":            ta_appt_flag_high_risk,
    "ta_appt_proactive_engagement":      ta_appt_proactive_engagement,
    "ta_appt_waitlist_replacement":      ta_appt_waitlist_replacement,
    "ta_appt_chronic_noshow":            ta_appt_chronic_noshow,
    "ta_appt_flag_preop_noshows":        ta_appt_flag_preop_noshows,
}
