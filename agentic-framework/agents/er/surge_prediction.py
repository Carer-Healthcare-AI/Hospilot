import logging
from datetime import datetime, timedelta, timezone

from temporalio import activity

from api.routes.ws import broadcast
from cache import redis as cache
from db.hasura import hasura
from util.forecast_client import forecast

logger = logging.getLogger(__name__)
_SA = "sa_er_surge_prediction"

# Control knob: how far ahead the model projects. Not a model feature -- the API
# just returns this many hourly rows starting at base_hour.
_HORIZON_HOURS = 6


def _clamp(value: float, lo: float, hi: float) -> float:
    """Keep a value inside the forecast API's documented input range."""
    return max(lo, min(hi, value))


def _season(month: int) -> str:
    """Map a month to the API's season enum (Summer|Monsoon|Winter|Spring).

    India-centric, matching the domain the model was trained on: the monsoon
    (Jun-Sep) and post-monsoon (Oct-Nov) are treated as distinct from winter.
    """
    if month in (12, 1, 2):
        return "Winter"
    if month in (3, 4, 5):
        return "Summer"
    if month in (6, 7, 8, 9):
        return "Monsoon"
    return "Spring"   # Oct-Nov (post-monsoon)


def _arrivals_last_hour(visits: list, now: datetime) -> int:
    """Count ER visits whose arrival timestamp falls in the trailing 60 minutes.

    This is the model's autoregressive anchor `prior_hour_volume` and the only
    input we can source from real data. Visits with an unparseable arrived_at are
    skipped (they still count toward the census-proxy fallback in the caller).
    """
    threshold = now - timedelta(hours=1)
    count = 0
    for v in visits:
        arrived = v.get("arrived_at") or v.get("visit_date") or ""
        try:
            dt = datetime.fromisoformat(str(arrived).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= threshold:
            count += 1
    return count


@activity.defn
async def forecast_er_surge(session_id: str) -> dict:
    """Forecast incoming ER arrival volume per hour via the ML service.

    Integration for the Hospilot forecast API (/forecast/er-surge). The model's
    primary driver, `prior_hour_volume`, is sourced from real data -- the count of
    active ER visits that arrived in the trailing hour. The optional autoregressive
    lags (lag_24h / lag_168h / rolling_7d_mean) are NOT sent: we have no hourly
    arrival history, and the API defaults them to prior_hour_volume and still
    returns a forecast. `is_holiday` is hardcoded to 0 (no holiday calendar wired).

    The client returns None when the service is unconfigured or down, so this task
    degrades gracefully to `forecast_available: 0` and the reactive triage /
    boarding paths still protect the ED.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})

    visits = await hasura.get_active_er_visits() or []
    now = datetime.now(timezone.utc)

    prior_hour = _arrivals_last_hour(visits, now)
    # Fall back to current census as a weak proxy when no arrival could be timed
    # (e.g. Fabric omitted arrived_at) but the ED clearly isn't empty.
    if prior_hour == 0 and visits:
        prior_hour = len(visits)

    payload = {
        "base_hour":         now.hour,
        "day_of_week":       now.strftime("%A"),
        "month":             now.month,
        "season":            _season(now.month),
        "is_holiday":        0,     # no holiday calendar wired yet
        "prior_hour_volume": int(_clamp(prior_hour, 0, 500)),
        "horizon_hours":     _HORIZON_HOURS,
    }

    forecast_resp = await forecast("/forecast/er-surge", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "forecast service unavailable",
                  "prior_hour_volume": payload["prior_hour_volume"], "hours": []}
        logger.info("forecast_er_surge  session=%s  service unavailable  prior_hour=%d",
                    session_id, payload["prior_hour_volume"])
        return result

    # Verified live envelope: {"task","model","prediction":{"horizon_hours",
    # "hourly_forecast":[{hour, day, predicted_arrivals, surge_level,
    # staffing_recommendation}], "peak_hour","peak_arrivals","total_expected"},
    # "fallback_used", ...}. The body is nested under "prediction" (a dict), so unwrap
    # it first; older builds put the rows directly at the top level.
    body = forecast_resp["prediction"] if isinstance(forecast_resp.get("prediction"), dict) else forecast_resp
    rows = (body.get("hourly_forecast") or body.get("prediction") or body.get("forecast")
            or body.get("hourly") or [])
    if not isinstance(rows, list):
        rows = []
    hours = []
    for r in rows:
        if not isinstance(r, dict):   # guard: never iterate a dict's keys / stray strings
            continue
        predicted = next((r[k] for k in ("predicted_arrivals", "predicted_volume",
                                         "predicted_count", "value") if k in r), None)
        surge_level = r.get("surge_level") or r.get("level") or "unknown"
        hours.append({
            "hour":        r.get("hour", r.get("hour_of_day", r.get("time"))),
            "predicted_volume": predicted,
            "surge_level": surge_level,
            "recommended_action": (r.get("staffing_recommendation")
                                   or r.get("recommended_action") or r.get("action") or ""),
        })

    predicted_vals = [h["predicted_volume"] for h in hours if h["predicted_volume"] is not None]
    total_expected = next((body[k] for k in ("total_expected", "total_forecast")
                           if k in body), None)
    if total_expected is None and predicted_vals:
        total_expected = sum(predicted_vals)
    # Prefer the server-provided peak; fall back to the max of parsed rows.
    peak = body.get("peak_arrivals")
    if peak is None:
        peak = max(predicted_vals) if predicted_vals else None
    surge_hours = [h for h in hours if str(h["surge_level"]).lower() in ("surge", "elevated")]

    if surge_hours:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": (f"ER surge forecast: {len(surge_hours)} of the next {_HORIZON_HOURS}h "
                        f"flagged {surge_hours[0]['surge_level']} (peak {peak} arrivals/hr) — "
                        f"pre-emptively staff triage."),
        })

    result = {
        "forecast_available": 1,
        "prior_hour_volume":  payload["prior_hour_volume"],
        "horizon_hours":      _HORIZON_HOURS,
        "total_expected":     total_expected,
        "peak_volume":        peak,
        "surge_hour_count":   len(surge_hours),
        "hours":              hours,
        "fallback_used":      bool(forecast_resp.get("fallback_used")),
    }
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("forecast_er_surge  session=%s  prior_hour=%d  total=%s  surge_hours=%d",
                session_id, payload["prior_hour_volume"], total_expected, len(surge_hours))
    return result


# -- sa_er_wait_time -----------------------------------------------------------

def _wait_horizon(goal: str) -> str:
    """Map the request goal to an /er/wait-time forecast_period (3h|6h|12h|24h|3d|7d)."""
    g = (goal or "").lower()
    if "week" in g or "7 day" in g or "7d" in g:
        return "7d"
    if "3 day" in g or "3d" in g or "72h" in g:
        return "3d"
    if "12h" in g or "12 hour" in g:
        return "12h"
    if "24h" in g or "24 hour" in g or "tomorrow" in g or "today" in g:
        return "24h"
    if "3h" in g or "3 hour" in g:
        return "3h"
    return "6h"   # default near-term ER horizon


@activity.defn
async def forecast_er_wait_time(session_id: str, goal: str = "") -> dict:
    """Forecast the average ER wait time (minutes) a chosen time ahead via the ML
    service (/er/wait-time), with wait status and 8-min target-breach risk.

    Real signals: patients waiting (untriaged queue), patients currently in the ED,
    recent arrivals (trailing hour) and critical CTAS 1-2 count. doctors_on_duty /
    nurses_on_duty are HOSPITAL-WIDE proxies -- ER is not a distinct roster area, so
    doctors come from the registered doctor-user count and nurses from current-shift
    roster headcount (NOT ER-scoped). avg_minutes_per_patient falls to the model
    default. Degrades to forecast_available: 0 when the service is unconfigured/down
    or there are no doctors to staff against.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_er_wait_time"})

    visits = await hasura.get_active_er_visits() or []
    untriaged = await hasura.get_untriaged_visits() or []
    pressure = await hasura.get_er_pressure() or {}
    try:
        doctors = int(await hasura.count_users_by_role("doctor") or 0)
    except Exception:  # noqa: BLE001 -- proxy; best-effort
        doctors = 0
    if doctors <= 0:
        result = {"forecast_available": 0, "reason": "no doctor staffing data"}
        logger.info("forecast_er_wait_time  session=%s  no doctor data", session_id)
        return result

    try:
        roster = await hasura.staff_list_roster(None)
    except Exception:  # noqa: BLE001
        roster = []
    nurses = sum(int(r.get("headcount") or 0) for r in (roster or [])
                 if "nurse" in (f"{r.get('role') or ''} {r.get('area') or ''}").lower())

    now = datetime.now(timezone.utc)
    horizon = _wait_horizon(goal)
    payload = {
        "forecast_period":         horizon,
        "patients_waiting":        int(_clamp(len(untriaged), 0, 5000)),
        "doctors_on_duty":         int(_clamp(doctors, 0, 5000)),
        "patients_currently_in_er": int(_clamp(len(visits), 0, 5000)),
        "recent_er_arrivals":      int(_clamp(_arrivals_last_hour(visits, now), 0, 5000)),
        "nurses_on_duty":          int(_clamp(nurses, 0, 5000)),
        "critical_patients":       int(_clamp(int(pressure.get("ctas_1_2") or 0), 0, 5000)),
    }

    forecast_resp = await forecast("/er/wait-time", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_wait_time", "result": result})
        logger.info("forecast_er_wait_time  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    # Envelope: flat dict (per the sample) or {"prediction": [{...}]}.
    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    minutes = next((pred[k] for k in ("predicted_wait_minutes", "predicted_wait", "value") if k in pred), None)
    breach_risk = pred.get("target_breach_risk") or pred.get("breach_risk") or pred.get("risk") or "unknown"
    result = {
        "forecast_available":     1,
        "horizon":                horizon,
        "predicted_wait_minutes": minutes,
        "change_vs_now_minutes":  pred.get("change_vs_now_minutes"),
        "wait_status":            pred.get("wait_status") or pred.get("status"),
        "target_breach_risk":     breach_risk,
        "expected_range":         pred.get("expected_range"),
        "recommended_action":     pred.get("recommended_action") or pred.get("action") or "",
        "patients_waiting":       payload["patients_waiting"],
        "doctors_on_duty":        payload["doctors_on_duty"],
        "fallback_used":          bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }

    if str(breach_risk).lower() in ("medium", "high"):
        severity = "critical" if str(breach_risk).lower() == "high" else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"ER wait-time forecast ({horizon}): predicted {minutes} min, "
                        f"target_breach_risk={breach_risk} — {result['recommended_action']}"),
        })

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_wait_time", "result": result})
    logger.info("forecast_er_wait_time  session=%s  horizon=%s  minutes=%s  breach=%s",
                session_id, horizon, minutes, breach_risk)
    return result


# -- sa_er_boarding_forecast ---------------------------------------------------

@activity.defn
async def forecast_er_boarding(session_id: str, goal: str = "") -> dict:
    """Forecast admitted patients boarding in the ED awaiting inpatient beds via the
    ML service (/er/boarding), with boarding time and risk.

    Real signals: current boarders (ER visits with status 'boarded'), available and
    total inpatient beds + current census (beds summary), ER queue size and available
    ICU beds. Expected discharges / bed-assignment / staffing indices fall to model
    defaults. Degrades to forecast_available: 0 when the service is unconfigured/down
    or there is no bed data.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_er_boarding_forecast"})

    beds = await hasura.get_beds_summary() or {}
    total = int(beds.get("total_beds") or 0)
    if total <= 0:
        result = {"forecast_available": 0, "reason": "no bed data"}
        logger.info("forecast_er_boarding  session=%s  no bed data", session_id)
        return result

    visits = await hasura.get_active_er_visits() or []
    try:
        all_v = await cache.get_many("er_visit:*")
        boarders = sum(1 for v in (all_v or []) if isinstance(v, dict) and v.get("status") == "boarded")
    except Exception:  # noqa: BLE001 -- boarder cache is best-effort
        boarders = 0

    icu_total = int(beds.get("icu_total") or 0)
    icu_occ = int(beds.get("icu_occupied") or 0)
    horizon = _wait_horizon(goal)
    if horizon == "3h":   # /er/boarding enum has no 3h
        horizon = "6h"
    payload = {
        "forecast_period":         horizon,
        "current_boarding_patients": int(_clamp(boarders, 0, 5000)),
        "available_inpatient_beds": int(_clamp(int(beds.get("available_beds") or 0), 0, 100000)),
        "hospital_admissions":     int(_clamp(int(beds.get("occupied_beds") or 0), 0, 100000)),
        "er_patient_count":        int(_clamp(len(visits), 0, 5000)),
        "available_icu_beds":      int(_clamp(max(icu_total - icu_occ, 0), 0, 5000)),
        "occupied_hospital_beds":  int(_clamp(int(beds.get("occupied_beds") or 0), 0, 100000)),
        "total_inpatient_beds":    int(_clamp(total, 0, 100000)),
    }

    forecast_resp = await forecast("/er/boarding", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_boarding_forecast", "result": result})
        logger.info("forecast_er_boarding  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    predicted = next((pred[k] for k in ("predicted_boarding_patients", "predicted_boarders", "value") if k in pred), None)
    boarding_risk = pred.get("boarding_risk") or pred.get("risk") or pred.get("level") or "unknown"
    result = {
        "forecast_available":         1,
        "horizon":                    horizon,
        "predicted_boarding_patients": predicted,
        "predicted_average_boarding_time": pred.get("predicted_average_boarding_time"),
        "predicted_available_inpatient_beds": pred.get("predicted_available_inpatient_beds"),
        "predicted_er_bed_occupancy": pred.get("predicted_er_bed_occupancy"),
        "boarding_status":            pred.get("boarding_status") or pred.get("status"),
        "boarding_risk":              boarding_risk,
        "expected_range":             pred.get("expected_range"),
        "recommended_action":         pred.get("recommended_action") or pred.get("action") or "",
        "current_boarding_patients":  payload["current_boarding_patients"],
        "fallback_used":              bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }

    if str(boarding_risk).lower() in ("medium", "high", "critical"):
        severity = "critical" if str(boarding_risk).lower() in ("high", "critical") else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"ED boarding forecast ({horizon}): ~{predicted} boarding, "
                        f"boarding_risk={boarding_risk} — {result['recommended_action']}"),
        })

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_boarding_forecast", "result": result})
    logger.info("forecast_er_boarding  session=%s  horizon=%s  boarding=%s  risk=%s",
                session_id, horizon, predicted, boarding_risk)
    return result


# -- sa_er_lwbs ----------------------------------------------------------------

def _avg_wait_minutes(visits: list, now: datetime) -> float:
    """Mean minutes-since-arrival across the given visits (0.0 if none can be timed)."""
    ages = []
    for v in visits:
        raw = v.get("arrived_at") or v.get("visit_date") or ""
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ages.append((now - dt).total_seconds() / 60.0)
    return round(sum(ages) / len(ages), 1) if ages else 0.0


@activity.defn
async def forecast_er_lwbs(session_id: str, goal: str = "") -> dict:
    """Forecast patients Leaving Without Being Seen (LWBS) via the ML service
    (/er/lwbs-forecast), with LWBS rate and risk.

    Real signals: waiting (untriaged) queue size, average wait time (mean age of the
    waiting queue), patients currently in the ED, recent arrivals and critical CTAS
    1-2 count. ER-capacity fields (available_er_beds / treatment_rooms) aren't
    modelled but are OPTIONAL here, so they fall to model defaults. Degrades to
    forecast_available: 0 when the service is unconfigured/down or the ED is empty.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_er_lwbs"})

    visits = await hasura.get_active_er_visits() or []
    untriaged = await hasura.get_untriaged_visits() or []
    if not visits and not untriaged:
        result = {"forecast_available": 0, "reason": "no ER activity"}
        logger.info("forecast_er_lwbs  session=%s  no ER activity", session_id)
        return result
    pressure = await hasura.get_er_pressure() or {}

    now = datetime.now(timezone.utc)
    horizon = _wait_horizon(goal)
    if horizon == "3h":   # /er/lwbs-forecast enum has no 3h
        horizon = "6h"
    payload = {
        "forecast_period":         horizon,
        "current_waiting_patients": int(_clamp(len(untriaged), 0, 5000)),
        "average_wait_time":       round(_clamp(_avg_wait_minutes(untriaged or visits, now), 0, 100000), 1),
        "current_er_patients":     int(_clamp(len(visits), 0, 5000)),
        "er_arrivals":             int(_clamp(_arrivals_last_hour(visits, now), 0, 5000)),
        "triage_queue_size":       int(_clamp(len(untriaged), 0, 5000)),
        "critical_patient_count":  int(_clamp(int(pressure.get("ctas_1_2") or 0), 0, 5000)),
    }

    forecast_resp = await forecast("/er/lwbs-forecast", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_lwbs", "result": result})
        logger.info("forecast_er_lwbs  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    lwbs = next((pred[k] for k in ("predicted_lwbs_patients", "predicted_lwbs", "value") if k in pred), None)
    lwbs_risk = pred.get("lwbs_risk_level") or pred.get("lwbs_risk") or pred.get("risk") or "unknown"
    result = {
        "forecast_available":       1,
        "horizon":                  horizon,
        "predicted_lwbs_patients":  lwbs,
        "predicted_lwbs_rate":      pred.get("predicted_lwbs_rate"),
        "predicted_average_wait_time": pred.get("predicted_average_wait_time"),
        "predicted_waiting_patients": pred.get("predicted_waiting_patients"),
        "lwbs_risk_level":          lwbs_risk,
        "expected_range":           pred.get("expected_range"),
        "recommended_action":       pred.get("recommended_action") or pred.get("action") or "",
        "current_waiting_patients": payload["current_waiting_patients"],
        "average_wait_time":        payload["average_wait_time"],
        "fallback_used":            bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }

    if str(lwbs_risk).lower() in ("medium", "high", "critical"):
        severity = "critical" if str(lwbs_risk).lower() in ("high", "critical") else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"ER LWBS forecast ({horizon}): ~{lwbs} patients may leave without being seen, "
                        f"risk={lwbs_risk} — {result['recommended_action']}"),
        })

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_lwbs", "result": result})
    logger.info("forecast_er_lwbs  session=%s  horizon=%s  lwbs=%s  risk=%s",
                session_id, horizon, lwbs, lwbs_risk)
    return result


# -- sa_er_congestion ----------------------------------------------------------

@activity.defn
async def forecast_er_congestion(session_id: str, goal: str = "") -> dict:
    """Forecast overall ED congestion as a composite score (0-100) via the ML
    service (/er/congestion), with a congestion level and recommended action.

    Real signals: patients currently in the ED, the waiting (untriaged) queue,
    current boarders, average wait time (mean age of the waiting queue), recent
    arrivals (trailing hour), critical CTAS 1-2 count, available inpatient beds and
    doctor/nurse staffing. doctors/nurses are HOSPITAL-WIDE proxies (ER is not a
    distinct roster area), mirroring sa_er_wait_time. The remaining model inputs
    (ambulance arrivals, ER-scoped beds / treatment rooms, average LOS, discharges,
    seasonal disease index) aren't sourced and fall to model defaults. Degrades to
    forecast_available: 0 when the service is unconfigured/down or the ED is empty.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_er_congestion"})

    visits = await hasura.get_active_er_visits() or []
    untriaged = await hasura.get_untriaged_visits() or []
    if not visits and not untriaged:
        result = {"forecast_available": 0, "reason": "no ER activity"}
        logger.info("forecast_er_congestion  session=%s  no ER activity", session_id)
        return result

    pressure = await hasura.get_er_pressure() or {}
    beds = await hasura.get_beds_summary() or {}
    try:
        doctors = int(await hasura.count_users_by_role("doctor") or 0)
    except Exception:  # noqa: BLE001 -- proxy; best-effort
        doctors = 0
    try:
        roster = await hasura.staff_list_roster(None)
    except Exception:  # noqa: BLE001
        roster = []
    nurses = sum(int(r.get("headcount") or 0) for r in (roster or [])
                 if "nurse" in (f"{r.get('role') or ''} {r.get('area') or ''}").lower())
    try:
        all_v = await cache.get_many("er_visit:*")
        boarders = sum(1 for v in (all_v or []) if isinstance(v, dict) and v.get("status") == "boarded")
    except Exception:  # noqa: BLE001 -- boarder cache is best-effort
        boarders = 0

    now = datetime.now(timezone.utc)
    horizon = _wait_horizon(goal)
    if horizon == "3h":   # /er/congestion enum has no 3h
        horizon = "6h"
    payload = {
        "forecast_period":          horizon,
        "current_er_patients":      int(_clamp(len(visits), 0, 5000)),
        "current_waiting_patients": int(_clamp(len(untriaged), 0, 5000)),
        "current_boarding_patients": int(_clamp(boarders, 0, 5000)),
        "average_er_wait_time":     round(_clamp(_avg_wait_minutes(untriaged or visits, now), 0, 100000), 1),
        "er_arrivals":              int(_clamp(_arrivals_last_hour(visits, now), 0, 5000)),
        "critical_patient_count":   int(_clamp(int(pressure.get("ctas_1_2") or 0), 0, 5000)),
        "available_inpatient_beds": int(_clamp(int(beds.get("available_beds") or 0), 0, 100000)),
        "available_doctors":        int(_clamp(doctors, 0, 5000)),
        "available_nurses":         int(_clamp(nurses, 0, 5000)),
    }

    forecast_resp = await forecast("/er/congestion", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_congestion", "result": result})
        logger.info("forecast_er_congestion  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    # Envelope: flat dict with {"prediction": [{predicted_congestion_score,
    # recommended_action}], "thresholds_applied": {level_busy, level_congested,
    # level_critical}}. Older builds may put the row directly at the top level.
    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    score = next((pred[k] for k in ("predicted_congestion_score", "congestion_score", "value") if k in pred), None)

    # Derive a level from the server's own thresholds so the label always matches
    # the score the model returned (no hard-coded cutoffs).
    thresholds = forecast_resp.get("thresholds_applied") if isinstance(forecast_resp, dict) else None
    thresholds = thresholds or {}
    busy = thresholds.get("level_busy", 45)
    congested = thresholds.get("level_congested", 65)
    critical = thresholds.get("level_critical", 80)
    level = pred.get("congestion_level") or pred.get("level")
    if level is None and isinstance(score, (int, float)):
        level = ("critical" if score >= critical else "congested" if score >= congested
                 else "busy" if score >= busy else "normal")
    level = level or "unknown"

    result = {
        "forecast_available":         1,
        "horizon":                    horizon,
        "predicted_congestion_score": score,
        "congestion_level":           level,
        "recommended_action":         pred.get("recommended_action") or pred.get("action") or "",
        "thresholds_applied":         thresholds,
        "current_er_patients":        payload["current_er_patients"],
        "current_waiting_patients":   payload["current_waiting_patients"],
        "fallback_used":              bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }

    if str(level).lower() in ("congested", "critical"):
        severity = "critical" if str(level).lower() == "critical" else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"ED congestion forecast ({horizon}): score {score} ({level}) — "
                        f"{result['recommended_action']}"),
        })

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_congestion", "result": result})
    logger.info("forecast_er_congestion  session=%s  horizon=%s  score=%s  level=%s",
                session_id, horizon, score, level)
    return result


# -- sa_er_ambulance_arrivals --------------------------------------------------

@activity.defn
async def forecast_ambulance_arrivals(session_id: str, goal: str = "") -> dict:
    """Forecast ambulances that will arrive at the ED over a horizon via the ML
    service (/er/ambulance-arrivals).

    The model is driven by arrival history, time-of-day/season and external risk
    (accidents/weather/events) -- NOT by ED capacity. Its one live lever we can source
    is current_ambulance_arrivals, proxied from the count of Busy units in the live
    fleet (cache.get_all_ambulances); the ED-context fields (er_patient_count, waiting,
    ICU beds, occupancy, doctor/nurse proxies) are model non-features that only feed a
    server-derived accessibility index, sent as real values where we have them.
    available_er_beds isn't modelled anywhere and is a required schema non-feature, so
    it is sent as 0. External indices (road/weather/season/events) fall to model
    defaults. Degrades to forecast_available: 0 when the service is unconfigured/down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_er_ambulance_arrivals"})

    fleet = await cache.get_all_ambulances() or []
    busy = sum(1 for a in fleet if str(a.get("status") or "").lower() == "busy")
    visits = await hasura.get_active_er_visits() or []
    untriaged = await hasura.get_untriaged_visits() or []
    pressure = await hasura.get_er_pressure() or {}
    beds = await hasura.get_beds_summary() or {}
    try:
        doctors = int(await hasura.count_users_by_role("doctor") or 0)
    except Exception:  # noqa: BLE001 -- proxy; best-effort
        doctors = 0
    try:
        roster = await hasura.staff_list_roster(None)
    except Exception:  # noqa: BLE001
        roster = []
    nurses = sum(int(r.get("headcount") or 0) for r in (roster or [])
                 if "nurse" in (f"{r.get('role') or ''} {r.get('area') or ''}").lower())

    icu_total, icu_occ = int(beds.get("icu_total") or 0), int(beds.get("icu_occupied") or 0)
    total_beds, occ_beds = int(beds.get("total_beds") or 0), int(beds.get("occupied_beds") or 0)
    occ_pct = round(occ_beds / total_beds * 100, 1) if total_beds > 0 else 0.0

    horizon = _wait_horizon(goal)
    if horizon == "3h":   # /er/ambulance-arrivals enum has no 3h
        horizon = "6h"
    payload = {
        "forecast_period":            horizon,
        "current_ambulance_arrivals": int(_clamp(busy, 0, 5000)),   # Busy-fleet proxy
        "er_patient_count":           int(_clamp(len(visits), 0, 5000)),
        "available_er_beds":          0,   # ER beds not modelled; required schema non-feature
        "er_waiting_patients":        int(_clamp(len(untriaged), 0, 5000)),
        "critical_patient_count":     int(_clamp(int(pressure.get("ctas_1_2") or 0), 0, 5000)),
        "available_doctors":          int(_clamp(doctors, 0, 5000)),
        "available_nurses":           int(_clamp(nurses, 0, 5000)),
        "available_icu_beds":         int(_clamp(max(icu_total - icu_occ, 0), 0, 5000)),
        "hospital_bed_occupancy":     round(_clamp(occ_pct, 0, 100), 1),
    }

    forecast_resp = await forecast("/er/ambulance-arrivals", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_ambulance_arrivals", "result": result})
        logger.info("forecast_ambulance_arrivals  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    arrivals = next((pred[k] for k in ("predicted_ambulance_arrivals", "predicted_arrivals", "value") if k in pred), None)
    # The model may or may not return a level; we do NOT synthesize one (thresholds are
    # ratios vs a baseline we don't have, and hourly current vs windowed predicted aren't
    # comparable). Surface any model-provided level, else "unknown".
    level = pred.get("arrival_level") or pred.get("surge_level") or pred.get("risk") or pred.get("level") or "unknown"

    result = {
        "forecast_available":          1,
        "horizon":                     horizon,
        "predicted_ambulance_arrivals": arrivals,
        "current_ambulance_arrivals":  payload["current_ambulance_arrivals"],
        "arrival_level":               level,
        "recommended_action":          pred.get("recommended_action") or pred.get("action") or "",
        "fallback_used":               bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }

    if str(level).lower() in ("busy", "surge", "high", "medium", "critical"):
        severity = "critical" if str(level).lower() in ("surge", "high", "critical") else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"Ambulance-arrival forecast ({horizon}): ~{arrivals} ambulances inbound "
                        f"({level}) — {result['recommended_action']}"),
        })

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_er_ambulance_arrivals", "result": result})
    logger.info("forecast_ambulance_arrivals  session=%s  horizon=%s  arrivals=%s  level=%s",
                session_id, horizon, arrivals, level)
    return result
