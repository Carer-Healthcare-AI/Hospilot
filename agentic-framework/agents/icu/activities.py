import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from temporalio import activity

from fhir.resources.encounter import Encounter
from fhir.resources.location import Location

from db.hasura import hasura
from fhirgw import repository as repo
from fhirgw.mappers import observation as obs_map
from fhirgw.mappers._common import ref_id
from agents.icu.service import analyze_icu, rank_icu_admissions, is_critical
from util.idem import make_idem_key
from util.forecast_client import forecast
from workflows.temporal.workflow._escalation import start_escalating_approval
from api.routes.ws import broadcast

logger = logging.getLogger(__name__)


def _clamp(value: float, lo: float, hi: float) -> float:
    """Keep a value inside the forecast API's documented input range."""
    return max(lo, min(hi, value))


def _demand_horizon(goal: str) -> str:
    """Map the request goal to an /icu/demand forecast_period.

    This model's enum is 3h|6h|12h|24h|48h (no 3d/7d), so multi-day goals cap at
    48h. Default 24h.
    """
    g = (goal or "").lower()
    if ("48h" in g or "2 day" in g or "2d" in g or "day after" in g
            or "week" in g or "7d" in g or "3 day" in g or "3d" in g or "72h" in g):
        return "48h"
    if "12h" in g or "12 hour" in g:
        return "12h"
    if "6h" in g or "6 hour" in g:
        return "6h"
    if "3h" in g or "3 hour" in g:
        return "3h"
    return "24h"   # "today" / "tomorrow" / unspecified


def _horizon_from_goal(goal: str) -> str:
    """Map the request goal to an /icu/occupancy forecast_period; default 24h."""
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


def _fhir_json(resource) -> dict:
    """Canonical FHIR JSON for crossing the Temporal activity boundary."""
    return resource.model_dump(mode="json", by_alias=True, exclude_none=True)


def _enc_bed_id(enc: Encounter) -> str | None:
    """The referenced bed id from Encounter.location[0].location (or None)."""
    locs = getattr(enc, "location", None)
    if not locs:
        return None
    return ref_id(locs[0].location)


@dataclass
class IcuAnalysisInput:
    session_id: str
    icu_admissions: list             # FHIR Encounter JSON
    non_icu_admissions: list         # FHIR Encounter JSON
    available_beds: list             # FHIR Location JSON
    bed_by_id: dict | None = None    # bed_id -> FHIR Location JSON (occupied beds)


@dataclass
class IcuApprovalInput:
    session_id: str
    step_down_candidates: list
    escalation_candidates: list
    summary: str


@dataclass
class IcuConfirmInput:
    session_id: str
    critical_vital_ids: list   # vitals to flag is_critical=true
    assessments: list          # full Claude result for audit


@dataclass
class IcuTransferInput:
    session_id: str
    incoming_patients: list    # ER critical patients with bed_type_needed == "ICU"
    available_beds: list


@dataclass
class IcuAdmissionInput:
    session_id: str
    ranked_requests: list      # output of rank_icu_requests


@activity.defn
async def get_icu_census(session_id: str) -> dict:
    """Fetch ICU beds, current ICU patients, and non-ICU admitted patients."""
    await broadcast(session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_icu_census",
    })

    # FHIR-native reads: canonical Encounter / Location resources via the repository.
    icu_encs, icu_beds = await repo.icu_admissions()
    non_encs, non_beds = await repo.non_icu_admissions()
    available_beds     = await repo.available_icu_beds()
    bed_by_id          = {**icu_beds, **non_beds}   # occupied beds, for ventilation lookup

    await broadcast(session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_icu_census",
        "result": {
            "icu_occupied":      len(icu_encs),
            "icu_available":     len(available_beds),
            "non_icu_admitted":  len(non_encs),
        },
    })
    logger.info(
        "ICU census  session=%s  icu=%d  available=%d  ward=%d",
        session_id, len(icu_encs), len(available_beds), len(non_encs),
    )
    return {
        "icu_admissions":     [_fhir_json(e) for e in icu_encs],
        "non_icu_admissions": [_fhir_json(e) for e in non_encs],
        "available_beds":     [_fhir_json(b) for b in available_beds],
        "bed_by_id":          {k: _fhir_json(v) for k, v in bed_by_id.items()},
    }


@activity.defn
async def analyze_icu_status(inp: IcuAnalysisInput) -> dict:
    """
    Fetch vitals for all patients, pre-filter escalation candidates by critical thresholds,
    then call Claude for step-down and escalation recommendations.
    """
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_icu_analysis",
    })

    # Re-parse the FHIR JSON handed across the activity boundary into models.
    icu_encs  = [Encounter.model_validate(a) for a in inp.icu_admissions]
    non_encs  = [Encounter.model_validate(a) for a in inp.non_icu_admissions]
    beds      = [Location.model_validate(b) for b in inp.available_beds]
    bed_by_id = {k: Location.model_validate(v) for k, v in (inp.bed_by_id or {}).items()}

    def _bed_for(enc: Encounter):
        bid = _enc_bed_id(enc)
        return bed_by_id.get(bid) if bid else None

    # Fetch vitals (FHIR Observations) for ICU patients
    icu_with_vitals = []
    for enc in icu_encs:
        token  = ref_id(enc.subject)
        vitals = await repo.latest_vitals(token) if token else []
        icu_with_vitals.append({"encounter": enc, "vitals": vitals, "bed": _bed_for(enc)})

    # Fetch vitals for non-ICU patients and keep only those with critical readings.
    # Deduplicate by patient token -- same patient across multiple admissions appears once.
    escalation_candidates = []
    critical_vital_ids: list[str] = []
    seen_tokens: set[str] = set()
    for enc in non_encs:
        token  = ref_id(enc.subject)
        vitals = await repo.latest_vitals(token) if token else []
        if vitals and is_critical(vitals):
            if token and token in seen_tokens:
                continue
            if token:
                seen_tokens.add(token)
            escalation_candidates.append({"encounter": enc, "vitals": vitals, "bed": _bed_for(enc)})
            vid = obs_map.vitals_to_internal(vitals).get("id")
            if vid:
                critical_vital_ids.append(vid)

    result = await analyze_icu(icu_with_vitals, escalation_candidates, beds)
    result["critical_vital_ids"] = critical_vital_ids
    # Combined count gating ta_create_icu_approval: the approval fires when there is at
    # least one transfer candidate of EITHER kind (step-down OR escalation). Typed
    # conditions are single-symbol with no OR, so the union is surfaced as one field.
    result["transfer_candidate_count"] = (
        len(result.get("step_down_candidates", [])) + len(result.get("escalation_candidates", []))
    )
    result["vitals_by_admission_id"] = {
        p["encounter"].id: obs_map.vitals_to_internal(p["vitals"])
        for p in icu_with_vitals + escalation_candidates
        if p.get("vitals")
    }

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_icu_analysis",
        "result": {
            "step_down":    len(result.get("step_down_candidates", [])),
            "escalations":  len(result.get("escalation_candidates", [])),
            "critical_vitals": len(critical_vital_ids),
        },
    })
    logger.info(
        "ICU analysis  session=%s  step_down=%d  escalations=%d  critical_vitals=%d",
        inp.session_id,
        len(result.get("step_down_candidates", [])),
        len(result.get("escalation_candidates", [])),
        len(critical_vital_ids),
    )
    return result


@activity.defn
async def create_icu_approval(inp: IcuApprovalInput) -> dict:
    approval = await hasura.create_approval_task(
        session_id=inp.session_id,
        agent_id="icu_agent",
        action_type="icu_transfer_recommendations",
        payload={
            "step_down_candidates":   inp.step_down_candidates,
            "escalation_candidates":  inp.escalation_candidates,
            "summary":                inp.summary,
        },
        idempotency_key=make_idem_key(
            "icu_transfer", inp.session_id,
            sorted(c.get("admission_id") for c in
                   (inp.step_down_candidates + inp.escalation_candidates))),
    )

    # Fetch patient names -- patients are not cached in Redis on this branch, use Fabric directly.
    all_tokens = list({
        c["patient_token"]
        for c in inp.step_down_candidates + inp.escalation_candidates
        if c.get("patient_token")
    })
    patient_map = await hasura.get_patient_names(all_tokens)

    def _patient_display(token: str | None) -> tuple[str, str]:
        """Returns (full_name, display_id) for a patient token."""
        if not token:
            return "Unknown Patient", "--"
        p = patient_map.get(token)
        if not p:
            return f"Patient {token[:8]}", token[:8]
        name = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or f"Patient {token[:8]}"
        uid  = p.get("uhid") or token[:8]
        return name, uid

    step_downs = []
    for c in inp.step_down_candidates:
        name, uid = _patient_display(c.get("patient_token"))
        step_downs.append({
            "patient_name": name,
            "patient_id":   uid,
            "reason":       c.get("reason") or c.get("chief_complaint") or "Step-down recommended",
            "confidence":   c.get("confidence", ""),
        })

    escalations = []
    for c in inp.escalation_candidates:
        name, uid = _patient_display(c.get("patient_token"))
        escalations.append({
            "patient_name": name,
            "patient_id":   uid,
            "reason":       c.get("reason") or c.get("chief_complaint") or "ICU escalation needed",
            "urgency":      c.get("urgency", ""),
        })

    await broadcast(inp.session_id, {
        "type":             "approval_required",
        "approval_id":      approval["id"],
        "action":           "icu_transfer_recommendations",
        "step_down_count":  len(inp.step_down_candidates),
        "escalation_count": len(inp.escalation_candidates),
        "summary":          inp.summary,
        "step_downs":       step_downs,
        "escalations":      escalations,
    })
    logger.info(
        "ICU approval created  session=%s  approval=%s  step_down=%d  escalations=%d",
        inp.session_id, approval["id"],
        len(inp.step_down_candidates), len(inp.escalation_candidates),
    )
    await start_escalating_approval(
        session_id=inp.session_id,
        approval_id=approval["id"],
        agent_id="icu_agent",
        action_type="icu_transfer_recommendations",
        payload={"step_down_candidates": inp.step_down_candidates,
                 "escalation_candidates": inp.escalation_candidates, "summary": inp.summary},
    )
    return {"approval_id": approval["id"]}


@activity.defn
async def confirm_icu_actions(inp: IcuConfirmInput) -> dict:
    from cache import redis as cache

    # Flag critical vitals immediately -- safety-critical, no staging
    flagged = 0
    for vital_id in inp.critical_vital_ids:
        await repo.mark_observation_critical(vital_id)
        flagged += 1

    # Stage transfer_pending update for /commit
    assessments = inp.assessments if isinstance(inp.assessments, dict) else {}
    transfer_admission_ids = [
        c["admission_id"]
        for c in (
            assessments.get("step_down_candidates", []) +
            assessments.get("escalation_candidates", [])
        )
        if c.get("admission_id")
    ]
    await cache.stage(inp.session_id, "icu", {"transfer_admission_ids": transfer_admission_ids})

    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="icu_agent",
        event_type="icu_recommendations_staged",
        payload=inp.assessments,
    )
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_icu_confirm",
        "result": {
            "critical_vitals_flagged": flagged,
            "transfers_staged": len(transfer_admission_ids),
        },
    })
    logger.info("ICU staged  session=%s  flagged=%d  transfers_staged=%d",
                inp.session_id, flagged, len(transfer_admission_ids))
    return {"critical_vitals_flagged": flagged, "transfers_staged": len(transfer_admission_ids)}


# -- sa_icu_transfer ------------------------------------------------------------

@activity.defn
async def rank_icu_requests(inp: IcuTransferInput) -> dict:
    """Rank incoming ICU admission requests by clinical acuity."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_transfer"})
    beds = [Location.model_validate(b) for b in inp.available_beds]
    result = await rank_icu_admissions(inp.incoming_patients, beds)
    ranked = result.get("ranked_requests", [])
    out = {
        "ranked_requests":          ranked,
        "ventilator_dependent_count": sum(1 for r in ranked if r.get("ventilator_dependent")),
        "deterioration_risk_count":   sum(1 for r in ranked if r.get("deterioration_risk_high")),
    }
    await broadcast(inp.session_id, {
        "type": "sub_agent_completed", "sub_agent": "sa_icu_transfer",
        "result": {k: v for k, v in out.items() if k != "ranked_requests"},
    })
    logger.info("rank_icu_requests  session=%s  ranked=%d", inp.session_id, len(ranked))
    return out


@activity.defn
async def prioritize_ventilator_bed(inp: IcuAdmissionInput) -> dict:
    """Reorder ranked requests so ventilator-dependent patients are first."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_ventilator_priority"})
    vent = [r for r in inp.ranked_requests if r.get("ventilator_dependent")]
    non_vent = [r for r in inp.ranked_requests if not r.get("ventilator_dependent")]
    reordered = vent + non_vent
    if vent:
        await broadcast(inp.session_id, {
            "type": "alert", "severity": "critical",
            "message": f"Ventilator ICU bed prioritized: {len(vent)} ventilator-dependent patient(s)",
            "patients": [r.get("patient_token") for r in vent],
        })
    result = {"ventilator_priority_count": len(vent), "ranked_requests": reordered}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_ventilator_priority", "result": {"ventilator_priority_count": len(vent)}})
    logger.info("prioritize_ventilator_bed  session=%s  ventilator_patients=%d", inp.session_id, len(vent))
    return result


@activity.defn
async def reserve_icu_admission(inp: IcuAdmissionInput) -> dict:
    """Create an ICU admission approval for the top-ranked patient."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_reserve"})
    if not inp.ranked_requests:
        return {"approval_id": None, "patient_token": None}
    top = inp.ranked_requests[0]
    approval = await hasura.create_approval_task(
        session_id=inp.session_id,
        agent_id="icu_agent",
        action_type="icu_admission_request",
        payload={
            "patient_token": top.get("patient_token"),
            "rank":          top.get("rank", 1),
            "reason":        top.get("reason", ""),
            "ventilator_dependent": top.get("ventilator_dependent", False),
        },
        idempotency_key=make_idem_key(
            "icu_admission", inp.session_id, top.get("patient_token")),
    )
    await broadcast(inp.session_id, {
        "type": "approval_required",
        "approval_id": approval["id"],
        "action": "icu_admission_request",
        "patient_token": top.get("patient_token"),
        "reason": top.get("reason", ""),
        "ventilator_dependent": top.get("ventilator_dependent", False),
    })
    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="icu_agent",
        event_type="icu_admission_reserved",
        payload={"patient_token": top.get("patient_token"), "approval_id": approval["id"]},
    )
    result = {"approval_id": approval["id"], "patient_token": top.get("patient_token")}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_reserve", "result": result})
    logger.info("reserve_icu_admission  session=%s  patient=%s", inp.session_id, top.get("patient_token"))
    await start_escalating_approval(
        session_id=inp.session_id,
        approval_id=approval["id"],
        agent_id="icu_agent",
        action_type="icu_admission_request",
        payload={"patient_token": top.get("patient_token"), "rank": top.get("rank", 1),
                 "reason": top.get("reason", ""), "ventilator_dependent": top.get("ventilator_dependent", False)},
    )
    return result


@activity.defn
async def trigger_overflow_evaluation(inp: IcuAdmissionInput) -> dict:
    """Broadcast overflow alert when ICU is full and admission requests are pending."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_overflow"})
    pending = len(inp.ranked_requests)
    await broadcast(inp.session_id, {
        "type": "alert", "severity": "critical",
        "message": f"ICU at full capacity -- {pending} admission request(s) pending. Overflow evaluation triggered.",
        "patients": [r.get("patient_token") for r in inp.ranked_requests],
    })
    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="icu_agent",
        event_type="icu_overflow_evaluation_triggered",
        payload={"patients_pending": pending},
    )
    result = {"overflow_triggered": True, "patients_pending": pending}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_overflow", "result": result})
    logger.info("trigger_overflow_evaluation  session=%s  pending=%d", inp.session_id, pending)
    return result


@activity.defn
async def escalate_deterioration(inp: IcuAdmissionInput) -> dict:
    """Escalate high-deterioration-risk patients to immediate attention."""
    await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_deterioration"})
    high_risk = [r for r in inp.ranked_requests if r.get("deterioration_risk_high")]
    for patient in high_risk:
        await broadcast(inp.session_id, {
            "type": "alert", "severity": "critical",
            "message": f"HIGH DETERIORATION RISK: patient {patient.get('patient_token', 'unknown')} -- {patient.get('reason', '')}",
            "patient_token": patient.get("patient_token"),
        })
    if high_risk:
        await hasura.write_audit(
            session_id=inp.session_id,
            agent_id="icu_agent",
            event_type="deterioration_escalated",
            payload={"count": len(high_risk), "patients": [r.get("patient_token") for r in high_risk]},
        )
    result = {"escalated": len(high_risk)}
    await broadcast(inp.session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_deterioration", "result": result})
    logger.info("escalate_deterioration  session=%s  escalated=%d", inp.session_id, len(high_risk))
    return result


@activity.defn
async def forecast_icu_demand(session_id: str, goal: str = "") -> dict:
    """Forecast ICU admissions over a goal-derived horizon via /icu/demand.

    The inflow twin of forecast_icu_occupancy (which predicts census). Real signals:
    licensed ICU beds (/beds/summary) and ER admission pressure (/er/pressure, the
    upstream pool escalating into critical care). icu_admissions_last_24h has no
    trailing-window source -- ICU admission records carry no timestamp -- so the
    current ICU census stands in as a proxy. holiday_flag is 0 (no holiday calendar
    wired). Degrades to forecast_available: 0 when there is no ICU bed data or the
    service is unconfigured/down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_capacity_forecast"})

    beds_summary = await hasura.get_beds_summary() or {}
    total_icu = int(beds_summary.get("icu_total") or 0)
    if total_icu <= 0:
        result = {"forecast_available": 0, "reason": "no ICU bed data",
                  "predicted_admissions_24h": None, "capacity_alert": "unknown"}
        logger.info("forecast_icu_demand  session=%s  no ICU bed data", session_id)
        return result

    er_pressure = await hasura.get_er_pressure() or {}
    icu_admissions = await hasura.get_icu_admissions() or []
    er_per_day = er_pressure.get("est_admissions")
    if not er_per_day:
        er_per_day = len(await hasura.get_active_er_visits() or [])

    horizon = _demand_horizon(goal)
    payload = {
        "forecast_period":         horizon,
        "total_icu_beds":          int(_clamp(total_icu, 1, 500)),
        "er_admissions_per_day":   float(_clamp(float(er_per_day or 0), 0, 2200)),
        "icu_admissions_last_24h": float(_clamp(len(icu_admissions), 0, 160)),  # census proxy
        "holiday_flag":            0,   # no holiday calendar wired
    }

    forecast_resp = await forecast("/icu/demand", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "forecast service unavailable",
                  "predicted_admissions_24h": None, "capacity_alert": "unknown"}
        logger.info("forecast_icu_demand  session=%s  service unavailable", session_id)
        return result

    pred = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    if not isinstance(pred, dict):
        pred = forecast_resp if isinstance(forecast_resp, dict) else {}
    predicted = next((pred[k] for k in ("predicted_admissions_24h", "predicted_admissions",
                                        "predicted_demand", "value") if k in pred), None)
    capacity_alert = pred.get("capacity_alert") or pred.get("alert_level") or pred.get("level") or "unknown"
    action = pred.get("recommended_action") or pred.get("action") or ""

    if str(capacity_alert).lower() in ("elevated", "high"):
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": (f"ICU demand forecast: ~{predicted} admissions in next 24h "
                        f"(capacity_alert={capacity_alert}) — pre-plan overnight beds / anaesthetist cover."),
        })

    result = {
        "forecast_available":       1,
        "horizon":                  horizon,
        "predicted_admissions_24h": predicted,
        "capacity_alert":           capacity_alert,
        "recommended_action":       action,
        "fallback_used":            bool(forecast_resp.get("fallback_used")),
    }
    await broadcast(session_id, {"type": "sub_agent_completed",
                                 "sub_agent": "sa_icu_capacity_forecast", "result": result})
    logger.info("forecast_icu_demand  session=%s  predicted=%s  alert=%s",
                session_id, predicted, capacity_alert)
    return result


@activity.defn
async def forecast_icu_occupancy(session_id: str, goal: str = "") -> dict:
    """Forecast the ICU census (occupied/free beds + overflow risk) at a
    goal-derived horizon via the ML service (/icu/occupancy).

    The census twin of forecast_icu_demand (which predicts inflow): this predicts
    how full the ICU will BE. Real signals: total/occupied ICU beds (/beds/summary)
    and critical patients awaiting ICU (critical-vitals backlog). Staffing inputs
    (icu_doctors/nurses_on_duty) are optional and left to the model default -- we
    have no per-role ICU roster wired. Degrades to forecast_available: 0 when the
    service is unconfigured or down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_occupancy"})

    beds_summary = await hasura.get_beds_summary() or {}
    total = int(beds_summary.get("icu_total") or 0)
    if total <= 0:
        result = {"forecast_available": 0, "reason": "no ICU bed data"}
        logger.info("forecast_icu_occupancy  session=%s  no ICU bed data", session_id)
        return result

    occupied = beds_summary.get("icu_occupied")
    if occupied is None:
        occupied = len(await hasura.get_icu_admissions() or [])
    waiting = await hasura.get_critical_escalation_backlog()

    occupied_c = int(_clamp(int(occupied or 0), 0, total))
    step_down = int(beds_summary.get("available_beds") or 0)   # ward beds free to receive step-downs

    horizon = _horizon_from_goal(goal)
    payload = {
        "forecast_period":                   horizon,                               # REQUIRED
        "total_icu_beds":                    int(_clamp(total, 1, 500)),             # REQUIRED
        "occupied_icu_beds":                 occupied_c,                             # REQUIRED (anchor)
        "step_down_beds_available":          int(_clamp(step_down, 0, 140)),         # downstream blocker
        "ventilated_patients":               int(_clamp(round(0.43 * occupied_c), 0, occupied_c)),  # 43% census est.
        "critical_patients_waiting_for_icu": int(_clamp(int(waiting or 0), 0, 200)),
        "holiday_flag":                      0,                                      # no holiday calendar wired
        # No LOS / admission-rate feeds wired -> conservative documented defaults so
        # the model uses stable levers rather than degrading to its internal priors.
        "average_icu_stay_days":             4.0,
        "expected_icu_admissions":           2.0,
        "post_surgical_icu_admissions":      1.0,
    }

    forecast_resp = await forecast("/icu/occupancy", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_occupancy", "result": result})
        logger.info("forecast_icu_occupancy  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    # The model returns predicted_occupied_beds + thresholds; free beds, occupancy %
    # and status are derived here (the endpoint doesn't return them directly).
    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    th = (forecast_resp.get("thresholds_applied") or {}) if isinstance(forecast_resp, dict) else {}

    predicted_occupied = pred.get("predicted_occupied_beds")
    if isinstance(predicted_occupied, (int, float)):
        predicted_occupied = int(_clamp(predicted_occupied, 0, total))
        predicted_free = total - predicted_occupied
        occ_pct = round(predicted_occupied / total * 100, 1) if total > 0 else None
    else:
        predicted_free, occ_pct = None, None

    if occ_pct is None:
        status, overflow_risk = "unknown", "unknown"
    elif occ_pct >= th.get("status_full", 95):
        status, overflow_risk = "full", "high"
    elif occ_pct >= th.get("status_high", 85):
        status, overflow_risk = "high", "high"
    elif occ_pct >= th.get("status_busy", 70):
        status, overflow_risk = "busy", "medium"
    else:
        status, overflow_risk = "normal", "low"

    result = {
        "forecast_available":          1,
        "horizon":                     horizon,
        "predicted_occupancy_percent": occ_pct,
        "predicted_occupied_beds":     predicted_occupied,
        "predicted_free_beds":         predicted_free,
        "change_vs_now_beds":          (predicted_occupied - occupied_c) if isinstance(predicted_occupied, int) else None,
        "status":                      status,
        "overflow_risk":               overflow_risk,
        "recommended_action":          pred.get("recommended_action") or pred.get("action") or "",
        "fallback_used":               bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }

    if str(overflow_risk).lower() in ("medium", "high"):
        severity = "critical" if str(overflow_risk).lower() == "high" else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"ICU occupancy forecast ({horizon}): predicted {occ_pct}% occupied, "
                        f"overflow_risk={overflow_risk} — {result['recommended_action']}"),
        })

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_occupancy", "result": result})
    logger.info("forecast_icu_occupancy  session=%s  horizon=%s  occ=%s%%  risk=%s",
                session_id, horizon, occ_pct, overflow_risk)
    return result


# -- sa_icu_stepdown_demand ----------------------------------------------------

@activity.defn
async def forecast_icu_stepdown_demand(session_id: str, goal: str = "") -> dict:
    """Forecast ICU patients becoming ready for a ward (step-down) bed over a horizon
    via the ML service (/icu/stepdown-demand).

    DEPRECATED ENDPOINT: /icu/stepdown-demand is flagged deprecated by the forecast
    API in favour of /icu/transfer-forecast ("same quantity, rebuilt model") -- its
    live model is a hospital-size lookup table that largely ignores the request
    inputs. Integrated on explicit request; migrate to /icu/transfer-forecast when
    that endpoint's required capacity inputs become sourceable.

    Real signals: current ICU census and available general-ward beds (/beds/summary),
    ICU admissions (census proxy), general-ward occupancy and ward doctor/nurse
    staffing (proxies). patients_ready_for_transfer has no standalone source (the
    step-down-ready set only comes from the heavier ICU analysis path), so it is a
    documented ~20%-of-census proxy. Remaining inputs fall to model defaults. Degrades
    to forecast_available: 0 when there is no ICU bed data or the service is down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_stepdown_demand"})

    beds = await hasura.get_beds_summary() or {}
    total_icu = int(beds.get("icu_total") or 0)
    if total_icu <= 0:
        result = {"forecast_available": 0, "reason": "no ICU bed data"}
        logger.info("forecast_icu_stepdown_demand  session=%s  no ICU bed data", session_id)
        return result

    icu_adm = await hasura.get_icu_admissions() or []
    occupied = beds.get("icu_occupied")
    current_icu = int(_clamp(int(occupied if occupied is not None else len(icu_adm)), 0, 5000))
    ward_free = int(_clamp(int(beds.get("available_beds") or 0), 0, 100000))
    total_beds = int(beds.get("total_beds") or 0)
    occ_beds = int(beds.get("occupied_beds") or 0)
    ward_occ = round(occ_beds / total_beds * 100, 1) if total_beds > 0 else 0.0

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

    horizon = _horizon_from_goal(goal)
    if horizon == "3h":   # /icu/stepdown-demand enum has no 3h
        horizon = "6h"
    payload = {
        "forecast_period":            horizon,
        "current_icu_patients":       current_icu,
        # No standalone step-down-ready source -> documented ~20%-of-census proxy.
        "patients_ready_for_transfer": int(_clamp(round(0.2 * current_icu), 0, 5000)),
        "general_ward_available_beds": ward_free,
        "icu_admissions":             int(_clamp(len(icu_adm), 0, 5000)),
        "general_ward_occupancy":     round(_clamp(ward_occ, 0, 100), 1),
        "available_ward_doctors":     int(_clamp(doctors, 0, 5000)),
        "available_ward_nurses":      int(_clamp(nurses, 0, 5000)),
    }

    forecast_resp = await forecast("/icu/stepdown-demand", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_stepdown_demand", "result": result})
        logger.info("forecast_icu_stepdown_demand  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    # Envelope: flat dict with {"prediction": [{predicted_step_down_bed_demand,
    # recommended_action}], "thresholds_applied": {status_busy, status_near_full,
    # status_full, ...}}.
    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    demand = next((pred[k] for k in ("predicted_step_down_bed_demand", "predicted_stepdown_demand", "value") if k in pred), None)

    # The model returns no explicit status; derive a ward-absorption status from ward
    # occupancy vs the returned thresholds (busy/near_full/full are occupancy %s).
    th = (forecast_resp.get("thresholds_applied") or {}) if isinstance(forecast_resp, dict) else {}
    if ward_occ >= th.get("status_full", 100):
        ward_status = "full"
    elif ward_occ >= th.get("status_near_full", 85):
        ward_status = "near_full"
    elif ward_occ >= th.get("status_busy", 60):
        ward_status = "busy"
    else:
        ward_status = "normal"

    result = {
        "forecast_available":            1,
        "horizon":                       horizon,
        "predicted_step_down_bed_demand": demand,
        "ward_status":                   ward_status,
        "general_ward_available_beds":   ward_free,
        "general_ward_occupancy":        payload["general_ward_occupancy"],
        "recommended_action":            pred.get("recommended_action") or pred.get("action") or "",
        "deprecated_endpoint":           True,   # /icu/stepdown-demand -> migrate to /icu/transfer-forecast
        "fallback_used":                 bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }

    if ward_status in ("near_full", "full"):
        severity = "critical" if ward_status == "full" else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"ICU step-down forecast ({horizon}): ~{demand} beds of ward step-down demand, "
                        f"wards {ward_status} ({payload['general_ward_occupancy']}% occupied) — "
                        f"{result['recommended_action']}"),
        })

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_stepdown_demand", "result": result})
    logger.info("forecast_icu_stepdown_demand  session=%s  horizon=%s  demand=%s  ward=%s",
                session_id, horizon, demand, ward_status)
    return result


# -- sa_icu_ventilator_demand --------------------------------------------------

def _bed_ventilation(adm) -> str:
    """Ventilation status of an admission's bed ('full_ventilator'|'bipap'|...), lower-cased.
    Handles bed as a dict or a single-element list; '' when absent."""
    b = adm.get("bed")
    if isinstance(b, list):
        b = b[0] if b else {}
    return str((b or {}).get("ventilation") or "").lower() if isinstance(b, dict) else ""


@activity.defn
async def forecast_icu_ventilator_demand(session_id: str, goal: str = "") -> dict:
    """Forecast ventilators clinically required at t+horizon (incl. demand the current
    fleet cannot meet) via the ML service (/icu/ventilator-demand).

    Real signals: current ICU census, patients currently ON a ventilator (occupied ICU
    beds with ventilation == full_ventilator, from /admissions/icu), ventilator-capable
    beds free (/beds/available-icu), available ICU beds, ICU admissions and average LOS
    (from admitted_at). When occupied-bed ventilation isn't populated the on-ventilator
    count falls back to a documented ~43%-of-census estimate. Respiratory-diagnosis
    counts and external indices (outbreak/season/public-health) have no source and fall
    to model defaults; available_intensivists/nurses are model non-features and skipped.
    Degrades to forecast_available: 0 when there is no ICU bed data or the service is down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_ventilator_demand"})

    beds = await hasura.get_beds_summary() or {}
    total_icu = int(beds.get("icu_total") or 0)
    if total_icu <= 0:
        result = {"forecast_available": 0, "reason": "no ICU bed data"}
        logger.info("forecast_icu_ventilator_demand  session=%s  no ICU bed data", session_id)
        return result

    icu_adm = await hasura.get_icu_admissions() or []
    avail_icu = await hasura.get_available_icu_beds() or []
    occupied = beds.get("icu_occupied")
    current_icu = int(_clamp(int(occupied if occupied is not None else len(icu_adm)), 0, 5000))

    available_ventilators = sum(1 for b in avail_icu
                                if str((b or {}).get("ventilation") or "").lower() == "full_ventilator")
    on_vent_real = sum(1 for a in icu_adm if _bed_ventilation(a) in ("full_ventilator", "bipap"))
    # Fall back to a documented census-share estimate only when the occupied-bed
    # ventilation field isn't populated (real count 0 but ICU clearly not empty).
    current_on_vent = on_vent_real if on_vent_real > 0 else int(round(0.43 * current_icu))

    now = datetime.now(timezone.utc)
    los_days = []
    for a in icu_adm:
        raw = a.get("admitted_at") or ""
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        los_days.append((now - dt).total_seconds() / 86400.0)
    avg_los = round(sum(los_days) / len(los_days), 1) if los_days else 0.0

    horizon = _horizon_from_goal(goal)
    if horizon == "3h":   # /icu/ventilator-demand enum has no 3h
        horizon = "6h"
    payload = {
        "forecast_period":               horizon,
        "current_icu_patients":          current_icu,
        "current_patients_on_ventilator": int(_clamp(current_on_vent, 0, 5000)),
        "available_ventilators":         int(_clamp(available_ventilators, 0, 5000)),
        "icu_admissions":                int(_clamp(len(icu_adm), 0, 5000)),
        "available_icu_beds":            int(_clamp(max(total_icu - current_icu, 0), 0, 5000)),
    }
    if avg_los > 0:
        payload["icu_average_length_of_stay"] = round(_clamp(avg_los, 0, 365), 1)

    forecast_resp = await forecast("/icu/ventilator-demand", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_ventilator_demand", "result": result})
        logger.info("forecast_icu_ventilator_demand  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    demand = next((pred[k] for k in ("predicted_ventilator_demand", "predicted_demand", "value") if k in pred), None)

    # Shortfall = demand the current free fleet cannot meet.
    shortfall = None
    if isinstance(demand, (int, float)):
        shortfall = max(int(demand) - payload["available_ventilators"], 0)

    result = {
        "forecast_available":             1,
        "horizon":                        horizon,
        "predicted_ventilator_demand":    demand,
        "current_patients_on_ventilator": payload["current_patients_on_ventilator"],
        "available_ventilators":          payload["available_ventilators"],
        "unmet_ventilator_need":          shortfall,
        "recommended_action":             pred.get("recommended_action") or pred.get("action") or "",
        "fallback_used":                  bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }

    if shortfall and shortfall > 0:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": (f"Ventilator-demand forecast ({horizon}): ~{demand} needed vs "
                        f"{payload['available_ventilators']} free — {shortfall} short. "
                        f"{result['recommended_action']}"),
        })

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_ventilator_demand", "result": result})
    logger.info("forecast_icu_ventilator_demand  session=%s  horizon=%s  demand=%s  short=%s",
                session_id, horizon, demand, shortfall)
    return result


# -- sa_icu_los ----------------------------------------------------------------

@activity.defn
async def forecast_icu_los(session_id: str, goal: str = "") -> dict:
    """Forecast the average ICU length of stay (days) at t+horizon via the ML service
    (/icu/los-forecast), with a trend vs the current average.

    Real signals: current ICU census, the CURRENT average LOS (the model's anchor,
    computed from each ICU admission's admitted_at), ICU admissions, patients on a
    ventilator (occupied ICU beds with ventilation == full_ventilator), available ICU
    beds, ICU/hospital bed occupancy and total ICU beds. Per-diagnosis counts (sepsis /
    post-surgical / respiratory-failure), high-risk surgeries and external indices have
    no source and fall to model defaults; available_intensivists/nurses are model
    non-features and skipped. Degrades to forecast_available: 0 when there is no ICU bed
    data or the service is unconfigured/down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_los"})

    beds = await hasura.get_beds_summary() or {}
    total_icu = int(beds.get("icu_total") or 0)
    if total_icu <= 0:
        result = {"forecast_available": 0, "reason": "no ICU bed data"}
        logger.info("forecast_icu_los  session=%s  no ICU bed data", session_id)
        return result

    icu_adm = await hasura.get_icu_admissions() or []
    occupied = beds.get("icu_occupied")
    current_icu = int(_clamp(int(occupied if occupied is not None else len(icu_adm)), 0, 5000))
    on_vent = sum(1 for a in icu_adm if _bed_ventilation(a) in ("full_ventilator", "bipap"))

    now = datetime.now(timezone.utc)
    los_days = []
    for a in icu_adm:
        raw = a.get("admitted_at") or ""
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        los_days.append((now - dt).total_seconds() / 86400.0)
    # The anchor: real current mean LOS from admitted_at; documented fallback only if
    # no admission timestamp parses.
    avg_los = round(sum(los_days) / len(los_days), 1) if los_days else 4.5

    total_beds, occ_beds = int(beds.get("total_beds") or 0), int(beds.get("occupied_beds") or 0)
    hosp_occ = round(occ_beds / total_beds * 100, 1) if total_beds > 0 else 0.0

    horizon = _horizon_from_goal(goal)
    if horizon == "3h":   # /icu/los-forecast enum has no 3h
        horizon = "6h"
    payload = {
        "forecast_period":        horizon,
        "current_icu_patients":   current_icu,
        "average_current_los":    round(_clamp(avg_los, 0, 365), 1),
        "icu_admissions":         int(_clamp(len(icu_adm), 0, 5000)),
        "patients_on_ventilator": int(_clamp(on_vent, 0, 5000)),
        "available_icu_beds":     int(_clamp(max(total_icu - current_icu, 0), 0, 5000)),
        "icu_bed_occupancy":      round(current_icu / total_icu * 100, 1) if total_icu > 0 else 0.0,
        "total_icu_beds":         int(_clamp(total_icu, 1, 5000)),
        "hospital_bed_occupancy": round(_clamp(hosp_occ, 0, 100), 1),
    }

    forecast_resp = await forecast("/icu/los-forecast", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_los", "result": result})
        logger.info("forecast_icu_los  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    predicted_los = next((pred[k] for k in ("predicted_average_los", "predicted_los", "value") if k in pred), None)

    # Trend vs current average, using the model's trend_band (in days); flag an
    # extended-stay regime when predicted crosses extended_los_days.
    th = (forecast_resp.get("thresholds_applied") or {}) if isinstance(forecast_resp, dict) else {}
    band = th.get("trend_band", 0.15)
    extended_days = th.get("extended_los_days", 7.0)
    trend = "unknown"
    if isinstance(predicted_los, (int, float)):
        delta = predicted_los - avg_los
        trend = "rising" if delta > band else "falling" if delta < -band else "steady"
    extended = bool(isinstance(predicted_los, (int, float)) and predicted_los >= extended_days)

    result = {
        "forecast_available":   1,
        "horizon":              horizon,
        "predicted_average_los": predicted_los,
        "average_current_los":  payload["average_current_los"],
        "los_trend":            trend,
        "extended_los":         extended,
        "recommended_action":   pred.get("recommended_action") or pred.get("action") or "",
        "fallback_used":        bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }

    if extended or trend == "rising":
        severity = "warning" if extended else "info"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"ICU LOS forecast ({horizon}): predicted avg {predicted_los}d "
                        f"(vs {payload['average_current_los']}d now, {trend}) — "
                        f"{result['recommended_action']}"),
        })

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_los", "result": result})
    logger.info("forecast_icu_los  session=%s  horizon=%s  los=%s  trend=%s",
                session_id, horizon, predicted_los, trend)
    return result


# -- sa_icu_staffing_demand ----------------------------------------------------

@activity.defn
async def forecast_icu_staffing_demand(session_id: str, goal: str = "") -> dict:
    """Forecast the peak acuity-weighted ICU nurses required (plus hours likely
    short-staffed) over a horizon via the ML service (/icu/staffing-demand) -- the
    STAFFING slice only.

    Real signals: ICU census, patients on a ventilator (occupied ICU beds with an active
    ventilation status), ICU nurses rostered (roster icu+nurse) and average LOS (from
    admitted_at). peak_nurses_required_last_7_days has no history source so it is proxied
    from the current ICU nursing requirement (~1 nurse per 1.5 ICU patients);
    critical-care-certified share, nurse absenteeism, planned admissions, seasonal and
    holiday have no source and use documented defaults. Degrades to forecast_available: 0
    when there is no ICU bed data or the service is down.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_icu_staffing_demand"})

    beds = await hasura.get_beds_summary() or {}
    total_icu = int(beds.get("icu_total") or 0)
    if total_icu <= 0:
        result = {"forecast_available": 0, "reason": "no ICU bed data"}
        logger.info("forecast_icu_staffing_demand  session=%s  no ICU bed data", session_id)
        return result

    icu_adm = await hasura.get_icu_admissions() or []
    occupied = beds.get("icu_occupied")
    current_icu = int(_clamp(int(occupied if occupied is not None else len(icu_adm)), 0, 5000))
    on_vent = sum(1 for a in icu_adm if _bed_ventilation(a) in ("full_ventilator", "bipap"))

    now = datetime.now(timezone.utc)
    los_days = []
    for a in icu_adm:
        raw = a.get("admitted_at") or ""
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        los_days.append((now - dt).total_seconds() / 86400.0)
    avg_los = round(sum(los_days) / len(los_days), 1) if los_days else 4.5

    try:
        roster = await hasura.staff_list_roster(None)
    except Exception:  # noqa: BLE001
        roster = []
    icu_nurses = sum(int(r.get("headcount") or 0) for r in (roster or [])
                     if "nurse" in (t := f"{r.get('role') or ''} {r.get('area') or ''}".lower()) and "icu" in t)
    if icu_nurses == 0:   # no ICU-scoped roster label -> estimate from census (~1:1.5)
        icu_nurses = max(round(current_icu / 1.5), 0)
    # No historical nurse-requirement log -> proxy the recent peak with the current
    # acuity-based requirement (ventilated ~1:1, others ~1:1.5).
    peak_proxy = max(round(on_vent + (current_icu - on_vent) / 1.5), icu_nurses, 1)

    horizon = _horizon_from_goal(goal)
    if horizon == "3h":   # /icu/staffing-demand enum has no 3h
        horizon = "6h"
    payload = {
        "forecast_period":                horizon,
        "total_icu_beds":                 int(_clamp(total_icu, 1, 5000)),
        "current_icu_patients":           current_icu,
        "patients_on_ventilator":         int(_clamp(on_vent, 0, 5000)),
        "patients_ready_for_stepdown":    int(_clamp(round(0.2 * current_icu), 0, 5000)),
        "peak_nurses_required_last_7_days": int(_clamp(peak_proxy, 1, 5000)),   # proxy (no history)
        "nurses_rostered_in_period":      int(_clamp(icu_nurses, 0, 5000)),
        "critical_care_certified_share":  0.65,   # no certification-tracking source
        "nurse_absenteeism_rate":         0.05,   # no absence source
        "planned_icu_admissions_in_period": 0,    # no ICU pre-booking source
        "seasonal_illness_index":         0.5,    # no epidemiology source
        "holiday_flag":                   0,
    }

    forecast_resp = await forecast("/icu/staffing-demand", payload)
    if forecast_resp is None:
        result = {"forecast_available": 0, "reason": "service unavailable", "horizon": horizon}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_staffing_demand", "result": result})
        logger.info("forecast_icu_staffing_demand  session=%s  horizon=%s  service unavailable", session_id, horizon)
        return result

    preds = forecast_resp.get("prediction") if isinstance(forecast_resp, dict) else None
    pred = (preds[0] if isinstance(preds, list) and preds else preds if isinstance(preds, dict) else forecast_resp) or {}
    peak = next((pred[k] for k in ("predicted_peak_nurses_required", "predicted_peak_nurses", "value") if k in pred), None)
    short_hours = pred.get("hours_likely_short_staffed")
    gap = int(round(peak)) - icu_nurses if isinstance(peak, (int, float)) else None

    result = {
        "forecast_available":            1,
        "horizon":                       horizon,
        "predicted_peak_nurses_required": peak,
        "hours_likely_short_staffed":    short_hours,
        "nurses_rostered":               icu_nurses,
        "additional_nurses_required":    max(gap, 0) if isinstance(gap, int) else None,
        "recommended_action":            pred.get("recommended_action") or pred.get("action") or "",
        "fallback_used":                 bool(forecast_resp.get("fallback_used")) if isinstance(forecast_resp, dict) else False,
    }
    if (isinstance(gap, int) and gap > 0) or (isinstance(short_hours, (int, float)) and short_hours > 0):
        severity = "critical" if (isinstance(gap, int) and gap >= 3) else "warning"
        await broadcast(session_id, {
            "type": "alert", "severity": severity,
            "message": (f"ICU staffing-demand forecast ({horizon}): peak ~{peak} nurses vs {icu_nurses} rostered, "
                        f"{short_hours} short-staffed hour(s) — {result['recommended_action']}"),
        })
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_icu_staffing_demand", "result": result})
    logger.info("forecast_icu_staffing_demand  session=%s  horizon=%s  peak=%s  short=%s",
                session_id, horizon, peak, short_hours)
    return result
