import asyncio
import logging
from dataclasses import dataclass, field

from temporalio import activity

from db.hasura import hasura
from agents.bed.service import forecast_capacity
from api.routes.ws import broadcast
from util.forecast_client import forecast

logger = logging.getLogger(__name__)

# ward_type enum accepted by the /bed/turnover model.
_WARD_TYPES = ("Surgical", "Medical", "ICU_HDU", "Maternity", "Paediatric")
_AVG_CLEANING_TIME_MIN = 45.0   # not tracked in DB -> typical turnaround (conservative)

# forecast_period enum accepted by the /bed/occupancy model.
_OCC_HORIZONS = ("3h", "6h", "12h", "24h", "3d", "7d")


def _horizon_from_goal(goal: str) -> str:
    """Map the request goal to a /bed/occupancy forecast_period; default 24h."""
    g = (goal or "").lower()
    if "week" in g or "7 day" in g or "7d" in g:
        return "7d"
    if "3 day" in g or "3d" in g or "72h" in g:
        return "3d"
    if "12h" in g or "12 hour" in g:
        return "12h"
    if "6h" in g or "6 hour" in g:
        return "6h"
    if "3h" in g or "3 hour" in g:
        return "3h"
    return "24h"   # "today" / "tomorrow" / unspecified


def _clamp(value: float, lo: float, hi: float) -> float:
    """Keep a value inside the forecast API's documented input range."""
    return max(lo, min(hi, value))


def _classify_ward(ward: str) -> str:
    """Best-effort map of a free-text ward string to the model's ward_type enum.

    Only ICU/HDU is reliably distinguishable in our bed data (see agent_activities
    query_beds); everything else is matched on common substrings and defaults to
    Medical. ICU/HDU is checked first so NICU/PICU land in ICU_HDU.
    """
    w = (ward or "").upper()
    if "ICU" in w or "HDU" in w or "HIGH" in w:
        return "ICU_HDU"
    if "SURG" in w or "OT" in w or "POST-OP" in w or "POSTOP" in w or "PACU" in w:
        return "Surgical"
    if "MATERN" in w or "OBG" in w or "OBS" in w or "LABOUR" in w or "DELIV" in w:
        return "Maternity"
    if "PAED" in w or "PED" in w or "CHILD" in w:
        return "Paediatric"
    return "Medical"


@dataclass
class CapacitySnapshotInput:
    session_id: str
    context: dict = field(default_factory=dict)


@dataclass
class BedForecastInput:
    session_id: str
    snapshot: dict


@activity.defn
async def get_capacity_snapshot(inp: CapacitySnapshotInput) -> dict:
    """Gather all data needed for capacity prediction in parallel queries.

    Enriches the snapshot with more precise data from prior agents when
    available (er_agent triage results, icu_agent analysis, discharge_agent
    readiness counts) so Claude gets a higher-fidelity picture.
    """
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_bed_pred_census",
    })

    beds_summary, dr_now, horizon_4h, horizon_24h, er_pressure, critical_backlog = (
        await asyncio.gather(
            hasura.get_beds_summary(),
            hasura.get_discharge_ready_count(),
            hasura.get_discharge_horizon(4),
            hasura.get_discharge_horizon(24),
            hasura.get_er_pressure(),
            hasura.get_critical_escalation_backlog(),
        )
    )

    snapshot = {
        **beds_summary,
        "discharge_ready_now": dr_now,
        "discharge_4h":        horizon_4h,
        "discharge_24h":       horizon_24h,
        **er_pressure,
        "critical_backlog":    critical_backlog,
    }

    # Enrich with already-computed triage data from er_agent
    er_ctx = inp.context.get("er_agent", {})
    if er_ctx.get("results"):
        ctas_counts: dict[str, int] = {}
        for r in er_ctx["results"]:
            key = str(r.get("ctas", "unknown"))
            ctas_counts[key] = ctas_counts.get(key, 0) + 1
        snapshot["er_ctas_breakdown"] = ctas_counts
        snapshot["er_critical_from_triage"] = er_ctx.get("critical", 0)

    # Enrich with ICU analysis if icu_agent already ran
    icu_ctx = inp.context.get("icu_agent", {})
    if icu_ctx.get("escalation_candidates") is not None:
        snapshot["icu_escalation_backlog"] = len(icu_ctx["escalation_candidates"])
    if icu_ctx.get("step_down_recommended") is not None:
        snapshot["icu_step_down_available"] = icu_ctx["step_down_recommended"]

    # Use confirmed discharge count from discharge_agent if available
    discharge_ctx = inp.context.get("discharge_agent", {})
    if discharge_ctx.get("ready") is not None:
        snapshot["discharge_ready_confirmed"] = discharge_ctx["ready"]

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_bed_pred_census",
        "result": {
            "total_beds":      snapshot["total_beds"],
            "occupancy_pct":   snapshot["occupancy_pct"],
            "discharge_ready": dr_now,
            "er_pressure":     er_pressure["est_admissions"],
        },
    })
    logger.info(
        "capacity snapshot  session=%s  occupancy=%d%%  dr_ready=%d  er_pressure=%d  icu=%d%%",
        inp.session_id,
        snapshot["occupancy_pct"],
        dr_now,
        er_pressure["est_admissions"],
        snapshot["icu_pct"],
    )
    return snapshot


@activity.defn
async def run_capacity_forecast(inp: BedForecastInput) -> dict:
    """Claude generates a plain-language capacity forecast from the snapshot."""
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_bed_pred_forecast",
    })

    result = await forecast_capacity(inp.snapshot)

    risk = result.get("overflow_risk", "unknown")
    icu_risk = result.get("icu_risk", "unknown")

    # Escalate as alert if risk is medium or high
    if risk in ("medium", "high") or icu_risk in ("medium", "high"):
        severity = "critical" if risk == "high" or icu_risk == "high" else "warning"
        await broadcast(inp.session_id, {
            "type": "alert",
            "severity": severity,
            "message": result.get("forecast", "Capacity risk detected"),
        })

    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="bed_prediction_agent",
        event_type="capacity_forecast_generated",
        payload=result,
    )

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_bed_pred_forecast",
        "result": {
            "overflow_risk":    risk,
            "icu_risk":         icu_risk,
            "beds_freeing_4h":  result.get("beds_freeing_4h", 0),
            "beds_needed":      result.get("beds_needed", 0),
        },
    })
    logger.info(
        "capacity forecast  session=%s  overflow=%s  icu=%s",
        inp.session_id, risk, icu_risk,
    )
    return result


@activity.defn
async def forecast_bed_turnover(session_id: str, goal: str = "") -> dict:
    """Forecast beds becoming available per ward via the ML service (/bed/turnover).

    The DEPLOYED model is a per-ward census forecast keyed on forecast_period (the
    per-ward sibling of /bed/occupancy) — NOT the old fixed-shift schema. We classify
    live beds into the ward_type enum and issue one request per ward. Real signals:
    occupied_beds and total_beds (from the /beds list) and beds_being_cleaned (from
    /beds/dirty). Hospital-wide discharge signals are apportioned to each ward by its
    share of occupied beds; planned_admissions_today is not tracked (0);
    avg_cleaning_minutes is a constant. Degrades to forecast_available: 0 when the
    service is unconfigured or down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_bed_turnover"})

    beds, dirty, dr_now, horizon_8h = await asyncio.gather(
        hasura.get_enriched_beds(),
        hasura.get_dirty_beds(),
        hasura.get_discharge_ready_count(),
        hasura.get_discharge_horizon(8),   # next 8h shift window
    )
    beds = beds or []
    if not beds:
        result = {"forecast_available": 0, "reason": "no bed data", "wards": []}
        logger.info("forecast_bed_turnover  session=%s  no bed data", session_id)
        return result

    # Bucket total beds, occupied beds and beds-in-cleaning by ward_type.
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
    period = _horizon_from_goal(goal)

    # Wards to forecast: any that have beds (total, occupied, or in cleaning).
    wards = sorted(set(total_by_ward) | set(occ_by_ward) | set(clean_by_ward))

    predictions = []
    degraded = 0
    for wt in wards:
        occ = occ_by_ward.get(wt, 0)
        cleaning = clean_by_ward.get(wt, 0)
        total = max(total_by_ward.get(wt, 0), occ + cleaning)   # dirty beds may sit outside the enriched list
        share = (occ / total_occ) if total_occ else 0.0
        expected_discharges = horizon_8h * share   # apportioned shift discharge outlook
        recent_discharges   = dr_now * share       # apportioned discharge-ready proxy

        payload = {
            "forecast_period":           period,
            "ward_type":                 wt,
            "occupied_beds":             int(_clamp(occ, 0, 500)),
            "total_beds":                int(_clamp(total, occ, 500)),
            "beds_being_cleaned":        int(_clamp(cleaning, 0, 50)),
            "expected_discharges_today": round(_clamp(expected_discharges, 0, 100), 2),
            "planned_admissions_today":  0,       # not tracked
            "recent_discharges_4h":      int(_clamp(recent_discharges, 0, 50)),
            "avg_cleaning_minutes":      _AVG_CLEANING_TIME_MIN,
        }

        forecast_resp = await forecast("/bed/turnover", payload)
        if forecast_resp is None:
            degraded += 1
            continue

        # Envelope: flat dict or {"prediction": [{...}]}. The redesigned model mirrors
        # /bed/occupancy, so probe occupancy-style output names as well as the legacy ones.
        preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
        pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
        beds_available = next((pred[k] for k in ("beds_available_next_shift", "predicted_free_beds_next_shift",
                                                 "predicted_free_beds", "beds_available",
                                                 "predicted_beds_available", "value") if k in pred), None)
        capacity_alert = (pred.get("capacity_status") or pred.get("capacity_alert") or pred.get("status")
                          or pred.get("alert_level") or pred.get("overflow_risk") or pred.get("level") or "unknown")
        predictions.append({
            "ward_type":                 wt,
            "occupied_beds":             occ,
            "total_beds":                total,
            "beds_being_cleaned":        cleaning,
            "beds_available_next_shift": beds_available,
            "capacity_alert":            capacity_alert,
            "recommended_action":        (pred.get("recommended_action")
                                          or pred.get("staffing_recommendation") or pred.get("action") or ""),
            "fallback_used":             bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
        })

        if str(capacity_alert).lower() in ("low", "surge", "tight", "critical", "high"):
            await broadcast(session_id, {
                "type": "alert", "severity": "warning",
                "message": (f"Bed turnover forecast: {wt} has ~{beds_available} beds freeing ({period}) "
                            f"(capacity={capacity_alert}) — expedite pending discharges / cleaning."),
            })

    forecast_available = 0 if (not predictions and degraded) else 1 if predictions else 0
    low = [p for p in predictions if str(p.get("capacity_alert")).lower() in ("low", "surge", "tight", "critical", "high")]
    result = {
        "forecast_available": forecast_available,
        "wards_forecast":     len(predictions),
        "wards_degraded":     degraded,
        "low_capacity_count": len(low),
        "wards":              predictions,
    }
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_bed_turnover", "result": result})
    logger.info("forecast_bed_turnover  session=%s  wards=%d  degraded=%d  low=%d",
                session_id, len(predictions), degraded, len(low))
    return result


@activity.defn
async def forecast_bed_occupancy(session_id: str, goal: str = "") -> dict:
    """Whole-hospital forward census forecast via the ML service (/bed/occupancy).

    Unlike forecast_bed_turnover (per-ward, fixed shift), this is a single
    hospital-wide call at a horizon inferred from the goal. Real signals:
    total/occupied beds (/beds/summary), expected ER admissions (/er/pressure)
    and the discharge outlook (/admissions/discharge-horizon). Degrades to
    forecast_available: 0 when the service is unconfigured or down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_bed_occupancy"})

    summary, er_pressure, discharges = await asyncio.gather(
        hasura.get_beds_summary(),
        hasura.get_er_pressure(),
        hasura.get_discharge_horizon(24),
    )
    total = int((summary or {}).get("total_beds") or 0)
    if total <= 0:
        result = {"forecast_available": 0, "reason": "no bed data"}
        logger.info("forecast_bed_occupancy  session=%s  no bed data", session_id)
        return result

    horizon = _horizon_from_goal(goal)
    payload = {
        "forecast_period":      horizon,
        "total_beds":           total,
        "occupied_beds":        int((summary or {}).get("occupied_beds") or 0),
        "emergency_admissions": int((er_pressure or {}).get("est_admissions", 0) or 0),
        "discharges":           int(discharges or 0),
    }

    forecast_resp = await forecast("/bed/occupancy", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_bed_occupancy", "result": result})
        logger.info("forecast_bed_occupancy  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    # Envelope: flat dict (per the /bed/occupancy sample) or {"prediction": [{...}]}.
    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if preds else forecast_resp) or {}
    occ_pct = next((pred[k] for k in ("predicted_occupancy_percent", "predicted_occupancy_pct",
                                      "occupancy_percent", "value") if k in pred), None)
    overflow_risk = pred.get("overflow_risk") or pred.get("risk") or pred.get("level") or "unknown"
    result = {
        "forecast_available":          1,
        "horizon":                     horizon,
        "predicted_occupancy_percent": occ_pct,
        "predicted_occupied_beds":     pred.get("predicted_occupied_beds"),
        "predicted_free_beds":         pred.get("predicted_free_beds"),
        "change_vs_now_beds":          pred.get("change_vs_now_beds"),
        "overflow_risk":               overflow_risk,
        "expected_range":              pred.get("expected_range"),
        "recommended_action":          pred.get("recommended_action") or pred.get("action") or "",
        "fallback_used":               bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }

    if str(overflow_risk).lower() in ("medium", "high"):
        severity = "critical" if str(overflow_risk).lower() == "high" else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"Bed occupancy forecast ({horizon}): predicted {occ_pct}% occupied, "
                        f"overflow_risk={overflow_risk} — {result['recommended_action']}"),
        })

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_bed_occupancy", "result": result})
    logger.info("forecast_bed_occupancy  session=%s  horizon=%s  occ=%s%%  risk=%s",
                session_id, horizon, occ_pct, overflow_risk)
    return result


# -- sa_bed_ward_capacity ------------------------------------------------------

# /bed/ward-capacity ward_type enum uses "Pediatrics" (not our classifier's
# "Paediatric"); the rest of our classified wards are already valid values.
_WARD_CAPACITY_ENUM = {"Paediatric": "Pediatrics"}


def _ward_capacity_horizon(goal: str) -> str:
    """Map the goal to a /bed/ward-capacity forecast_period (6h|12h|24h|3d|7d -- no 3h)."""
    h = _horizon_from_goal(goal)
    return "6h" if h == "3h" else h


@activity.defn
async def forecast_bed_ward_capacity(session_id: str, goal: str = "") -> dict:
    """Forecast per-ward occupancy and capacity utilization via the ML service
    (/bed/ward-capacity). Per-ward like /bed/turnover: classify live beds into the
    ward_type enum and issue one request per ward. Real signals: total and occupied
    beds per ward and beds under maintenance (cleaning backlog). Hospital-wide
    discharge outlook and ER pressure are apportioned by each ward's occupied share.
    Degrades to forecast_available: 0 when the service is unconfigured or down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_bed_ward_capacity"})

    beds, dirty, horizon_24h, er_pressure = await asyncio.gather(
        hasura.get_enriched_beds(),
        hasura.get_dirty_beds(),
        hasura.get_discharge_horizon(24),
        hasura.get_er_pressure(),
    )
    beds = beds or []
    if not beds:
        result = {"forecast_available": 0, "reason": "no bed data", "wards": []}
        logger.info("forecast_bed_ward_capacity  session=%s  no bed data", session_id)
        return result

    er_admissions = int((er_pressure or {}).get("est_admissions", 0) or 0)
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
    horizon = _ward_capacity_horizon(goal)
    wards = sorted(set(total_by_ward) | set(occ_by_ward) | set(clean_by_ward))

    predictions, degraded = [], 0
    for wt in wards:
        occ = occ_by_ward.get(wt, 0)
        total = max(total_by_ward.get(wt, 0), occ)
        share = (occ / total_occ) if total_occ else 0.0
        payload = {
            "forecast_period":        horizon,
            "ward_type":              _WARD_CAPACITY_ENUM.get(wt, wt),
            "total_ward_beds":        int(_clamp(total, 0, 500)),
            "occupied_beds":          int(_clamp(occ, 0, 500)),
            "expected_discharges":    int(_clamp(round(horizon_24h * share), 0, 200)),
            "emergency_admissions":   int(_clamp(round(er_admissions * share), 0, 200)),
            "beds_under_maintenance": int(_clamp(clean_by_ward.get(wt, 0), 0, 100)),
        }

        forecast_resp = await forecast("/bed/ward-capacity", payload)
        if forecast_resp is None:
            degraded += 1
            continue

        preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
        pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
        util = next((pred[k] for k in ("predicted_capacity_utilization", "capacity_utilization", "value") if k in pred), None)
        overflow_risk = pred.get("overflow_risk") or pred.get("risk") or pred.get("level") or "unknown"
        predictions.append({
            "ward_type":                     _WARD_CAPACITY_ENUM.get(wt, wt),
            "total_ward_beds":               total,
            "occupied_beds":                 occ,
            "predicted_occupied_beds":       pred.get("predicted_occupied_beds"),
            "predicted_available_beds":      pred.get("predicted_available_beds"),
            "predicted_capacity_utilization": util,
            "expected_net_patient_change":   pred.get("expected_net_patient_change"),
            "ward_capacity_status":          pred.get("ward_capacity_status") or pred.get("status"),
            "overflow_risk":                 overflow_risk,
            "recommended_action":            pred.get("recommended_action") or pred.get("action") or "",
            "fallback_used":                 bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
        })
        if str(overflow_risk).lower() in ("medium", "high", "critical"):
            await broadcast(session_id, {
                "type": "alert", "severity": "critical" if str(overflow_risk).lower() in ("high", "critical") else "warning",
                "message": (f"Ward capacity forecast ({horizon}): {_WARD_CAPACITY_ENUM.get(wt, wt)} "
                            f"~{util}% utilised, overflow_risk={overflow_risk}."),
            })

    high = [p for p in predictions if str(p.get("overflow_risk")).lower() in ("medium", "high", "critical")]
    result = {
        "forecast_available": 1 if predictions else 0,
        "horizon":            horizon,
        "wards_forecast":     len(predictions),
        "wards_degraded":     degraded,
        "high_risk_count":    len(high),
        "wards":              predictions,
    }
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_bed_ward_capacity", "result": result})
    logger.info("forecast_bed_ward_capacity  session=%s  horizon=%s  wards=%d  degraded=%d  high=%d",
                session_id, horizon, len(predictions), degraded, len(high))
    return result


# -- sa_bed_isolation_demand ---------------------------------------------------

_ISOLATION_ROOM_TYPES = ("isolation", "side_room", "negative_pressure")


def _is_isolation_bed(b: dict) -> bool:
    return str(b.get("room_type") or "").lower() in _ISOLATION_ROOM_TYPES


@activity.defn
async def forecast_bed_isolation_demand(session_id: str, goal: str = "") -> dict:
    """Forecast isolation-bed demand and shortage risk via the ML service
    (/bed/isolation-demand). Real signals: total and occupied isolation beds
    (room_type isolation/side_room/negative_pressure), active infectious-disease
    cases (infection-control domain), suspected cases (active + isolation-required
    but not yet confirmed), and isolation beds under maintenance. Degrades to
    forecast_available: 0 when the service is unconfigured or down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_bed_isolation_demand"})

    beds, dirty, infections, suspected = await asyncio.gather(
        hasura.get_enriched_beds(),
        hasura.get_dirty_beds(),
        hasura.get_active_infection_cases(),
        hasura.get_non_isolated_infection_cases(),
    )
    beds = beds or []
    if not beds:
        result = {"forecast_available": 0, "reason": "no bed data"}
        logger.info("forecast_bed_isolation_demand  session=%s  no bed data", session_id)
        return result

    iso = [b for b in beds if b.get("is_active", True) and _is_isolation_bed(b)]
    total_iso = len(iso)
    occ_iso = sum(1 for b in iso if str(b.get("status") or "").lower() == "occupied")
    iso_dirty = sum(1 for b in (dirty or []) if _is_isolation_bed(b))

    horizon = _ward_capacity_horizon(goal)   # 6h|12h|24h|3d|7d (no 3h)
    payload = {
        "forecast_period":                 horizon,
        "current_isolation_beds_occupied": int(_clamp(occ_iso, 0, 500)),
        "total_isolation_beds":            int(_clamp(total_iso, 0, 500)),
        "infectious_disease_admissions":   int(_clamp(len(infections or []), 0, 1000)),
        "suspected_infectious_cases":      int(_clamp(len(suspected or []), 0, 1000)),
        "isolation_beds_under_maintenance": int(_clamp(iso_dirty, 0, 100)),
    }

    forecast_resp = await forecast("/bed/isolation-demand", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_bed_isolation_demand", "result": result})
        logger.info("forecast_bed_isolation_demand  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    required = next((pred[k] for k in ("predicted_isolation_beds_required", "predicted_beds_required", "value") if k in pred), None)
    shortage_risk = pred.get("isolation_shortage_risk") or pred.get("shortage_risk") or pred.get("risk") or "unknown"
    result = {
        "forecast_available":              1,
        "horizon":                         horizon,
        "predicted_isolation_beds_required": required,
        "predicted_available_isolation_beds": pred.get("predicted_available_isolation_beds"),
        "predicted_isolation_utilization": pred.get("predicted_isolation_utilization"),
        "expected_new_isolation_patients": pred.get("expected_new_isolation_patients"),
        "isolation_capacity_status":       pred.get("isolation_capacity_status") or pred.get("status"),
        "isolation_shortage_risk":         shortage_risk,
        "expected_range":                  pred.get("expected_range"),
        "recommended_action":              pred.get("recommended_action") or pred.get("action") or "",
        "total_isolation_beds":            payload["total_isolation_beds"],
        "current_isolation_beds_occupied": payload["current_isolation_beds_occupied"],
        "fallback_used":                   bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }

    if str(shortage_risk).lower() in ("medium", "high", "critical"):
        severity = "critical" if str(shortage_risk).lower() in ("high", "critical") else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"Isolation-bed demand forecast ({horizon}): ~{required} beds required, "
                        f"shortage_risk={shortage_risk} — {result['recommended_action']}"),
        })

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_bed_isolation_demand", "result": result})
    logger.info("forecast_bed_isolation_demand  session=%s  horizon=%s  required=%s  risk=%s",
                session_id, horizon, required, shortage_risk)
    return result
