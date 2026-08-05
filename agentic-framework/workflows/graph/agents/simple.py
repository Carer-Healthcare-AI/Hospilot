"""Non-approval agent bodies -- faithful ports of the corresponding Temporal
workflows. Mechanical change only: workflow.execute_activity(fn, X, timeout=,
retry=) -> await fn(X). Sub-agent planning / task gating / dynamic tasks use the
ported graph.planning helpers. Result dicts are byte-identical to the originals.
"""

import logging

from workflows.graph import patient
from workflows.graph.planning import should_run_task, plan_subagent, get_subagent_order, run_dynamic_tasks, seed_planned_slots, subagent_in_plan
from workflows.planner import SUB_AGENTS
from fhirgw.mappers.encounter import visit_to_fhir

from agents._shared.prefetch_activities import get_prefetch_cache, GetPrefetchInput
from agents.er.activities import (
    get_er_visits, triage_er_patients, save_triage_scores, route_fasttrack_patients,
    select_critical_patients, check_er_boarders, detect_cardiac_arrest, check_spo2_critical,
    detect_clinical_protocol, notify_specialist,
    ErTriageInput, ErSaveInput, ErFasttrackInput, SelectCriticalInput,
)
from agents.er.surge_prediction import (
    forecast_er_surge, forecast_er_wait_time, forecast_er_boarding, forecast_er_lwbs,
    forecast_er_congestion, forecast_ambulance_arrivals,
)
from agents.bed.prediction_activities import (
    get_capacity_snapshot, run_capacity_forecast, forecast_bed_turnover, forecast_bed_occupancy,
    forecast_bed_ward_capacity, forecast_bed_isolation_demand,
    BedForecastInput, CapacitySnapshotInput,
)
from agents.revenue.activities import (
    identify_revenue_leakage, optimize_package_utilization, analyze_resource_utilization,
    analyze_dept_profitability, predict_denial_risk_rev, presubmission_validation_rev,
    payer_rule_compliance_rev, detect_missing_docs_rev, escalation_recommendations_rev,
    forecast_revenue, forecast_claim_denial, forecast_claim_volume, forecast_collection,
    RevAnalysisInput,
)

logger = logging.getLogger(__name__)

# --- Execution seam ----------------------------------------------------------
# LangGraph orchestrates; Temporal executes. Rebind every imported Temporal
# activity so calls route through run_activity. Prefetch cache reads stay
# in-process. Call sites below are unchanged.
from functools import partial as _partial
from workflows.graph.agents._activity import run_activity as _run_activity
for _n, _f in list(globals().items()):
    if callable(_f) and hasattr(_f, "__temporal_activity_definition") and _n != "get_prefetch_cache":
        globals()[_n] = _partial(_run_activity, _f)

_ER_TASKS      = {sa.id: [t.schema() for t in sa.tasks] for sa in SUB_AGENTS.get("er_agent", [])}
_REVENUE_TASKS = {sa.id: [t.schema() for t in sa.tasks] for sa in SUB_AGENTS.get("revenue_agent", [])}


# -- ER ----------------------------------------------------------------------

def _visit_token(v) -> str | None:
    subj = v.get("subject") if isinstance(v, dict) else getattr(v, "subject", None)
    ref = subj.get("reference") if isinstance(subj, dict) else getattr(subj, "reference", None)
    return ref.split("/")[-1] if ref and "/" in ref else None


def _bound_encounter(pctx: dict):
    """An EMER Encounter for a bound incoming patient, so the real triage scorer rates
    them from their actual vitals (triage_er_patients fetches vitals by token). Built
    from the resolved token / current visit id."""
    return visit_to_fhir({
        "id":            pctx.get("current_visit_id") or pctx.get("token"),
        "patient_token": pctx.get("token"),
        "status":        "arrived",
    })


async def run_er_body(sid: str, ctx: dict) -> dict:
    goal = ctx.get("_goal", "")
    ta_results: dict = {}
    _raw_plan = ctx.get("_task_plan")
    task_plan: dict | None = dict(_raw_plan) if _raw_plan is not None else None
    if task_plan is not None and goal:
        seed_planned_slots(task_plan, _ER_TASKS)

    if task_plan is not None:
        for _sa in ("sa_er_triage", "sa_er_acuity_response", "sa_er_disposition",
                    "sa_er_boarding", "sa_er_surge_prediction", "sa_er_wait_time",
                    "sa_er_boarding_forecast", "sa_er_lwbs", "sa_er_congestion",
                    "sa_er_ambulance_arrivals"):
            await plan_subagent("er_agent", _sa, _ER_TASKS, task_plan, ta_results, goal, sid)

    # -- Surge prediction: forward-looking ML forecast of incoming ER arrivals ----
    # Runs independently of the triage spine (it reads recent arrival rate, not the
    # current queue), so it lives ahead of the "no active patients" early return.
    if subagent_in_plan("sa_er_surge_prediction", task_plan):
        if await should_run_task("ta_forecast_er_surge", "sa_er_surge_prediction", ta_results, task_plan, sid):
            ta_results["ta_forecast_er_surge"] = await forecast_er_surge(sid)

    # -- Wait-time forecast: predicted ER wait minutes + breach risk (queue + staffing).
    _er_wait_fc: dict = {}
    if subagent_in_plan("sa_er_wait_time", task_plan):
        if await should_run_task("ta_forecast_er_wait_time", "sa_er_wait_time", ta_results, task_plan, sid):
            ta_results["ta_forecast_er_wait_time"] = await forecast_er_wait_time(sid, goal)
            _er_wait_fc = {"er_wait_time_forecast": ta_results["ta_forecast_er_wait_time"]}

    if subagent_in_plan("sa_er_boarding_forecast", task_plan):
        if await should_run_task("ta_forecast_er_boarding", "sa_er_boarding_forecast", ta_results, task_plan, sid):
            ta_results["ta_forecast_er_boarding"] = await forecast_er_boarding(sid, goal)
            _er_wait_fc = {**_er_wait_fc, "er_boarding_forecast": ta_results["ta_forecast_er_boarding"]}

    if subagent_in_plan("sa_er_lwbs", task_plan):
        if await should_run_task("ta_forecast_er_lwbs", "sa_er_lwbs", ta_results, task_plan, sid):
            ta_results["ta_forecast_er_lwbs"] = await forecast_er_lwbs(sid, goal)
            _er_wait_fc = {**_er_wait_fc, "er_lwbs_forecast": ta_results["ta_forecast_er_lwbs"]}

    if subagent_in_plan("sa_er_congestion", task_plan):
        if await should_run_task("ta_forecast_er_congestion", "sa_er_congestion", ta_results, task_plan, sid):
            ta_results["ta_forecast_er_congestion"] = await forecast_er_congestion(sid, goal)
            _er_wait_fc = {**_er_wait_fc, "er_congestion_forecast": ta_results["ta_forecast_er_congestion"]}

    if subagent_in_plan("sa_er_ambulance_arrivals", task_plan):
        if await should_run_task("ta_forecast_ambulance_arrivals", "sa_er_ambulance_arrivals", ta_results, task_plan, sid):
            ta_results["ta_forecast_ambulance_arrivals"] = await forecast_ambulance_arrivals(sid, goal)
            _er_wait_fc = {**_er_wait_fc, "ambulance_arrival_forecast": ta_results["ta_forecast_ambulance_arrivals"]}

    triage: list = []
    fasttrack: dict = {"fasttrack_candidates": []}
    save_result: dict = {"saved": 0, "critical": 0}
    critical_patients: list = []
    if subagent_in_plan("sa_er_triage", task_plan):
        if await should_run_task("ta_get_er_visits", "sa_er_triage", ta_results, task_plan, sid):
            cached = await get_prefetch_cache(GetPrefetchInput(session_id=sid, task_id="ta_get_er_visits"))
            ta_results["ta_get_er_visits"] = cached if cached else {"visits": (await get_er_visits(sid)) or []}

    visits = (ta_results.get("ta_get_er_visits") or {}).get("visits") or []

    # Bind the flow to its incoming patient(s): identity is established upstream by
    # patient_verification_agent (single identification point), so read the resolved
    # contexts from cache. Prepend them as EMER encounters so the real triage scorer
    # rates them from their actual vitals -- prepended (not appended) so they survive
    # triage's [:20] cap. Empty when no incoming patient was identified.
    bound: list = []
    bound_count = 0
    if subagent_in_plan("sa_er_triage", task_plan):
        bound = await patient.get_cached(sid)
        if bound:
            existing = {_visit_token(v) for v in visits}
            stubs = [_bound_encounter(p) for p in bound if p.get("token") not in existing]
            bound_count = len(stubs)
            visits = stubs + visits

    if not visits:
        return {"status": "completed", "message": "No active ER patients",
                **({"er_surge_forecast": ta_results["ta_forecast_er_surge"]}
                   if "ta_forecast_er_surge" in ta_results else {}),
                **_er_wait_fc}

    # -- Triage spine: pull the queue, score it, persist (always-run unit) -------
    if subagent_in_plan("sa_er_triage", task_plan):
        if await should_run_task("ta_triage_patients", "sa_er_triage", ta_results, task_plan, sid):
            triage = await triage_er_patients(ErTriageInput(session_id=sid, visits=visits))
            t = triage or []
            ta_results["ta_triage_patients"] = {
                "triage": t,
                "triaged": len(t) - bound_count,
                "ctas1": sum(1 for x in t if x.get("score") == 1),
                "ctas2": sum(1 for x in t if x.get("score") == 2),
                "critical": sum(1 for x in t if x.get("score", 5) <= 2),
                "spo2_critical_count": sum(1 for x in t if x.get("spo2_critical")),
                "protocol_flags_count": sum(1 for x in t if x.get("protocol") not in (None, "none")),
                "specialist_needed_count": sum(1 for x in t if x.get("needs_specialist")),
                # Disposition gating counts -- CTAS 4-5 are fast-track candidates,
                # CTAS 1-3 are admission candidates (see ta_route_fasttrack / ta_select_critical).
                "fasttrack_count": sum(1 for x in t if x.get("score") in (4, 5)),
                "admission_candidate_count": sum(1 for x in t if x.get("score", 5) <= 3),
            }

        triage = (ta_results.get("ta_triage_patients") or {}).get("triage") or []

        if await should_run_task("ta_save_triage_scores", "sa_er_triage", ta_results, task_plan, sid):
            save_result = await save_triage_scores(ErSaveInput(session_id=sid, triage_results=triage))
            ta_results["ta_save_triage_scores"] = save_result
        else:
            save_result = {"saved": len(triage), "critical": 0}

    # -- Acuity response: reactive emergency handling, gated on triage flags -----
    if subagent_in_plan("sa_er_acuity_response", task_plan):
        if await should_run_task("ta_detect_cardiac_arrest", "sa_er_acuity_response", ta_results, task_plan, sid):
            ta_results["ta_detect_cardiac_arrest"] = await detect_cardiac_arrest(
                ErFasttrackInput(session_id=sid, triage_results=triage))

        if await should_run_task("ta_check_spo2_critical", "sa_er_acuity_response", ta_results, task_plan, sid):
            ta_results["ta_check_spo2_critical"] = await check_spo2_critical(
                ErFasttrackInput(session_id=sid, triage_results=triage))

        if await should_run_task("ta_detect_clinical_protocol", "sa_er_acuity_response", ta_results, task_plan, sid):
            ta_results["ta_detect_clinical_protocol"] = await detect_clinical_protocol(
                ErFasttrackInput(session_id=sid, triage_results=triage))

        if await should_run_task("ta_notify_specialist", "sa_er_acuity_response", ta_results, task_plan, sid):
            ta_results["ta_notify_specialist"] = await notify_specialist(
                ErFasttrackInput(session_id=sid, triage_results=triage))

    # -- Disposition: where triaged patients go next -----------------------------
    if subagent_in_plan("sa_er_disposition", task_plan):
        if await should_run_task("ta_route_fasttrack", "sa_er_disposition", ta_results, task_plan, sid):
            fasttrack = await route_fasttrack_patients(ErFasttrackInput(session_id=sid, triage_results=triage))
            ta_results["ta_route_fasttrack"] = fasttrack
        else:
            fasttrack = {"fasttrack_candidates": []}

        if await should_run_task("ta_select_critical", "sa_er_disposition", ta_results, task_plan, sid):
            critical_patients = await select_critical_patients(
                SelectCriticalInput(session_id=sid, triage_results=triage, n=20))
            ta_results["ta_select_critical"] = {"critical_patients": critical_patients or []}
        else:
            critical_patients = []

    if subagent_in_plan("sa_er_boarding", task_plan):
        if await should_run_task("ta_check_er_boarders", "sa_er_boarding", ta_results, task_plan, sid):
            ta_results["ta_check_er_boarders"] = await check_er_boarders(sid)

    fasttrack_candidates = fasttrack.get("fasttrack_candidates", [])

    _dynamic = await run_dynamic_tasks("er_agent", task_plan, ta_results, sid)
    _triage_meta = ta_results.get("ta_triage_patients") or {}
    return {
        "status": "completed",
        "triaged": save_result.get("saved", 0) - bound_count,
        "critical": _triage_meta.get("critical", save_result.get("critical", 0)),
        "fasttrack_candidates": fasttrack_candidates,
        "critical_patients": critical_patients,
        "results": triage,
        **({"er_surge_forecast": ta_results["ta_forecast_er_surge"]}
           if "ta_forecast_er_surge" in ta_results else {}),
        **_er_wait_fc,
        **(_dynamic and {"dynamic_tasks": _dynamic} or {}),
    }




# -- Patient Verification ------------------------------------------------------

async def run_patient_verification_body(sid: str, ctx: dict) -> dict:
    """Establish incoming patient identity as an explicit, planner-placed first node.

    sa_patient_identification:
      ta_identify_patients     (always)   -- pause for the incoming patient(s)' mobile
                                             number(s), resolve token + demographics +
                                             current_visit_id + latest vitals (Fabric).
      ta_flag_unknown_patients (uc > 0)   -- alert on incoming patient(s) with no record.

    sa_patient_registration (uc > 0):
      ta_register_patient                 -- request registration of the unknown
                                             patient(s) via Fabric (-> DB side, created
                                             MANUALLY by hospital staff), PAUSE the flow
                                             until Fabric reports the new record(s) back
                                             (Kafka `patient` data event -> resume), then
                                             rebind the now-known contexts. A long-timeout
                                             reaper escalates if staff never register them.
      Gated on the runtime unknown_count (the "patient not found" trigger), not on the
      planner -- it is an always-on safety step like ta_identify_patients.

    Downstream agents (ER / ICU ranking / bed reservation) read patient.get_cached(sid)
    and no longer self-prompt; this node is the single identification point.
    """
    _raw_plan = ctx.get("_task_plan")
    task_plan: dict | None = dict(_raw_plan) if _raw_plan is not None else None
    ta_results: dict = {}
    from api.routes.ws import broadcast

    # ta_identify_patients -- raises the patient_identification interrupt on first run,
    # returns the resolved (cached) contexts on resume. Inline (not a Temporal activity:
    # it raises a LangGraph interrupt). Populates session_patient:{sid} for consumers.
    await broadcast(sid, {"type": "sub_agent_started", "sub_agent": "sa_patient_identification"})
    bound = await patient.require_patients(
        sid, prompt="Provide the mobile number(s) of the incoming patient(s).")
    unknown_count = sum(1 for p in bound if not p.get("known_patient"))
    ta_results["ta_identify_patients"] = {
        "verified_count": len(bound),
        "unknown_count":  unknown_count,
        "patients": [
            {"token": p.get("token"), "patient_name": p.get("patient_name"),
             "known_patient": p.get("known_patient", False)}
            for p in bound
        ],
    }
    await broadcast(sid, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_patient_identification",
        "result": ta_results["ta_identify_patients"],
    })

    # ta_flag_unknown_patients -- conditional on ta_identify_patients.unknown_count > 0.
    if await should_run_task("ta_flag_unknown_patients", "sa_patient_identification",
                             ta_results, task_plan, sid):
        if unknown_count > 0:
            from api.routes.ws import broadcast
            await broadcast(sid, {
                "type": "alert", "severity": "warning",
                "message": (f"{unknown_count} incoming patient(s) have no record in the system "
                            f"-- provisional identity created; verify before clinical actions."),
            })
        ta_results["ta_flag_unknown_patients"] = {"flagged": unknown_count}

    # sa_patient_registration / ta_register_patient -- trigger when ta_identify_patients
    # found incoming patient(s) with no DB record. Requests their registration via Fabric
    # and PAUSES the flow (patient.register_patients raises a LangGraph interrupt) until
    # Fabric reports the new record(s) back -- on resume `bound` is the re-resolved,
    # now-known context set. Inline (raises an interrupt) and gated purely on the runtime
    # unknown_count, exactly like ta_identify_patients.
    registered_count = 0
    if unknown_count > 0:
        await broadcast(sid, {"type": "sub_agent_started", "sub_agent": "sa_patient_registration"})
        requested = unknown_count
        bound = await patient.register_patients(
            sid, [p for p in bound if not p.get("known_patient")])  # pauses here
        unknown_count = sum(1 for p in bound if not p.get("known_patient"))
        registered_count = requested - unknown_count
        ta_results["ta_register_patient"] = {
            "requested":     requested,
            "registered":    registered_count,
            "still_unknown": unknown_count,
            "patients": [
                {"token": p.get("token"), "patient_name": p.get("patient_name"),
                 "known_patient": p.get("known_patient", False)}
                for p in bound
            ],
        }
        await broadcast(sid, {
            "type": "sub_agent_completed",
            "sub_agent": "sa_patient_registration",
            "result": ta_results["ta_register_patient"],
        })

    _dynamic = await run_dynamic_tasks("patient_verification_agent", task_plan, ta_results, sid)
    return {
        "status": "completed",
        "agent_id": "patient_verification_agent",
        "verified_count": len(bound),
        "unknown_count": unknown_count,
        "registered_count": registered_count,
        "flagged": ta_results.get("ta_flag_unknown_patients", {}).get("flagged", 0),
        **(_dynamic and {"dynamic_tasks": _dynamic} or {}),
    }


# -- Bed Prediction ----------------------------------------------------------

async def run_bed_prediction_body(sid: str, ctx: dict) -> dict:
    task_plan: dict = ctx.get("_task_plan", {})
    ta_results: dict = {}

    if await should_run_task("ta_get_capacity_snapshot", "sa_bed_pred_census", ta_results, task_plan):
        cached = await get_prefetch_cache(GetPrefetchInput(session_id=sid, task_id="ta_get_capacity_snapshot"))
        snapshot = cached if cached else await get_capacity_snapshot(
            CapacitySnapshotInput(session_id=sid, context=ctx))
        ta_results["ta_get_capacity_snapshot"] = snapshot

    snapshot = ta_results.get("ta_get_capacity_snapshot", {})

    if await should_run_task("ta_run_capacity_forecast", "sa_bed_pred_forecast", ta_results, task_plan):
        ta_results["ta_run_capacity_forecast"] = await run_capacity_forecast(
            BedForecastInput(session_id=sid, snapshot=snapshot))

    # Per-ward ML forecast of beds freeing next shift (independent of the narrative
    # forecast above; reads live occupancy + cleaning backlog, not the snapshot).
    if await should_run_task("ta_forecast_bed_turnover", "sa_bed_turnover", ta_results, task_plan):
        ta_results["ta_forecast_bed_turnover"] = await forecast_bed_turnover(sid, ctx.get("_goal", ""))

    # Whole-hospital forward census forecast at a goal-derived horizon (independent
    # of the per-ward turnover forecast above; reads live census, not the snapshot).
    if await should_run_task("ta_forecast_bed_occupancy", "sa_bed_occupancy", ta_results, task_plan):
        ta_results["ta_forecast_bed_occupancy"] = await forecast_bed_occupancy(sid, ctx.get("_goal", ""))

    # Per-ward capacity/utilisation forecast (per-ward sibling of occupancy/turnover).
    if await should_run_task("ta_forecast_bed_ward_capacity", "sa_bed_ward_capacity", ta_results, task_plan):
        ta_results["ta_forecast_bed_ward_capacity"] = await forecast_bed_ward_capacity(sid, ctx.get("_goal", ""))

    if await should_run_task("ta_forecast_bed_isolation_demand", "sa_bed_isolation_demand", ta_results, task_plan):
        ta_results["ta_forecast_bed_isolation_demand"] = await forecast_bed_isolation_demand(sid, ctx.get("_goal", ""))

    result = ta_results.get("ta_run_capacity_forecast", {})
    return {
        "status": "completed",
        "overflow_risk": result.get("overflow_risk"),
        "icu_risk": result.get("icu_risk"),
        "beds_freeing_4h": result.get("beds_freeing_4h"),
        "beds_freeing_24h": result.get("beds_freeing_24h"),
        "beds_needed": result.get("beds_needed"),
        "icu_saturation_pct": result.get("icu_saturation_pct"),
        "forecast": result.get("forecast"),
        "recommended_actions": result.get("recommended_actions", []),
        **({"bed_turnover_forecast": ta_results["ta_forecast_bed_turnover"]}
           if "ta_forecast_bed_turnover" in ta_results else {}),
        **({"bed_occupancy_forecast": ta_results["ta_forecast_bed_occupancy"]}
           if "ta_forecast_bed_occupancy" in ta_results else {}),
        **({"bed_ward_capacity_forecast": ta_results["ta_forecast_bed_ward_capacity"]}
           if "ta_forecast_bed_ward_capacity" in ta_results else {}),
        **({"bed_isolation_demand_forecast": ta_results["ta_forecast_bed_isolation_demand"]}
           if "ta_forecast_bed_isolation_demand" in ta_results else {}),
    }


# -- Revenue -------------------------------------------------------------------

async def run_revenue_body(sid: str, ctx: dict) -> dict:
    # task_type = ctx.get("_task_type", "")  # (revenue/billing split 2026-06) billing task_types moved to run_billing_body
    goal = ctx.get("_goal", "")
    ta_results: dict = {}
    _raw_plan = ctx.get("_task_plan")
    task_plan: dict | None = dict(_raw_plan) if _raw_plan is not None else None
    if task_plan is not None and goal:
        seed_planned_slots(task_plan, _REVENUE_TASKS)

    # Forward-looking organization-wide revenue forecast (INR); independent of the
    # leakage/denial analytics, surfaced in the return dict.
    _rev_fc: dict = {}
    if task_plan is not None:
        await plan_subagent("revenue_agent", "sa_rev_forecast", _REVENUE_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_rev_forecast", task_plan) and await should_run_task(
            "ta_forecast_revenue", "sa_rev_forecast", ta_results, task_plan, sid):
        ta_results["ta_forecast_revenue"] = await forecast_revenue(sid, goal)
        _rev_fc = {"revenue_forecast": ta_results["ta_forecast_revenue"]}

    # Forward-looking insurance claim-denial-rate forecast (% + denied-value %).
    if task_plan is not None:
        await plan_subagent("revenue_agent", "sa_rev_claim_denial", _REVENUE_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_rev_claim_denial", task_plan) and await should_run_task(
            "ta_forecast_claim_denial", "sa_rev_claim_denial", ta_results, task_plan, sid):
        ta_results["ta_forecast_claim_denial"] = await forecast_claim_denial(sid, goal)
        _rev_fc = {**_rev_fc, "claim_denial_forecast": ta_results["ta_forecast_claim_denial"]}

    # Forward-looking insurance claim-submission-volume forecast (+ per-staff load).
    if task_plan is not None:
        await plan_subagent("revenue_agent", "sa_rev_claim_volume", _REVENUE_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_rev_claim_volume", task_plan) and await should_run_task(
            "ta_forecast_claim_volume", "sa_rev_claim_volume", ta_results, task_plan, sid):
        ta_results["ta_forecast_claim_volume"] = await forecast_claim_volume(sid, goal)
        _rev_fc = {**_rev_fc, "claim_volume_forecast": ta_results["ta_forecast_claim_volume"]}

    # Forward-looking cash-collection forecast (payments actually collected).
    if task_plan is not None:
        await plan_subagent("revenue_agent", "sa_rev_collection", _REVENUE_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_rev_collection", task_plan) and await should_run_task(
            "ta_forecast_collection", "sa_rev_collection", ta_results, task_plan, sid):
        ta_results["ta_forecast_collection"] = await forecast_collection(sid, goal)
        _rev_fc = {**_rev_fc, "collection_forecast": ta_results["ta_forecast_collection"]}

    # (revenue/billing split 2026-06) patient_billing / initiate_billing task_types
    # are now handled by run_billing_body -- billing_agent executes billing ops.
    # Commented out rather than deleted -- remove once stable.
    # if task_type == "patient_billing":
    #     if task_plan is not None:
    #         await plan_subagent("revenue_agent", "sa_rev_patient_billing", {}, task_plan, ta_results, goal, sid)
    #     if await should_run_task("ta_get_patient_invoices", "sa_rev_patient_billing", ta_results, task_plan, sid):
    #         ta_results["ta_get_patient_invoices"] = await get_patient_billing(
    #             PatientBillingInput(session_id=sid, goal=goal))
    #     return {"status": "completed", "mode": "patient_billing", **ta_results.get("ta_get_patient_invoices", {})}
    #
    # if task_type == "initiate_billing":
    #     if task_plan is not None:
    #         await plan_subagent("revenue_agent", "sa_rev_initiate_billing", {}, task_plan, ta_results, goal, sid)
    #     if await should_run_task("ta_create_billing_request", "sa_rev_initiate_billing", ta_results, task_plan, sid):
    #         ta_results["ta_create_billing_request"] = await create_billing_request(
    #             InitiateBillingInput(session_id=sid, goal=goal))
    #     return {"status": "completed", "mode": "initiate_billing", "agent_id": "revenue_agent",
    #             **ta_results.get("ta_create_billing_request", {})}

    if task_plan is not None:
        await plan_subagent("revenue_agent", "sa_rev_optimization", {}, task_plan, ta_results, goal, sid)

    c_optimization = await get_prefetch_cache(GetPrefetchInput(session_id=sid, task_id="ta_identify_revenue_leakage"))

    if subagent_in_plan("sa_rev_optimization", task_plan):
        if await should_run_task("ta_identify_revenue_leakage", "sa_rev_optimization", ta_results, task_plan, sid):
            ta_results["ta_identify_revenue_leakage"] = c_optimization or await identify_revenue_leakage(sid)
        if await should_run_task("ta_optimize_package_utilization", "sa_rev_optimization", ta_results, task_plan, sid):
            ta_results["ta_optimize_package_utilization"] = await optimize_package_utilization(
                RevAnalysisInput(session_id=sid, goal=goal))
        if await should_run_task("ta_analyze_resource_utilization", "sa_rev_optimization", ta_results, task_plan, sid):
            ta_results["ta_analyze_resource_utilization"] = await analyze_resource_utilization(sid)
        if await should_run_task("ta_analyze_dept_profitability", "sa_rev_optimization", ta_results, task_plan, sid):
            ta_results["ta_analyze_dept_profitability"] = await analyze_dept_profitability(sid)

    if task_plan is not None:
        await plan_subagent("revenue_agent", "sa_rev_denial_prevention", {}, task_plan, ta_results, goal, sid)

    if subagent_in_plan("sa_rev_denial_prevention", task_plan):
        if await should_run_task("ta_predict_denial_risk_rev", "sa_rev_denial_prevention", ta_results, task_plan, sid):
            ta_results["ta_predict_denial_risk_rev"] = await predict_denial_risk_rev(sid)
        if await should_run_task("ta_presubmission_validation_rev", "sa_rev_denial_prevention", ta_results, task_plan, sid):
            ta_results["ta_presubmission_validation_rev"] = await presubmission_validation_rev(sid)
        if await should_run_task("ta_payer_rule_compliance_rev", "sa_rev_denial_prevention", ta_results, task_plan, sid):
            ta_results["ta_payer_rule_compliance_rev"] = await payer_rule_compliance_rev(sid)
        if await should_run_task("ta_detect_missing_docs_rev", "sa_rev_denial_prevention", ta_results, task_plan, sid):
            ta_results["ta_detect_missing_docs_rev"] = await detect_missing_docs_rev(sid)
        if await should_run_task("ta_escalation_recommendations_rev", "sa_rev_denial_prevention", ta_results, task_plan, sid):
            ta_results["ta_escalation_recommendations_rev"] = await escalation_recommendations_rev(
                RevAnalysisInput(session_id=sid, goal=goal))

    # (revenue/billing split 2026-06) sa_rev_initiate_billing dispatch moved to
    # run_billing_body. Commented out rather than deleted -- remove once stable.
    # if task_plan is not None:
    #     await plan_subagent("revenue_agent", "sa_rev_initiate_billing", {}, task_plan, ta_results, goal, sid)
    #
    # if subagent_in_plan("sa_rev_initiate_billing", task_plan):
    #     if await should_run_task("ta_create_billing_request", "sa_rev_initiate_billing", ta_results, task_plan, sid):
    #         ta_results["ta_create_billing_request"] = await create_billing_request(
    #             InitiateBillingInput(session_id=sid, goal=goal))

    optimization = ta_results.get("ta_identify_revenue_leakage", {})
    denial = ta_results.get("ta_predict_denial_risk_rev", {})
    # billing = ta_results.get("ta_create_billing_request", {})  # (split) now billing_agent
    _dynamic = await run_dynamic_tasks("revenue_agent", task_plan, ta_results, sid)
    return {
        "status": "completed",
        "agent_id": "revenue_agent",
        # (revenue/billing split 2026-06) billing_requests now surfaced by run_billing_body:
        # **({"billing_requests": {
        #     "patient_count":   billing.get("patient_count", 0),
        #     "patients_billed": billing.get("patients_billed", []),
        #     "status":          billing.get("status", ""),
        # }} if billing else {}),
        "optimization": {
            "leakage_amount": optimization.get("leakage_amount", 0),
            "unbilled_count": optimization.get("unbilled_count", 0),
            "utilization_score": (ta_results.get("ta_analyze_resource_utilization") or {}).get("utilization_score", 0),
            "below_target_depts": (ta_results.get("ta_analyze_dept_profitability") or {}).get("below_target_count", 0),
            "savings_identified": (ta_results.get("ta_optimize_package_utilization") or {}).get("savings_identified", 0),
        },
        "denial_prevention": {
            "high_risk_count": denial.get("high_risk_count", 0),
            "validation_issues": (ta_results.get("ta_presubmission_validation_rev") or {}).get("issues_found", 0),
            "compliance_issues": (ta_results.get("ta_payer_rule_compliance_rev") or {}).get("compliance_issues", 0),
            "missing_docs": (ta_results.get("ta_detect_missing_docs_rev") or {}).get("missing_docs_count", 0),
            "escalation_count": (ta_results.get("ta_escalation_recommendations_rev") or {}).get("escalation_count", 0),
        },
        **_rev_fc,
        **(_dynamic and {"dynamic_tasks": _dynamic} or {}),
    }


