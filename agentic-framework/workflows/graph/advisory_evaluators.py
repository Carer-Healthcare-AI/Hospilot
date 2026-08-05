"""Advisory rule evaluators -- the pluggable half of the advisory engine.

An evaluator answers one question against live data: does this rule's condition
hold right now? The engine (workflows/graph/advisory.py) decides WHEN to ask
(Kafka change events and/or a clock cadence, per the rule row) and what to do
with the answer (cooldown gate -> insert an advisories row).

How to add a rule (full guide: docs/agentic-framework/ADVISORY_ENGINE.md):

  1. Write an evaluator here:
         async def eval_my_rule(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
             ...
             return fired, detail, data
     - fired: condition holds
     - detail: one human sentence used as the advisory body
     - data:   small evidence snapshot (counts, ids) stored as jsonb
     Rules of the road: deterministic async I/O reads only (hasura.get_*,
     cache.*, util.forecast_client.forecast) -- no LLM calls, no writes.
     Thresholds come from `params` (operator-editable in the DB); always
     default them. A missing/down data source means NOT fired (forecast()
     already returns None for this), never an exception.

  2. Register it: EVALUATORS["my_rule_key"] = eval_my_rule

  3. Seed the rule row in its own numbered migration (ON CONFLICT (rule_key)
     DO NOTHING) choosing the trigger: `trigger_entities` for change-driven
     conditions, `check_interval_seconds` for time-driven ones (SLA timeouts,
     forecasts), or both.
"""

import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from cache import redis as cache
from db.hasura import hasura
from util.forecast_client import forecast

# (fired, detail, data) -- see module docstring
Evaluator = Callable[[str | None, dict], Awaitable[tuple[bool, str, dict]]]

EVALUATORS: dict[str, Evaluator] = {}


def _bed_status_is(bed: dict, status: str) -> bool:
    return str(bed.get("status") or "").lower() == status


# ── Bed Management ────────────────────────────────────────────────────────────


def _hours_to_period(hours: int) -> str:
    """Map an hour horizon to the /bed/turnover forecast_period enum."""
    if hours <= 3:
        return "3h"
    if hours <= 6:
        return "6h"
    if hours <= 12:
        return "12h"
    if hours <= 24:
        return "24h"
    if hours <= 72:
        return "3d"
    return "7d"


async def _beds_freeing_ml(period: str = "24h") -> int | None:
    """Beds predicted to free over `period`, summed over wards via the ML
    /bed/turnover model (payload mirrors agents/bed/prediction_activities.
    forecast_bed_turnover -- the redesigned per-ward census contract). None when
    the service is unconfigured/down for every ward -- callers fall back to the DB
    discharge horizon."""
    from agents.bed.prediction_activities import (
        _AVG_CLEANING_TIME_MIN, _clamp, _classify_ward)

    beds, dirty, dr_now, horizon_8h = await asyncio.gather(
        hasura.get_enriched_beds(), hasura.get_dirty_beds(),
        hasura.get_discharge_ready_count(), hasura.get_discharge_horizon(8))
    beds = beds or []
    if not beds:
        return None

    total_by_ward: dict[str, int] = {}
    occ_by_ward: dict[str, int] = {}
    for b in beds:
        if not b.get("is_active", True):
            continue
        wt = _classify_ward(b.get("ward") or b.get("type") or "")
        total_by_ward[wt] = total_by_ward.get(wt, 0) + 1
        if str(b.get("status") or "").lower() == "occupied":
            occ_by_ward[wt] = occ_by_ward.get(wt, 0) + 1
    clean_by_ward: dict[str, int] = {}
    for b in (dirty or []):
        wt = _classify_ward(b.get("ward") or b.get("type") or "")
        clean_by_ward[wt] = clean_by_ward.get(wt, 0) + 1

    total_occ = sum(occ_by_ward.values())
    freeing, got_any = 0, False
    for wt in sorted(set(total_by_ward) | set(occ_by_ward) | set(clean_by_ward)):
        occ = occ_by_ward.get(wt, 0)
        cleaning = clean_by_ward.get(wt, 0)
        total = max(total_by_ward.get(wt, 0), occ + cleaning)
        share = (occ / total_occ) if total_occ else 0.0
        resp = await forecast("/bed/turnover", {
            "forecast_period":           period,
            "ward_type":                 wt,
            "occupied_beds":             int(_clamp(occ, 0, 500)),
            "total_beds":                int(_clamp(total, occ, 500)),
            "beds_being_cleaned":        int(_clamp(cleaning, 0, 50)),
            "expected_discharges_today": round(_clamp(horizon_8h * share, 0, 100), 2),
            "planned_admissions_today":  0,
            "recent_discharges_4h":      int(_clamp(dr_now * share, 0, 50)),
            "avg_cleaning_minutes":      _AVG_CLEANING_TIME_MIN,
        })
        if resp is None:
            continue
        preds = resp.get("prediction") if isinstance(resp, dict) else None
        pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else resp) or {}
        val = next((pred[k] for k in ("beds_available_next_shift", "predicted_free_beds_next_shift",
                                      "predicted_free_beds", "beds_available",
                                      "predicted_beds_available", "value") if k in pred), None)
        if val is not None:
            freeing += int(val)
            got_any = True
    return freeing if got_any else None


async def eval_bed_occupancy_forecast(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Predicted occupancy over the horizon: current occupied + expected ER
    admissions - beds freeing (ML forecast, else DB discharge horizon)."""
    summary = await hasura.get_beds_summary() or {}
    total = int(summary.get("total_beds") or 0)
    if total <= 0:
        return False, "no bed data", {}
    occupied = int(summary.get("occupied_beds") or 0)
    pressure = await hasura.get_er_pressure() or {}
    est_admissions = int(pressure.get("est_admissions") or 0)
    horizon = int(params.get("horizon_hours", 6))
    threshold = float(params.get("predicted_occupancy_pct_threshold", 95))

    try:
        freeing, source = await _beds_freeing_ml(_hours_to_period(horizon)), "ml_forecast"
    except Exception:  # noqa: BLE001 -- ML path is best-effort, horizon is the fallback
        freeing = None
    if freeing is None:
        freeing, source = await hasura.get_discharge_horizon(horizon), "discharge_horizon"
    freeing = int(freeing or 0)
    predicted_pct = (occupied + est_admissions - freeing) / total * 100
    fired = predicted_pct > threshold
    detail = (f"Predicted bed occupancy {predicted_pct:.0f}% within {horizon}h "
              f"(now {occupied}/{total}, +{est_admissions} expected ER admissions, "
              f"-{freeing} beds freeing via {source}), threshold {threshold:.0f}%")
    return fired, detail, {"predicted_pct": round(predicted_pct, 1), "threshold": threshold,
                           "occupied": occupied, "total": total, "est_admissions": est_admissions,
                           "beds_freeing": freeing, "freeing_source": source,
                           "horizon_hours": horizon}


async def eval_er_boarding_pressure(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Admitted patients boarding in the ER awaiting an inpatient bed exceed the
    threshold (params.max_boarders). Same Redis source as agents/er check_er_boarders."""
    visits = await cache.get_many("er_visit:*") or []
    boarders = [v for v in visits if isinstance(v, dict) and v.get("status") == "boarded"]
    max_boarders = int(params.get("max_boarders", 5))
    fired = len(boarders) > max_boarders
    detail = f"{len(boarders)} ER patients boarding for inpatient beds (threshold {max_boarders})"
    return fired, detail, {"boarders": len(boarders), "threshold": max_boarders,
                           "visits": [{"visit_id": v.get("visit_id") or v.get("id"),
                                       "boarding_minutes": v.get("boarding_minutes")}
                                      for v in boarders[:20]]}


def _is_isolation_bed(bed: dict) -> bool:
    room_type = str(bed.get("room_type") or "").lower()
    features = " ".join(str(f).lower() for f in (bed.get("features") or []))
    return ("isolation" in room_type or "negative_pressure" in room_type
            or "isolation" in features)


async def eval_isolation_beds_full(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Available isolation beds have dropped below the floor
    (params.min_available_isolation_beds; 1 = alert when none are free)."""
    beds = await hasura.get_enriched_beds() or []
    iso = [b for b in beds if _is_isolation_bed(b)]
    available = [b for b in iso if _bed_status_is(b, "available")]
    min_available = int(params.get("min_available_isolation_beds", 1))
    fired = bool(iso) and len(available) < min_available
    detail = (f"{len(available)}/{len(iso)} isolation beds available "
              f"(alert below {min_available})")
    return fired, detail, {"isolation_total": len(iso), "isolation_available": len(available),
                           "min_available": min_available}


async def eval_discharged_bed_blocked(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Discharge-ready patients whose beds are still occupied
    (params.min_blocked_beds)."""
    discharged, beds = await asyncio.gather(
        hasura.get_recently_discharged_beds(), hasura.get_enriched_beds())
    occupied_ids = {b.get("id") for b in (beds or []) if _bed_status_is(b, "occupied")}
    blocked = [d for d in (discharged or []) if d.get("id") in occupied_ids]
    min_blocked = int(params.get("min_blocked_beds", 1))
    fired = len(blocked) >= min_blocked
    detail = f"{len(blocked)} discharged patient(s) still occupying beds"
    return fired, detail, {"blocked": len(blocked), "threshold": min_blocked,
                           "beds": [{"bed_id": d.get("id"), "admission_id": d.get("admission_id")}
                                    for d in blocked[:20]]}


# Dirty-since tracking for the turnaround SLA: Fabric carries no cleaning-start
# timestamp, so first-seen is tracked in memory -- ages reset on API restart.
_DIRTY_SINCE: dict[tuple[str | None, str], float] = {}


async def eval_bed_turnaround_sla(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Beds stuck in cleaning longer than the SLA (params.sla_minutes)."""
    dirty = await hasura.get_dirty_beds() or []
    sla_minutes = float(params.get("sla_minutes", 90))
    now = time.time()
    current: set[tuple[str | None, str]] = set()
    overdue = []
    for b in dirty:
        bed_id = b.get("id")
        if not bed_id:
            continue
        key = (org_id, bed_id)
        current.add(key)
        age_min = (now - _DIRTY_SINCE.setdefault(key, now)) / 60
        if age_min > sla_minutes:
            overdue.append({"bed_id": bed_id, "ward": b.get("ward"),
                            "minutes_in_cleaning": round(age_min)})
    for key in [k for k in _DIRTY_SINCE if k[0] == org_id and k not in current]:
        del _DIRTY_SINCE[key]
    fired = bool(overdue)
    detail = (f"{len(overdue)} bed(s) in cleaning past the {sla_minutes:.0f}-min SLA"
              if overdue else
              f"{len(dirty)} bed(s) in cleaning, none past the {sla_minutes:.0f}-min SLA")
    return fired, detail, {"overdue": overdue[:20], "sla_minutes": sla_minutes,
                           "dirty_count": len(dirty)}


# ── OT (operating theatres) ───────────────────────────────────────────────────
# OT data reaches us only through the Redis projection (Kafka hospilot.data.ot_*
# / cold-start sync) -- rows are raw HIS pass-throughs: nulls are literal "NULL"
# strings, numbers are strings, and stale rows keep status "Scheduled" with past
# dates, so every time rule scopes to today's schedule. Times are HIS-local
# naive; compared against naive datetime.now().

def _ot_val(row: dict, key: str):
    v = row.get(key)
    return None if v in (None, "", "NULL", "null") else v


def _ot_dt(row: dict, time_key: str, date_key: str = "scheduled_date") -> datetime | None:
    d, t = _ot_val(row, date_key), _ot_val(row, time_key)
    if not d or not t:
        return None
    try:
        return datetime.fromisoformat(f"{d} {t}")
    except ValueError:
        return None


def _is_today(row: dict, now: datetime) -> bool:
    return _ot_val(row, "scheduled_date") == now.strftime("%Y-%m-%d")


async def _ot_surgeries_today(now: datetime) -> list[dict]:
    rows = await cache.get_many("ot_surgery:*") or []
    return [r for r in rows if isinstance(r, dict) and _is_today(r, now)]


async def eval_ot_first_case_delayed(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Each room's first case of the day hasn't started within params.delay_minutes
    of its scheduled start."""
    now = datetime.now()
    delay = float(params.get("delay_minutes", 15))
    first_by_room: dict[str, dict] = {}
    for s in await _ot_surgeries_today(now):
        room = _ot_val(s, "ot_room_id") or _ot_val(s, "room_code")
        start = _ot_dt(s, "scheduled_start_time")
        if not room or not start:
            continue
        cur = first_by_room.get(room)
        if cur is None or start < _ot_dt(cur, "scheduled_start_time"):
            first_by_room[room] = s
    delayed = []
    for room, s in first_by_room.items():
        started = _ot_val(s, "actual_start_time")
        start = _ot_dt(s, "scheduled_start_time")
        if (not started and str(_ot_val(s, "status") or "").lower() == "scheduled"
                and now > start + timedelta(minutes=delay)):
            delayed.append({"room": room, "surgery_code": _ot_val(s, "surgery_code"),
                            "scheduled_start": str(start),
                            "minutes_late": round((now - start).total_seconds() / 60)})
    fired = bool(delayed)
    detail = (f"{len(delayed)} first case(s) of the day not started "
              f">{delay:.0f} min past schedule" if delayed else
              "all first cases started on time")
    return fired, detail, {"delayed": delayed[:20], "delay_minutes": delay}


async def eval_ot_surgery_overrun(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """In-progress surgeries running past scheduled end (or actual start +
    estimated duration) by more than params.overrun_minutes."""
    now = datetime.now()
    overrun_min = float(params.get("overrun_minutes", 30))
    overruns = []
    for s in await _ot_surgeries_today(now):
        if str(_ot_val(s, "status") or "").lower() not in ("in progress", "in-progress"):
            continue
        end = _ot_dt(s, "scheduled_end_time")
        if end is None:
            started = _ot_dt(s, "actual_start_time")
            est = _ot_val(s, "estimated_duration_minutes")
            if started and est:
                end = started + timedelta(minutes=float(est))
        if end and now > end + timedelta(minutes=overrun_min):
            overruns.append({"surgery_code": _ot_val(s, "surgery_code"),
                             "room": _ot_val(s, "ot_room_id"),
                             "minutes_over": round((now - end).total_seconds() / 60)})
    fired = bool(overruns)
    detail = (f"{len(overruns)} surgery(ies) overrunning schedule by "
              f">{overrun_min:.0f} min" if overruns else "no overruns")
    return fired, detail, {"overruns": overruns[:20], "overrun_minutes": overrun_min}


# Idle-since tracking mirrors _DIRTY_SINCE: room availability has no timestamp in
# the feed, so first-seen is in-memory and resets on API restart.
_OT_IDLE_SINCE: dict[tuple[str | None, str], float] = {}


async def eval_ot_room_idle(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Active rooms sitting Available for > params.idle_minutes while today's
    schedule still has unstarted cases for them (advance-the-next-case nudge)."""
    now = datetime.now()
    idle_min = float(params.get("idle_minutes", 60))
    statuses = await cache.get_many("ot_room_status:*") or []
    pending_rooms = set()
    for s in await _ot_surgeries_today(now):
        if str(_ot_val(s, "status") or "").lower() == "scheduled" and not _ot_val(s, "actual_start_time"):
            room = _ot_val(s, "ot_room_id") or _ot_val(s, "room_code")
            if room:
                pending_rooms.add(room)
    ts = time.time()
    current: set[tuple[str | None, str]] = set()
    idle = []
    for r in statuses:
        if not isinstance(r, dict) or str(_ot_val(r, "status") or "").lower() != "available":
            continue
        room = _ot_val(r, "id") or _ot_val(r, "room_code")
        if not room:
            continue
        key = (org_id, room)
        current.add(key)
        age_min = (ts - _OT_IDLE_SINCE.setdefault(key, ts)) / 60
        has_pending = room in pending_rooms or _ot_val(r, "room_code") in pending_rooms
        if age_min > idle_min and has_pending:
            idle.append({"room_code": _ot_val(r, "room_code"),
                         "idle_minutes": round(age_min),
                         "next_case": _ot_val(r, "current_surgery_name")})
    for key in [k for k in _OT_IDLE_SINCE if k[0] == org_id and k not in current]:
        del _OT_IDLE_SINCE[key]
    fired = bool(idle)
    detail = (f"{len(idle)} OT(s) idle >{idle_min:.0f} min with cases still waiting"
              if idle else "no idle theatres with pending cases")
    return fired, detail, {"idle": idle[:20], "idle_minutes": idle_min}


async def eval_ot_emergency_waiting(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Emergency-priority cases waiting to start (params.emergency_priorities;
    current HIS data only uses Elective/Non Elective -- add 'Non Elective' to the
    list if that is this org's emergency semantics)."""
    now = datetime.now()
    priorities = {str(p).lower() for p in params.get("emergency_priorities",
                                                     ["Emergency", "Urgent"])}
    min_waiting = int(params.get("min_waiting", 1))
    waiting = []
    for s in await _ot_surgeries_today(now):
        if (str(_ot_val(s, "priority") or "").lower() in priorities
                and str(_ot_val(s, "status") or "").lower() == "scheduled"
                and not _ot_val(s, "actual_start_time")):
            waiting.append({"surgery_code": _ot_val(s, "surgery_code"),
                            "surgery_name": _ot_val(s, "surgery_name"),
                            "room": _ot_val(s, "ot_room_id"),
                            "scheduled_start": str(_ot_dt(s, "scheduled_start_time"))})
    fired = len(waiting) >= min_waiting
    detail = f"{len(waiting)} emergency surgery(ies) waiting for a theatre"
    return fired, detail, {"waiting": waiting[:20], "threshold": min_waiting}


async def eval_ot_icu_capacity_post_surgery(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Upcoming ICU-prone surgeries (params.icu_surgery_types -- the HIS carries no
    needs-ICU flag) vs free ICU beds: fire when placing them all would leave fewer
    than params.min_free_icu_beds free within params.lookahead_hours."""
    now = datetime.now()
    lookahead = float(params.get("lookahead_hours", 4))
    min_free = int(params.get("min_free_icu_beds", 1))
    icu_types = {str(t).lower() for t in params.get("icu_surgery_types",
                                                    ["Cardiac", "Neuro", "Transplant"])}
    horizon = now + timedelta(hours=lookahead)
    upcoming = []
    for s in await _ot_surgeries_today(now):
        start = _ot_dt(s, "scheduled_start_time")
        if (start and now <= start <= horizon
                and str(_ot_val(s, "surgery_type") or "").lower() in icu_types
                and str(_ot_val(s, "status") or "").lower() in ("scheduled", "in progress", "in-progress")):
            upcoming.append({"surgery_code": _ot_val(s, "surgery_code"),
                             "surgery_type": _ot_val(s, "surgery_type"),
                             "scheduled_start": str(start)})
    summary = await hasura.get_beds_summary() or {}
    icu_free = int(summary.get("icu_available") or 0)
    fired = bool(upcoming) and (icu_free - len(upcoming)) < min_free
    detail = (f"{len(upcoming)} ICU-bound surgery(ies) within {lookahead:.0f}h "
              f"vs {icu_free} free ICU beds (keep {min_free} free)")
    return fired, detail, {"upcoming": upcoming[:20], "icu_available": icu_free,
                           "min_free_icu_beds": min_free, "lookahead_hours": lookahead}


async def eval_ot_equipment_unavailable(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Theatres out of service (status Maintenance) that still have cases on
    today's schedule. Proxy: the equipment-usage feed is empty in the current
    integration, so room maintenance is the observable equipment failure."""
    now = datetime.now()
    min_affected = int(params.get("min_affected", 1))
    statuses = await cache.get_many("ot_room_status:*") or []
    maint = {}
    for r in statuses:
        if isinstance(r, dict) and str(_ot_val(r, "status") or "").lower() == "maintenance":
            for k in (_ot_val(r, "id"), _ot_val(r, "room_code")):
                if k:
                    maint[k] = _ot_val(r, "room_code")
    affected = []
    if maint:
        for s in await _ot_surgeries_today(now):
            room = _ot_val(s, "ot_room_id") or _ot_val(s, "room_code")
            if room in maint and str(_ot_val(s, "status") or "").lower() == "scheduled":
                affected.append({"surgery_code": _ot_val(s, "surgery_code"),
                                 "room_code": maint[room],
                                 "scheduled_start": str(_ot_dt(s, "scheduled_start_time"))})
    fired = len(affected) >= min_affected
    detail = (f"{len(affected)} scheduled case(s) in theatre(s) under maintenance"
              if affected else
              f"{len(set(maint.values()))} theatre(s) in maintenance, none with cases today")
    return fired, detail, {"affected": affected[:20], "rooms_in_maintenance": sorted(set(maint.values())),
                           "threshold": min_affected}


# ── Discharge ─────────────────────────────────────────────────────────────────
# Sources are the Redis projections: admission:* (discharge_ready is "t"/"f" in
# sync rows, real booleans via the change feed -- use _truthy), invoice:* (synced
# Unpaid/Partial only, admission_id link), claim:* (patient_token link,
# submitted_date), pharmacy_order:* (patient_token link), discharge_summary:*
# (admission_id link). Invoices/claims have no Kafka topic -- clock-fresh only.

def _truthy(v) -> bool:
    return v in (True, "t", "true", "T", "True", 1, "1")


async def _discharge_ready_admissions() -> list[dict]:
    rows = await cache.get_many("admission:*") or []
    return [a for a in rows if isinstance(a, dict) and _truthy(a.get("discharge_ready"))]


# Ready-since tracking mirrors _DIRTY_SINCE: no ready-since timestamp in the data.
_DR_SINCE: dict[tuple[str | None, str], float] = {}


async def eval_discharge_fit_pending(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Patients flagged medically fit (discharge_ready) still admitted past the
    grace window (params.grace_minutes -- in-memory clock, no ready-since
    timestamp in the data; resets on restart)."""
    ready = await _discharge_ready_admissions()
    grace = float(params.get("grace_minutes", 60))
    min_pending = int(params.get("min_pending", 1))
    ts = time.time()
    current: set[tuple[str | None, str]] = set()
    pending = []
    for a in ready:
        adm_id = a.get("id")
        if not adm_id:
            continue
        key = (org_id, adm_id)
        current.add(key)
        age_min = (ts - _DR_SINCE.setdefault(key, ts)) / 60
        if age_min > grace:
            pending.append({"admission_id": adm_id, "bed_id": a.get("bed_id"),
                            "blocked_reason": a.get("discharge_blocked_reason"),
                            "minutes_pending": round(age_min)})
    for key in [k for k in _DR_SINCE if k[0] == org_id and k not in current]:
        del _DR_SINCE[key]
    fired = len(pending) >= min_pending
    detail = (f"{len(pending)} medically fit patient(s) still admitted "
              f">{grace:.0f} min after being flagged discharge-ready")
    return fired, detail, {"pending": pending[:20], "grace_minutes": grace,
                           "threshold": min_pending}


async def eval_discharge_billing_pending(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Discharge-ready patients with unpaid/partial invoices blocking them."""
    ready, invoices = await asyncio.gather(
        _discharge_ready_admissions(), cache.get_many("invoice:*"))
    ready_ids = {a.get("id") for a in ready}
    min_pending = int(params.get("min_pending", 1))
    blocked = []
    for inv in (invoices or []):
        if (isinstance(inv, dict) and inv.get("admission_id") in ready_ids
                and str(inv.get("payment_status") or "").lower() in ("unpaid", "partial")):
            blocked.append({"admission_id": inv.get("admission_id"),
                            "invoice_number": inv.get("invoice_number"),
                            "balance": inv.get("balance"),
                            "payment_status": inv.get("payment_status")})
    fired = len(blocked) >= min_pending
    detail = f"{len(blocked)} discharge-ready patient invoice(s) awaiting payment"
    return fired, detail, {"blocked": blocked[:20], "threshold": min_pending}


async def eval_discharge_pharmacy_pending(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Discharge-ready patients with medication orders not yet prepared
    (params.pending_statuses)."""
    ready, orders = await asyncio.gather(
        _discharge_ready_admissions(), cache.get_many("pharmacy_order:*"))
    tokens = {a.get("patient_token") for a in ready if a.get("patient_token")}
    statuses = {str(s).lower() for s in params.get("pending_statuses", ["pending", "on_hold"])}
    min_pending = int(params.get("min_pending", 1))
    pending = []
    for o in (orders or []):
        if (isinstance(o, dict) and o.get("patient_token") in tokens
                and str(o.get("status") or "").lower() in statuses):
            pending.append({"order_id": o.get("order_id") or o.get("id"),
                            "medication": o.get("medication_name"),
                            "status": o.get("status")})
    fired = len(pending) >= min_pending
    detail = f"{len(pending)} discharge medication order(s) not yet prepared"
    return fired, detail, {"pending": pending[:20], "threshold": min_pending}


async def eval_discharge_summary_pending(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Discharge-ready patients with no discharge summary on file."""
    ready, summaries = await asyncio.gather(
        _discharge_ready_admissions(), cache.get_many("discharge_summary:*"))
    summarized = {s.get("admission_id") for s in (summaries or []) if isinstance(s, dict)}
    min_pending = int(params.get("min_pending", 1))
    missing = [{"admission_id": a.get("id"), "bed_id": a.get("bed_id")}
               for a in ready if a.get("id") not in summarized]
    fired = len(missing) >= min_pending
    detail = f"{len(missing)} discharge-ready patient(s) missing a discharge summary"
    return fired, detail, {"missing": missing[:20], "threshold": min_pending}


async def eval_discharge_insurance_pending(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Discharge-ready patients whose insurance claims have been sitting in
    Submitted/Query for over params.pending_hours."""
    ready, claims = await asyncio.gather(
        _discharge_ready_admissions(), cache.get_many("claim:*"))
    tokens = {a.get("patient_token") for a in ready if a.get("patient_token")}
    pending_hours = float(params.get("pending_hours", 4))
    min_pending = int(params.get("min_pending", 1))
    now = datetime.now()
    stuck = []
    for c in (claims or []):
        if not (isinstance(c, dict) and c.get("patient_token") in tokens
                and str(c.get("status") or "").lower() in ("submitted", "query")):
            continue
        submitted = None
        raw = c.get("submitted_date") or c.get("created_at")
        if raw:
            try:
                submitted = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
            except ValueError:
                pass
        if submitted is None or now - submitted > timedelta(hours=pending_hours):
            stuck.append({"claim_number": c.get("claim_number"), "status": c.get("status"),
                          "tpa_name": c.get("tpa_name"), "submitted_date": str(raw)})
    fired = len(stuck) >= min_pending
    detail = (f"{len(stuck)} insurance claim(s) pending >{pending_hours:.0f}h "
              f"for discharge-ready patients")
    return fired, detail, {"stuck": stuck[:20], "pending_hours": pending_hours,
                           "threshold": min_pending}


async def eval_discharge_delayed(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Patients still admitted more than params.delay_hours past their
    expected_discharge_at."""
    rows = await cache.get_many("admission:*") or []
    delay_hours = float(params.get("delay_hours", 2))
    min_pending = int(params.get("min_pending", 1))
    now = datetime.now()
    delayed = []
    for a in rows:
        if not isinstance(a, dict):
            continue
        raw = a.get("expected_discharge_at")
        if not raw or str(raw).upper() == "NULL":
            continue
        try:
            expected = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            continue
        if now - expected > timedelta(hours=delay_hours):
            delayed.append({"admission_id": a.get("id"), "bed_id": a.get("bed_id"),
                            "expected_discharge_at": str(raw),
                            "hours_over": round((now - expected).total_seconds() / 3600, 1)})
    fired = len(delayed) >= min_pending
    detail = f"{len(delayed)} discharge(s) delayed >{delay_hours:.0f}h past expected time"
    return fired, detail, {"delayed": delayed[:20], "delay_hours": delay_hours,
                           "threshold": min_pending}


# ── Laboratory ────────────────────────────────────────────────────────────────
# Redis projections: lab:* (orders -- ordered_at/completed_at, status
# Ordered|Completed, priority mostly NULL), lab_result:* (flag Normal|Low|High
# today -- no Critical values and NO communicated/acknowledged field, so the
# critical rule fires on recent critical-flagged results and lets the cooldown
# nag), lab_sample:* (collection_status, lab_receipt_status, is_misplaced),
# lab_analyzer:* (status Online|...). Timestamps are UTC ("+00").

def _lab_ts(raw) -> datetime | None:
    """Parse a lab timestamp ('2026-06-09 11:07:49.936384+00') to naive UTC."""
    if raw is None or str(raw).upper() in ("NULL", "NONE", ""):
        return None
    s = str(raw).replace("Z", "+00:00")
    s = re.sub(r"([+-]\d{2})$", r"\1:00", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def eval_lab_tat_sla(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Pending lab orders older than the turnaround SLA (params.sla_minutes;
    params.stat_sla_minutes for priorities in params.stat_priorities)."""
    orders = await cache.get_many("lab:*") or []
    sla = float(params.get("sla_minutes", 120))
    stat_sla = float(params.get("stat_sla_minutes", 60))
    stat_priorities = {str(p).lower() for p in params.get("stat_priorities",
                                                          ["stat", "urgent", "asap"])}
    now = _utcnow()
    overdue = []
    for o in orders:
        if not isinstance(o, dict) or str(o.get("status") or "").lower() != "ordered":
            continue
        ordered = _lab_ts(o.get("ordered_at"))
        if not ordered:
            continue
        limit = stat_sla if str(o.get("priority") or "").lower() in stat_priorities else sla
        age_min = (now - ordered).total_seconds() / 60
        if age_min > limit:
            overdue.append({"order_id": o.get("id"), "priority": o.get("priority"),
                            "minutes_pending": round(age_min), "sla_minutes": limit})
    fired = bool(overdue)
    detail = f"{len(overdue)} lab order(s) past turnaround SLA"
    return fired, detail, {"overdue": overdue[:20], "sla_minutes": sla,
                           "stat_sla_minutes": stat_sla}


async def eval_lab_critical_result(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Critical-flagged results older than params.pending_minutes and younger
    than params.max_age_hours. No communicated/ack field exists, so the cooldown
    is the nag cadence; current data flags only Normal/Low/High -- add flags to
    params.critical_flags per org semantics."""
    results = await cache.get_many("lab_result:*") or []
    flags = {str(f).lower() for f in params.get("critical_flags",
                                                ["critical", "critical high", "critical low", "panic"])}
    pending_min = float(params.get("pending_minutes", 15))
    max_age_h = float(params.get("max_age_hours", 24))
    now = _utcnow()
    pending = []
    for r in results:
        if not isinstance(r, dict) or str(r.get("flag") or "").lower() not in flags:
            continue
        reported = _lab_ts(r.get("reported_at"))
        if not reported:
            continue
        age = now - reported
        if timedelta(minutes=pending_min) < age < timedelta(hours=max_age_h):
            pending.append({"test_name": r.get("test_name"), "flag": r.get("flag"),
                            "result_value": r.get("result_value"),
                            "order_id": r.get("order_id"),
                            "minutes_since_reported": round(age.total_seconds() / 60)})
    fired = len(pending) >= int(params.get("min_pending", 1))
    detail = f"{len(pending)} critical result(s) awaiting physician notification"
    return fired, detail, {"pending": pending[:20], "pending_minutes": pending_min}


async def eval_lab_analyzer_down(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Analyzers whose status is not in params.up_statuses."""
    analyzers = await cache.get_many("lab_analyzer:*") or []
    up = {str(s).lower() for s in params.get("up_statuses", ["Online"])}
    min_down = int(params.get("min_down", 1))
    down = [{"name": a.get("name"), "analyzer_type": a.get("analyzer_type"),
             "status": a.get("status"), "is_backup": a.get("is_backup")}
            for a in analyzers
            if isinstance(a, dict) and str(a.get("status") or "").lower() not in up]
    fired = len(down) >= min_down
    detail = f"{len(down)} analyzer(s) down ({', '.join(str(d['name']) for d in down[:5])})" \
        if down else "all analyzers online"
    return fired, detail, {"down": down[:20], "threshold": min_down,
                           "total_analyzers": len(analyzers)}


async def eval_lab_collection_delayed(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Pending orders older than params.delay_minutes with no collected sample."""
    orders, samples = await asyncio.gather(
        cache.get_many("lab:*"), cache.get_many("lab_sample:*"))
    delay = float(params.get("delay_minutes", 60))
    now = _utcnow()
    collected = {s.get("order_id") for s in (samples or [])
                 if isinstance(s, dict)
                 and (_lab_ts(s.get("collected_at"))
                      or str(s.get("collection_status") or "").lower() == "collected")}
    delayed = []
    for o in (orders or []):
        if not isinstance(o, dict) or str(o.get("status") or "").lower() != "ordered":
            continue
        ordered = _lab_ts(o.get("ordered_at"))
        if not ordered or o.get("id") in collected:
            continue
        age_min = (now - ordered).total_seconds() / 60
        if age_min > delay:
            delayed.append({"order_id": o.get("id"), "patient_token": o.get("patient_token"),
                            "minutes_waiting": round(age_min)})
    fired = len(delayed) >= int(params.get("min_pending", 1))
    detail = f"{len(delayed)} sample collection(s) delayed >{delay:.0f} min"
    return fired, detail, {"delayed": delayed[:20], "delay_minutes": delay}


async def eval_lab_sample_rejections(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Rejected/problem samples in the window exceed the threshold
    (params.rejected_statuses matched on receipt+collection status, plus
    misplaced samples)."""
    samples = await cache.get_many("lab_sample:*") or []
    rejected_statuses = {str(s).lower() for s in params.get("rejected_statuses",
                                                            ["Rejected", "Missing"])}
    max_rejections = int(params.get("max_rejections", 3))
    window_h = float(params.get("window_hours", 24))
    now = _utcnow()
    rejected = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        is_problem = (str(s.get("lab_receipt_status") or "").lower() in rejected_statuses
                      or str(s.get("collection_status") or "").lower() in rejected_statuses
                      or _truthy(s.get("is_misplaced")))
        if not is_problem:
            continue
        ts = (_lab_ts(s.get("received_at")) or _lab_ts(s.get("collected_at"))
              or _lab_ts(s.get("synced_at")))
        if ts and now - ts > timedelta(hours=window_h):
            continue
        rejected.append({"barcode": s.get("barcode"), "order_id": s.get("order_id"),
                         "receipt_status": s.get("lab_receipt_status"),
                         "misplaced": _truthy(s.get("is_misplaced"))})
    fired = len(rejected) > max_rejections
    detail = (f"{len(rejected)} rejected/problem sample(s) in the last "
              f"{window_h:.0f}h (threshold {max_rejections})")
    return fired, detail, {"rejected": rejected[:20], "threshold": max_rejections,
                           "window_hours": window_h}


# ── Revenue Cycle ─────────────────────────────────────────────────────────────
# Redis projections: invoice:* (sync pulls Unpaid/Partial only -- Drafts are
# included since they are unpaid; status Pending|Draft|Partially Paid, float
# amounts, ISO due_date), claim:* (status Submitted|Query|Paid|Denied,
# claim_amount/approved_amount, submitted_date), payment:*. No Kafka topics for
# financial data -> all rules are clock-driven, freshness = financial sync cadence.

def _num(value, default: float = 0.0) -> float:
    """Coerce Fabric/cache values (sometimes strings) to float; default on junk."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def eval_rc_claims_pending(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Claims sitting in submission states exceed the threshold
    (params.pending_statuses / params.max_pending)."""
    claims = await cache.get_many("claim:*") or []
    statuses = {str(s).lower() for s in params.get("pending_statuses", ["Submitted", "Query"])}
    max_pending = int(params.get("max_pending", 10))
    pending = [{"claim_number": c.get("claim_number"), "status": c.get("status"),
                "claim_amount": _num(c.get("claim_amount")), "tpa_name": c.get("tpa_name")}
               for c in claims
               if isinstance(c, dict) and str(c.get("status") or "").lower() in statuses]
    fired = len(pending) > max_pending
    detail = f"{len(pending)} insurance claim(s) pending submission/processing (threshold {max_pending})"
    return fired, detail, {"pending": pending[:20], "threshold": max_pending,
                           "total_amount": round(sum(p["claim_amount"] for p in pending), 2)}


async def eval_rc_claim_denial_spike(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Denied claims within the window exceed the threshold (params.max_denials
    over params.window_days)."""
    claims = await cache.get_many("claim:*") or []
    max_denials = int(params.get("max_denials", 3))
    window_d = float(params.get("window_days", 7))
    now = _utcnow()
    denied = []
    for c in claims:
        if not isinstance(c, dict) or str(c.get("status") or "").lower() != "denied":
            continue
        ts = _lab_ts(c.get("submitted_date")) or _lab_ts(c.get("created_at"))
        if ts and now - ts > timedelta(days=window_d):
            continue
        denied.append({"claim_number": c.get("claim_number"),
                       "denial_reason": c.get("denial_reason"),
                       "claim_amount": _num(c.get("claim_amount"))})
    fired = len(denied) > max_denials
    detail = f"{len(denied)} claim denial(s) in the last {window_d:.0f} days (threshold {max_denials})"
    return fired, detail, {"denied": denied[:20], "threshold": max_denials,
                           "window_days": window_d}


async def eval_rc_billing_backlog(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Draft (unfinalized) invoices exceed the workload threshold
    (params.max_draft_invoices)."""
    invoices = await cache.get_many("invoice:*") or []
    max_draft = int(params.get("max_draft_invoices", 15))
    drafts = [{"invoice_number": i.get("invoice_number"),
               "grand_total": _num(i.get("grand_total")),
               "invoice_date": i.get("invoice_date")}
              for i in invoices
              if isinstance(i, dict) and str(i.get("status") or "").lower() == "draft"]
    fired = len(drafts) > max_draft
    detail = f"{len(drafts)} draft invoice(s) awaiting billing (threshold {max_draft})"
    return fired, detail, {"drafts": drafts[:20], "threshold": max_draft,
                           "total_amount": round(sum(d["grand_total"] for d in drafts), 2)}


async def eval_rc_collections_overdue(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Outstanding balances on invoices past due (+grace) exceed
    params.max_overdue_amount -- proxy for collections-below-target (no revenue
    target exists in the data); the evidence list is the recovery list."""
    invoices = await cache.get_many("invoice:*") or []
    max_amount = _num(params.get("max_overdue_amount", 100000))
    grace_d = float(params.get("overdue_grace_days", 7))
    now = _utcnow()
    overdue = []
    for i in invoices:
        if not isinstance(i, dict):
            continue
        balance = _num(i.get("balance"))
        due = _lab_ts(i.get("due_date"))
        if balance <= 0 or not due or now <= due + timedelta(days=grace_d):
            continue
        overdue.append({"invoice_number": i.get("invoice_number"), "balance": balance,
                        "days_overdue": (now - due).days,
                        "patient_id": i.get("patient_id")})
    total = round(sum(o["balance"] for o in overdue), 2)
    fired = total > max_amount
    detail = (f"Overdue receivables at {total:,.0f} across {len(overdue)} invoice(s) "
              f"(threshold {max_amount:,.0f})")
    overdue.sort(key=lambda o: -o["balance"])
    return fired, detail, {"recovery_list": overdue[:20], "total_overdue": total,
                           "threshold": max_amount, "grace_days": grace_d}


async def eval_rc_revenue_leakage(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Underpayment gap (claim_amount - approved_amount) on settled claims within
    the window exceeds params.min_leakage_amount -- the measurable leakage channel;
    unbilled-service leakage is not detectable in this data."""
    claims = await cache.get_many("claim:*") or []
    min_leak = _num(params.get("min_leakage_amount", 50000))
    window_d = float(params.get("window_days", 30))
    settled = {str(s).lower() for s in params.get("settled_statuses", ["Paid", "Approved"])}
    now = _utcnow()
    gaps = []
    for c in claims:
        if not isinstance(c, dict) or str(c.get("status") or "").lower() not in settled:
            continue
        gap = _num(c.get("claim_amount")) - _num(c.get("approved_amount"))
        if gap <= 0:
            continue
        ts = _lab_ts(c.get("submitted_date")) or _lab_ts(c.get("created_at"))
        if ts and now - ts > timedelta(days=window_d):
            continue
        gaps.append({"claim_number": c.get("claim_number"), "gap": round(gap, 2),
                     "claim_amount": _num(c.get("claim_amount")),
                     "approved_amount": _num(c.get("approved_amount")),
                     "tpa_name": c.get("tpa_name")})
    total = round(sum(g["gap"] for g in gaps), 2)
    fired = total > min_leak
    detail = (f"Underpayment gap of {total:,.0f} across {len(gaps)} settled claim(s) "
              f"in {window_d:.0f} days (threshold {min_leak:,.0f})")
    gaps.sort(key=lambda g: -g["gap"])
    return fired, detail, {"gaps": gaps[:20], "total_gap": total, "threshold": min_leak,
                           "window_days": window_d}


# ── Emergency (ER) ────────────────────────────────────────────────────────────


# ── ICU ───────────────────────────────────────────────────────────────────────


async def eval_icu_predicted_full(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """ML forecast projects ICU census to meet/exceed capacity within the horizon."""
    from util.forecast_client import forecast
    s = await hasura.get_beds_summary() or {}
    total, occupied = int(_num(s.get("icu_total"))), int(_num(s.get("icu_occupied")))
    resp = await forecast("/icu/demand", {"icu_occupied": occupied, "icu_total": total})
    if not resp:  # service down/unconfigured -> not fired (never raise)
        return False, "", {}
    pred = resp.get("prediction") if isinstance(resp.get("prediction"), dict) else resp
    predicted = next((pred[k] for k in ("predicted_admissions_24h", "predicted_admissions",
                                        "predicted_demand", "value") if k in pred), None)
    if predicted is None:
        return False, "", {}
    projected = occupied + int(_num(predicted))
    fired = total > 0 and projected >= total
    detail = f"ICU projected to reach {projected}/{total} beds (+{int(_num(predicted))} predicted in 24h)"
    return fired, detail, {"predicted_admissions_24h": predicted, "icu_occupied": occupied,
                           "icu_total": total, "projected": projected}


EVALUATORS["icu_predicted_full"] = eval_icu_predicted_full


async def eval_icu_nurse_ratio_below_policy(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """ICU nurse load exceeds policy (patients-per-nurse too high). Reads staff_roster
    rows for the ICU area; not fired when no ICU nursing roster is present."""
    from cache import redis as cache
    area = str(params.get("roster_area", "icu")).lower()
    max_load = float(params.get("max_patients_per_nurse", 2))
    try:
        roster = await cache.get_all_staff_roster() or []
    except Exception:
        return False, "", {}
    rows = [r for r in roster if "nurse" in str(r.get("role", "")).lower()
            and area in (str(r.get("area", "")) + str(r.get("area_label", ""))).lower()]
    headcount = sum(_num(r.get("headcount")) for r in rows)
    load = sum(_num(r.get("assigned_load")) for r in rows)
    if not rows or headcount <= 0:  # no ICU nurse roster -> nothing to judge
        return False, "", {}
    ratio = load / headcount
    fired = ratio > max_load
    detail = (f"ICU nurse load {ratio:.1f} patients/nurse "
              f"({int(load)} patients / {int(headcount)} nurses), policy {max_load:g}")
    return fired, detail, {"patients_per_nurse": round(ratio, 2), "patients": int(load),
                           "nurses": int(headcount), "policy": max_load}


EVALUATORS["icu_nurse_ratio_below_policy"] = eval_icu_nurse_ratio_below_policy


# ── Staffing ──────────────────────────────────────────────────────────────────

_DEFAULT_ON_DUTY = ("on_duty", "on-duty", "present", "available", "working", "active", "duty")


def _on_duty(status, on_duty_values) -> bool:
    """True when a staff member's on_duty_status counts as present."""
    return str(status or "").strip().lower() in on_duty_values


async def eval_staffing_nurse_ratio_below_threshold(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Any nursing area's patients-per-nurse load exceeds the threshold."""
    from cache import redis as cache
    max_load = float(params.get("max_patients_per_nurse", 6))
    try:
        roster = await cache.get_all_staff_roster() or []
    except Exception:
        return False, "", {}
    rows = [r for r in roster if "nurse" in str(r.get("role", "")).lower()]
    if not rows:
        return False, "", {}
    worst = max(rows, key=lambda r: _num(r.get("load_per_staff")))
    ratio = _num(worst.get("load_per_staff"))
    fired = ratio > max_load
    detail = (f"Nurse ratio {ratio:g} patients/nurse in "
              f"{worst.get('area_label') or worst.get('area')} (threshold {max_load:g})")
    return fired, detail, {"worst_ratio": ratio, "area": worst.get("area_label") or worst.get("area"),
                           "threshold": max_load,
                           "areas_over": sum(1 for r in rows if _num(r.get("load_per_staff")) > max_load)}


EVALUATORS["staffing_nurse_ratio_below_threshold"] = eval_staffing_nurse_ratio_below_threshold


async def eval_staffing_overtime_above_limit(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Staff whose recorded overtime hours exceed the per-staff limit."""
    from cache import redis as cache
    limit = float(params.get("overtime_hours_limit", 12))
    min_staff = int(params.get("min_staff_over", 1))
    fields = params.get("overtime_fields", ["overtime_hours", "ot_hours", "overtime"])
    try:
        staff = await cache.get_all_staff() or []
    except Exception:
        return False, "", {}
    over = 0
    for s in staff:
        val = next((s[f] for f in fields if isinstance(s, dict) and f in s and s[f] is not None), None)
        if val is not None and _num(val) > limit:
            over += 1
    fired = over >= min_staff
    detail = f"{over} staff over the {limit:g}h overtime limit (alert at {min_staff}+)"
    return fired, detail, {"over_limit": over, "limit": limit, "min_staff": min_staff}


EVALUATORS["staffing_overtime_above_limit"] = eval_staffing_overtime_above_limit


async def eval_staffing_icu_shortage(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """ICU nursing understaffed vs policy (patients per ICU nurse over the limit)."""
    from cache import redis as cache
    area = str(params.get("roster_area", "icu")).lower()
    max_load = float(params.get("max_patients_per_nurse", 2))
    try:
        roster = await cache.get_all_staff_roster() or []
    except Exception:
        return False, "", {}
    rows = [r for r in roster if "nurse" in str(r.get("role", "")).lower()
            and area in (str(r.get("area", "")) + str(r.get("area_label", ""))).lower()]
    headcount = sum(_num(r.get("headcount")) for r in rows)
    if not rows or headcount <= 0:  # no ICU nurse roster -> nothing to judge
        return False, "", {}
    ratio = sum(_num(r.get("assigned_load")) for r in rows) / headcount
    fired = ratio > max_load
    detail = f"ICU nurse load {ratio:.1f} patients/nurse ({int(headcount)} nurses), policy {max_load:g}"
    return fired, detail, {"patients_per_nurse": round(ratio, 2), "nurses": int(headcount), "policy": max_load}


EVALUATORS["staffing_icu_shortage"] = eval_staffing_icu_shortage


# ── Ambulance ─────────────────────────────────────────────────────────────────

def _past_due(value) -> bool:
    """True when an ISO date/datetime string is in the past (else False)."""
    if not value:
        return False
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt < datetime.now(timezone.utc)


async def eval_ambulance_demand_surge_predicted(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """ML forecast predicts an emergency-demand surge (ER-surge model as the signal)."""
    from util.forecast_client import forecast
    from cache import redis as cache
    surge_levels = {s.lower() for s in params.get("surge_levels", ["surge", "elevated", "high"])}
    try:
        amb = await cache.get_all_ambulances() or []
    except Exception:
        amb = []
    prior = sum(1 for a in amb if a.get("emergency_type"))  # crude prior-hour volume proxy
    resp = await forecast("/forecast/er-surge", {"prior_hour_volume": prior})
    if not resp:  # service down/unconfigured -> not fired
        return False, "", {}
    rows = resp.get("prediction") or resp.get("forecast") or resp.get("hourly") or []
    surge_hours = [r for r in rows
                   if str(r.get("surge_level") or r.get("level") or "").lower() in surge_levels]
    peak = max((_num(next((r[k] for k in ("predicted_volume", "predicted_arrivals", "value")
                           if k in r), 0)) for r in rows), default=0.0)
    fired = bool(surge_hours)
    detail = f"Demand surge predicted in {len(surge_hours)} upcoming hour(s), peak ~{int(peak)} arrivals"
    return fired, detail, {"surge_hours": len(surge_hours), "peak_predicted": int(peak)}


EVALUATORS["ambulance_demand_surge_predicted"] = eval_ambulance_demand_surge_predicted


# ── Pharmacy ──────────────────────────────────────────────────────────────────

def _age_minutes(value) -> float | None:
    """Minutes since an ISO timestamp, or None if unparseable/absent."""
    if not value:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60


# ── Patient Flow ──────────────────────────────────────────────────────────────

def _reason_matches(reason, keywords) -> bool:
    """True when a discharge_blocked_reason contains any of the keywords."""
    r = str(reason or "").lower()
    return bool(r) and any(k.lower() in r for k in keywords)


async def eval_patient_readmission_risk(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Heuristic: patients currently admitted who also appear in recently-discharged
    (discharged then readmitted) -- a proxy for readmission risk (no risk model wired)."""
    min_readmissions = int(params.get("min_readmissions", 1))
    adm = await hasura.get_admissions_with_wards() or []
    discharged = await hasura.get_recently_discharged_beds() or []
    disch_tokens = {d.get("patient_token") for d in discharged if d.get("patient_token")}
    readmitted = [a for a in adm if a.get("patient_token") in disch_tokens]
    fired = len(readmitted) >= min_readmissions
    detail = f"{len(readmitted)} readmitted patient(s) identified (alert at {min_readmissions}+)"
    return fired, detail, {"readmitted": len(readmitted), "min_readmissions": min_readmissions,
                           "admissions": [{"admission_id": a.get("id"),
                                           "patient_token": a.get("patient_token")} for a in readmitted[:20]]}


EVALUATORS["patient_readmission_risk"] = eval_patient_readmission_risk


# ── Executive ─────────────────────────────────────────────────────────────────
# Meta-rules over the whole house: the same projections/DB reads the domain
# rules use, plus the advisories fire history (hasura.list_advisories_since).
# Executive-topic fires are excluded from the history counts so the meta-rules
# never feed on their own output. All clock-only.

_EXEC_TOPIC = "Executive"

# SLA-flavoured rule_keys counted by exec_sla_breaches (params.sla_rule_keys overrides)
_SLA_RULE_KEYS = ["bed_turnaround_sla", "lab_tat_sla", "lab_collection_delayed",
                  "ot_first_case_delayed", "discharge_delayed"]


async def _ward_census() -> dict[str, dict]:
    """Per-ward {total, occupied} from the enriched beds feed."""
    beds = await hasura.get_enriched_beds() or []
    wards: dict[str, dict] = {}
    for b in beds:
        w = wards.setdefault(str(b.get("ward") or b.get("type") or "Unassigned"),
                             {"total": 0, "occupied": 0})
        w["total"] += 1
        if _bed_status_is(b, "occupied"):
            w["occupied"] += 1
    return wards


async def eval_exec_stress_index(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Weighted composite of the domain pressure signals, scaled 0-100 (each
    component = value/norm capped at 1; no HIS stress index exists). Fires above
    params.stress_index_threshold; weights and norms are operator-editable."""
    threshold = float(params.get("stress_index_threshold", 70))
    weights = {"bed_occupancy": 0.35, "er_boarding": 0.25, "ot_backlog": 0.15,
               "discharge_delays": 0.15, "lab_tat": 0.10,
               **(params.get("weights") or {})}
    norms = {"bed_occupancy": 90, "er_boarding": 5, "ot_backlog": 2,
             "discharge_delays": 5, "lab_tat": 5,
             **(params.get("component_norms") or {})}
    emergency = {str(p).lower() for p in params.get("emergency_priorities",
                                                    ["Emergency", "Urgent"])}

    now = datetime.now()
    summary, visits, surgeries, admissions, lab_orders = await asyncio.gather(
        hasura.get_beds_summary(), cache.get_many("er_visit:*"),
        _ot_surgeries_today(now), cache.get_many("admission:*"),
        cache.get_many("lab:*"))
    utc = _utcnow()
    values = {
        "bed_occupancy": float((summary or {}).get("occupancy_pct") or 0),
        "er_boarding": sum(1 for v in (visits or [])
                           if isinstance(v, dict) and v.get("status") == "boarded"),
        "ot_backlog": sum(1 for s in surgeries
                          if str(_ot_val(s, "priority") or "").lower() in emergency
                          and str(_ot_val(s, "status") or "").lower() == "scheduled"
                          and not _ot_val(s, "actual_start_time")),
        "discharge_delays": sum(1 for a in (admissions or [])
                                if isinstance(a, dict)
                                and (exp := _lab_ts(a.get("expected_discharge_at")))
                                and utc > exp),
        # 120 = lab_tat_sla's default sla_minutes
        "lab_tat": sum(1 for o in (lab_orders or [])
                       if isinstance(o, dict)
                       and str(o.get("status") or "").lower() == "ordered"
                       and (ts := _lab_ts(o.get("ordered_at")))
                       and (utc - ts).total_seconds() / 60 > 120),
    }

    total_w = sum(_num(w) for w in weights.values()) or 1.0
    components = {}
    for k, v in values.items():
        norm = _num(norms.get(k)) or 1.0
        components[k] = {"value": round(v, 1), "norm": norm,
                         "weight": round(_num(weights.get(k)) / total_w, 3),
                         "load": round(min(1.0, v / norm), 3)}
    score = 100 * sum(c["weight"] * c["load"] for c in components.values())
    fired = score > threshold
    top = sorted(components.items(), key=lambda kv: -kv[1]["weight"] * kv[1]["load"])[:2]
    detail = (f"Hospital stress index {score:.0f} (threshold {threshold:.0f}); "
              "top drivers: "
              + ", ".join(f"{k} {c['value']:g}/{c['norm']:g}" for k, c in top))
    return fired, detail, {"stress_index": round(score, 1), "threshold": threshold,
                           "components": components}


async def eval_exec_sla_breaches(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """SLA-type advisories (params.sla_rule_keys) fired across departments within
    params.window_hours reach params.min_breaches -- systemic, not local."""
    window_h = float(params.get("window_hours", 4))
    min_breaches = int(params.get("min_breaches", 3))
    keys = {str(k) for k in params.get("sla_rule_keys", _SLA_RULE_KEYS)}
    since = (datetime.now(timezone.utc) - timedelta(hours=window_h)).isoformat()
    rows = await hasura.list_advisories_since(since, org_id=org_id) or []
    by_rule: dict[str, int] = {}
    for r in rows:
        if r.get("rule_key") in keys:
            by_rule[r["rule_key"]] = by_rule.get(r["rule_key"], 0) + 1
    total = sum(by_rule.values())
    fired = total >= min_breaches
    detail = (f"{total} SLA-breach advisory(ies) across {len(by_rule)} rule(s) "
              f"in the last {window_h:.0f}h (threshold {min_breaches})")
    return fired, detail, {"breaches": total, "by_rule": by_rule,
                           "window_hours": window_h, "threshold": min_breaches}


async def eval_exec_capacity_forecast(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Per-ward predicted occupancy -- current census plus ER admission pressure
    apportioned by occupancy share, minus the discharge horizon -- breaches
    params.predicted_occupancy_pct in at least params.min_wards_critical wards.
    Deterministic ward-level sibling of bed_occupancy_forecast_critical (which
    stays house-level + ML); ER pressure is not scaled to the horizon."""
    threshold = float(params.get("predicted_occupancy_pct", 90))
    horizon_h = int(params.get("horizon_hours", 24))
    min_wards = int(params.get("min_wards_critical", 2))
    min_beds = int(params.get("min_ward_beds", 5))
    wards, pressure, freeing = await asyncio.gather(
        _ward_census(), hasura.get_er_pressure(),
        hasura.get_discharge_horizon(horizon_h))
    est_admissions = int((pressure or {}).get("est_admissions", 0) or 0)
    freeing = int(freeing or 0)
    total_occ = sum(w["occupied"] for w in wards.values())
    critical = []
    for name, w in sorted(wards.items()):
        if w["total"] < min_beds:
            continue
        share = (w["occupied"] / total_occ) if total_occ else 0.0
        predicted = (w["occupied"] + (est_admissions - freeing) * share) / w["total"] * 100
        if predicted > threshold:
            critical.append({"ward": name, "predicted_pct": round(predicted, 1),
                             "occupied": w["occupied"], "total": w["total"]})
    fired = len(critical) >= min_wards
    detail = (f"{len(critical)} ward(s) predicted >{threshold:.0f}% occupancy within "
              f"{horizon_h}h (+{est_admissions} ER admissions, -{freeing} beds freeing; "
              f"alert at {min_wards})")
    return fired, detail, {"critical_wards": critical[:20], "threshold": threshold,
                           "horizon_hours": horizon_h, "est_admissions": est_admissions,
                           "beds_freeing": freeing}


async def eval_exec_kpi_deteriorating(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Per-topic advisory fire rate in the recent window vs the trailing baseline
    daily average (proxy KPI -- no KPI store exists). A topic needs
    params.min_baseline_fires in the baseline to count; fires when any topic runs
    params.min_pct_deterioration above its baseline-expected rate."""
    window_h = float(params.get("window_hours", 24))
    baseline_d = float(params.get("baseline_days", 7))
    min_pct = float(params.get("min_pct_deterioration", 25))
    min_base = int(params.get("min_baseline_fires", 5))
    now = _utcnow()
    since = (now - timedelta(days=baseline_d, hours=window_h)
             ).replace(tzinfo=timezone.utc).isoformat()
    rows = await hasura.list_advisories_since(since, org_id=org_id) or []
    cutoff = now - timedelta(hours=window_h)
    recent: dict[str, int] = {}
    baseline: dict[str, int] = {}
    for r in rows:
        topic, ts = r.get("topic"), _lab_ts(r.get("created_at"))
        if not topic or topic == _EXEC_TOPIC or not ts:
            continue
        bucket = recent if ts >= cutoff else baseline
        bucket[topic] = bucket.get(topic, 0) + 1
    worsening = []
    for topic, base_count in baseline.items():
        if base_count < min_base:
            continue
        expected = base_count / baseline_d * (window_h / 24)
        pct = (recent.get(topic, 0) - expected) / expected * 100
        if pct >= min_pct:
            worsening.append({"topic": topic, "recent_fires": recent.get(topic, 0),
                              "baseline_daily_avg": round(base_count / baseline_d, 2),
                              "pct_worse": round(pct)})
    worsening.sort(key=lambda w: -w["pct_worse"])
    fired = bool(worsening)
    detail = (f"{len(worsening)} topic(s) firing >={min_pct:.0f}% above their "
              f"{baseline_d:.0f}-day baseline"
              + (f" ({worsening[0]['topic']} +{worsening[0]['pct_worse']}%)"
                 if worsening else ""))
    return fired, detail, {"worsening": worsening[:10], "window_hours": window_h,
                           "baseline_days": baseline_d, "threshold_pct": min_pct}


async def eval_exec_utilization_imbalance(org_id: str | None, params: dict) -> tuple[bool, str, dict]:
    """Occupancy spread between the busiest and quietest wards exceeds
    params.max_occupancy_spread_pct (wards under params.min_ward_beds are
    ignored). Beds are the rebalanceable resource visible in the data; staff
    rosters are not integrated."""
    max_spread = float(params.get("max_occupancy_spread_pct", 30))
    min_beds = int(params.get("min_ward_beds", 5))
    min_wards = int(params.get("min_wards", 2))
    census = await _ward_census()
    wards = [{"ward": name, "occupancy_pct": round(w["occupied"] / w["total"] * 100, 1),
              "occupied": w["occupied"], "total": w["total"]}
             for name, w in census.items() if w["total"] >= min_beds]
    if len(wards) < min_wards:
        return False, f"only {len(wards)} ward(s) large enough to compare", {"wards": wards}
    wards.sort(key=lambda w: -w["occupancy_pct"])
    hi, lo = wards[0], wards[-1]
    spread = hi["occupancy_pct"] - lo["occupancy_pct"]
    fired = spread > max_spread
    detail = (f"Ward occupancy spread {spread:.0f} pts -- {hi['ward']} "
              f"{hi['occupancy_pct']:.0f}% vs {lo['ward']} {lo['occupancy_pct']:.0f}% "
              f"(threshold {max_spread:.0f})")
    return fired, detail, {"spread_pct": round(spread, 1), "threshold": max_spread,
                           "wards": wards[:20]}


EVALUATORS.update({
    "bed_occupancy_forecast_critical": eval_bed_occupancy_forecast,
    "er_boarding_pressure":            eval_er_boarding_pressure,
    "isolation_beds_full":             eval_isolation_beds_full,
    "discharged_bed_blocked":          eval_discharged_bed_blocked,
    "bed_turnaround_sla":              eval_bed_turnaround_sla,
    "ot_first_case_delayed":           eval_ot_first_case_delayed,
    "ot_surgery_overrun":              eval_ot_surgery_overrun,
    "ot_room_idle":                    eval_ot_room_idle,
    "ot_emergency_waiting":            eval_ot_emergency_waiting,
    "ot_icu_capacity_post_surgery":    eval_ot_icu_capacity_post_surgery,
    "ot_equipment_unavailable":        eval_ot_equipment_unavailable,
    "discharge_fit_pending":           eval_discharge_fit_pending,
    "discharge_billing_pending":       eval_discharge_billing_pending,
    "discharge_pharmacy_pending":      eval_discharge_pharmacy_pending,
    "discharge_summary_pending":       eval_discharge_summary_pending,
    "discharge_insurance_pending":     eval_discharge_insurance_pending,
    "discharge_delayed":               eval_discharge_delayed,
    "lab_tat_sla":                     eval_lab_tat_sla,
    "lab_critical_result":             eval_lab_critical_result,
    "lab_analyzer_down":               eval_lab_analyzer_down,
    "lab_collection_delayed":          eval_lab_collection_delayed,
    "lab_sample_rejections":           eval_lab_sample_rejections,
    "rc_claims_pending":               eval_rc_claims_pending,
    "rc_claim_denial_spike":           eval_rc_claim_denial_spike,
    "rc_billing_backlog":              eval_rc_billing_backlog,
    "rc_collections_overdue":          eval_rc_collections_overdue,
    "rc_revenue_leakage":              eval_rc_revenue_leakage,
    "exec_stress_index":               eval_exec_stress_index,
    "exec_sla_breaches":               eval_exec_sla_breaches,
    "exec_capacity_forecast":          eval_exec_capacity_forecast,
    "exec_kpi_deteriorating":          eval_exec_kpi_deteriorating,
    "exec_utilization_imbalance":      eval_exec_utilization_imbalance,
})
