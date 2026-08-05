"""Approval agent bodies: ICU, Discharge, Staff.

Each replaces the Temporal `decide` signal + wait_condition with the resume-aware
HITL pattern (see graph.hitl): on first run the body does its work, creates the
Hasura approval row, saves a small pending record to Redis, then calls
interrupt() (raises, suspending the graph). On resume the node re-runs, takes the
pending branch, interrupt() returns the decision, and the body finalises
(confirm on approval; no-op/return on reject/timeout) -- without repeating work or
re-creating the approval.
"""

import logging

from workflows.graph import hitl, patient
from workflows.graph.step_rec import emit_step_recommendation
from workflows.graph.planning import should_run_task, plan_subagent, get_subagent_order, run_dynamic_tasks, seed_planned_slots, subagent_in_plan
from workflows.planner import SUB_AGENTS

from agents._shared.prefetch_activities import get_prefetch_cache, GetPrefetchInput
from agents.icu.activities import (
    get_icu_census, analyze_icu_status, create_icu_approval, confirm_icu_actions,
    forecast_icu_demand, forecast_icu_occupancy, forecast_icu_stepdown_demand,
    forecast_icu_ventilator_demand, forecast_icu_los, forecast_icu_staffing_demand,
    rank_icu_requests, prioritize_ventilator_bed, reserve_icu_admission,
    trigger_overflow_evaluation, escalate_deterioration,
    IcuAnalysisInput, IcuApprovalInput, IcuConfirmInput, IcuTransferInput, IcuAdmissionInput,
)
from agents.staff.activities import (
    get_ward_workload, get_hourly_workload, get_area_staffing, get_documentation_gaps,
    analyze_staff_workload, create_staff_approval, confirm_staff_recommendation, requested_staff_areas,
    forecast_nurse_demand, forecast_doctor_demand,
    forecast_shift_coverage, forecast_overtime, forecast_absenteeism, forecast_workforce_utilization,
    forecast_skill_mix,
    StaffAnalysisInput, StaffApprovalInput, StaffConfirmInput, AreaStaffingInput,
)

logger = logging.getLogger(__name__)

# --- Execution seam ----------------------------------------------------------
# LangGraph orchestrates; Temporal executes. Rebind every imported Temporal
# activity so calls route through run_activity (durable, retried on the worker).
# Prefetch cache reads stay in-process. Call sites below are unchanged.
from functools import partial as _partial
from workflows.graph.agents._activity import run_activity as _run_activity
for _n, _f in list(globals().items()):
    if callable(_f) and hasattr(_f, "__temporal_activity_definition") and _n != "get_prefetch_cache":
        globals()[_n] = _partial(_run_activity, _f)

_ICU_TASKS       = {sa.id: [t.schema() for t in sa.tasks] for sa in SUB_AGENTS.get("icu_agent", [])}


def _enc_token(enc: dict) -> str | None:
    ref = ((enc or {}).get("subject") or {}).get("reference")
    return ref.split("/")[-1] if ref and "/" in ref else None


def _enc_bed_id(enc: dict) -> str | None:
    locs = (enc or {}).get("location") or []
    if not locs:
        return None
    ref = ((locs[0] or {}).get("location") or {}).get("reference")
    return ref.split("/")[-1] if ref and "/" in ref else None


# -- ICU -----------------------------------------------------------------------

async def _icu_finalize(sid: str, pending: dict, decision: str) -> dict:
    v = pending["vars"]
    step_down = v["step_down"]
    escalations_enriched = v["escalations_enriched"]
    icu_available = v["icu_available"]

    if decision != "approved":
        status = "timeout" if decision == "timeout" else "rejected"
        out = {
            "status": status,
            "icu_full": icu_available == 0,
            "step_down": len(step_down),
            "escalations": len(escalations_enriched),
        }
        if status == "timeout":
            out["error"] = "Approval timed out after 30 min"
        return out

    ta_results = pending["ta_results"]
    task_plan = pending["task_plan"]
    analysis = v["analysis"]
    confirm: dict = {}
    if await should_run_task("ta_confirm_icu_actions", "sa_icu_stepdown", ta_results, task_plan, sid):
        confirm = await confirm_icu_actions(IcuConfirmInput(
            session_id=sid,
            critical_vital_ids=v["critical_vital_ids"],
            assessments={**analysis, "step_down_candidates": step_down, "escalation_candidates": escalations_enriched},
        ))
        ta_results["ta_confirm_icu_actions"] = confirm

    _dynamic = await run_dynamic_tasks("icu_agent", task_plan, ta_results, sid)
    return {
        "status": "completed",
        "icu_occupied": v["icu_occupied"],
        "icu_available": icu_available,
        "icu_full": icu_available == 0,
        "step_down_recommended": len(step_down),
        "escalations_recommended": len(escalations_enriched),
        "critical_vitals_flagged": confirm.get("critical_vitals_flagged", 0),
        "summary": analysis.get("summary", ""),
        "step_down_candidates": step_down,
        "escalation_candidates": escalations_enriched,
        "icu_transfer": ta_results.get("ta_rank_icu_requests") or {},
        "transfer_reserved": ta_results.get("ta_reserve_icu_admission") or {},
        **(_dynamic and {"dynamic_tasks": _dynamic} or {}),
    }


async def run_icu_body(sid: str, ctx: dict) -> dict:
    base = "icu_agent"
    pending = await hitl.load_pending(sid, base)
    if pending is not None:
        decision = hitl.await_decision({"kind": "icu_approval", "session_id": sid, "agent_id": base})
        await hitl.clear_pending(sid, base)
        return await _icu_finalize(sid, pending, decision)

    task_type = ctx.get("_task_type", "")
    goal = ctx.get("_goal", "").lower()
    ta_results: dict = {}
    _raw_plan = ctx.get("_task_plan")
    task_plan: dict | None = dict(_raw_plan) if _raw_plan is not None else None
    if task_plan is not None and goal:
        seed_planned_slots(task_plan, _ICU_TASKS)

    # Forward-looking ICU demand forecast; runs ahead of the census short-circuits.
    _icu_fc: dict = {}
    if task_plan is not None:
        await plan_subagent("icu_agent", "sa_icu_capacity_forecast", {}, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_icu_capacity_forecast", task_plan) and await should_run_task(
            "ta_forecast_icu_demand", "sa_icu_capacity_forecast", ta_results, task_plan, sid):
        ta_results["ta_forecast_icu_demand"] = await forecast_icu_demand(sid, goal)
        _icu_fc = {"icu_demand_forecast": ta_results["ta_forecast_icu_demand"]}

    # Forward-looking ICU census forecast (occupied/free beds + overflow risk) at a
    # goal-derived horizon; the census twin of the demand forecast above.
    if task_plan is not None:
        await plan_subagent("icu_agent", "sa_icu_occupancy", {}, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_icu_occupancy", task_plan) and await should_run_task(
            "ta_forecast_icu_occupancy", "sa_icu_occupancy", ta_results, task_plan, sid):
        ta_results["ta_forecast_icu_occupancy"] = await forecast_icu_occupancy(sid, goal)
        _icu_fc["icu_occupancy_forecast"] = ta_results["ta_forecast_icu_occupancy"]

    # Forward-looking ICU-to-ward step-down demand forecast (deprecated endpoint).
    if task_plan is not None:
        await plan_subagent("icu_agent", "sa_icu_stepdown_demand", {}, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_icu_stepdown_demand", task_plan) and await should_run_task(
            "ta_forecast_icu_stepdown_demand", "sa_icu_stepdown_demand", ta_results, task_plan, sid):
        ta_results["ta_forecast_icu_stepdown_demand"] = await forecast_icu_stepdown_demand(sid, goal)
        _icu_fc["icu_stepdown_demand_forecast"] = ta_results["ta_forecast_icu_stepdown_demand"]

    # Forward-looking ICU ventilator-demand forecast (need + unmet shortfall).
    if task_plan is not None:
        await plan_subagent("icu_agent", "sa_icu_ventilator_demand", {}, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_icu_ventilator_demand", task_plan) and await should_run_task(
            "ta_forecast_icu_ventilator_demand", "sa_icu_ventilator_demand", ta_results, task_plan, sid):
        ta_results["ta_forecast_icu_ventilator_demand"] = await forecast_icu_ventilator_demand(sid, goal)
        _icu_fc["icu_ventilator_demand_forecast"] = ta_results["ta_forecast_icu_ventilator_demand"]

    # Forward-looking ICU length-of-stay forecast (avg LOS days + trend).
    if task_plan is not None:
        await plan_subagent("icu_agent", "sa_icu_los", {}, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_icu_los", task_plan) and await should_run_task(
            "ta_forecast_icu_los", "sa_icu_los", ta_results, task_plan, sid):
        ta_results["ta_forecast_icu_los"] = await forecast_icu_los(sid, goal)
        _icu_fc["icu_los_forecast"] = ta_results["ta_forecast_icu_los"]

    # Forward-looking ICU nurse-staffing-demand forecast (peak nurses + short hours).
    if task_plan is not None:
        await plan_subagent("icu_agent", "sa_icu_staffing_demand", {}, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_icu_staffing_demand", task_plan) and await should_run_task(
            "ta_forecast_icu_staffing_demand", "sa_icu_staffing_demand", ta_results, task_plan, sid):
        ta_results["ta_forecast_icu_staffing_demand"] = await forecast_icu_staffing_demand(sid, goal)
        _icu_fc["icu_staffing_demand_forecast"] = ta_results["ta_forecast_icu_staffing_demand"]

    _CAPACITY_WORDS = ("space", "capacity", "available", "fit", "accommodate",
                       "how many", "room for", "beds available", "can icu")
    _TRANSFER_WORDS = ("step down", "step-down", "transfer", "move",
                       "optimize", "optimise", "free up", "escalat")
    is_capacity_check = task_type == "capacity_check" or (
        not task_type and any(w in goal for w in _CAPACITY_WORDS) and not any(w in goal for w in _TRANSFER_WORDS)
    )

    if task_plan is not None:
        await plan_subagent("icu_agent", "sa_icu_census", {}, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_icu_census", task_plan) and await should_run_task("ta_get_icu_census", "sa_icu_census", ta_results, task_plan, sid):
        cached = await get_prefetch_cache(GetPrefetchInput(session_id=sid, task_id="ta_get_icu_census"))
        census = cached if cached else await get_icu_census(sid)
        ta_results["ta_get_icu_census"] = census

    census = ta_results.get("ta_get_icu_census", {})
    icu_admissions = census.get("icu_admissions", [])
    non_icu_admissions = census.get("non_icu_admissions", [])
    available_beds = census.get("available_beds", [])

    if not icu_admissions and not non_icu_admissions:
        return {"status": "completed", "message": "No active admissions found", "icu_full": False, **_icu_fc}

    # Capacity-only short-circuit -- but NOT when the plan also selected ICU ranking
    # (sa_icu_transfer). A goal like "check ICU beds available AND rank this admission"
    # needs both; falling through lets the ranking section bind + rank the incoming
    # patient instead of stopping at a bed count.
    rank_planned = task_plan is not None and subagent_in_plan("sa_icu_transfer", task_plan, ta_results)
    if is_capacity_check and not rank_planned:
        n = len(available_beds)
        msg = (f"{n} ICU beds currently available" if n > 0
               else "ICU at full capacity -- step-down evaluation may be needed to free space")
        return {
            "status": "completed", "mode": "capacity_check",
            "icu_occupied": len(icu_admissions), "icu_available": n,
            "icu_full": n == 0, "message": msg, **_icu_fc,
        }

    if ta_results.get("ta_get_icu_census") is not None:
        ta_results["ta_get_icu_census"]["icu_available"] = len(available_beds)

    if task_plan is not None:
        await plan_subagent("icu_agent", "sa_icu_transfer", {}, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_icu_transfer", task_plan, ta_results):
        # Existing pending ICU requests surfaced upstream by ER triage.
        er_requests = [
            p for p in (ctx.get("er_agent") or {}).get("critical_patients", [])
            if p.get("bed_type_needed") == "ICU"
        ]
        # The flow's incoming patient(s): identity is established upstream by
        # patient_verification_agent (single identification point); read from cache and
        # rank them ALONGSIDE the existing requests (UNION, deduped by token) -- this is
        # "rank THIS admission against pending ICU requests", not either/or.
        bound = await patient.get_cached(sid)
        seen = {p.get("patient_token") for p in er_requests}
        incoming_icu_patients = er_requests + [
            {**p, "bed_type_needed": "ICU"} for p in bound
            if p.get("patient_token") not in seen
        ]
        if not incoming_icu_patients:
            logger.info("SKIP  %-28s  reason=no_incoming_icu_patients", "sa_icu_transfer")
        else:
            if await should_run_task("ta_rank_icu_requests", "sa_icu_transfer", ta_results, task_plan, sid):
                ta_results["ta_rank_icu_requests"] = await rank_icu_requests(
                    IcuTransferInput(session_id=sid, incoming_patients=incoming_icu_patients, available_beds=available_beds))
            ranked_requests = (ta_results.get("ta_rank_icu_requests") or {}).get("ranked_requests", [])
            if await should_run_task("ta_prioritize_ventilator_bed", "sa_icu_transfer", ta_results, task_plan, sid):
                result = await prioritize_ventilator_bed(IcuAdmissionInput(session_id=sid, ranked_requests=ranked_requests))
                ta_results["ta_prioritize_ventilator_bed"] = result
                ranked_requests = result.get("ranked_requests", ranked_requests)
            if await should_run_task("ta_reserve_icu_admission", "sa_icu_transfer", ta_results, task_plan, sid):
                ta_results["ta_reserve_icu_admission"] = await reserve_icu_admission(
                    IcuAdmissionInput(session_id=sid, ranked_requests=ranked_requests))
            if await should_run_task("ta_trigger_overflow_evaluation", "sa_icu_transfer", ta_results, task_plan, sid):
                ta_results["ta_trigger_overflow_evaluation"] = await trigger_overflow_evaluation(
                    IcuAdmissionInput(session_id=sid, ranked_requests=ranked_requests))
            if await should_run_task("ta_escalate_deterioration", "sa_icu_transfer", ta_results, task_plan, sid):
                ta_results["ta_escalate_deterioration"] = await escalate_deterioration(
                    IcuAdmissionInput(session_id=sid, ranked_requests=ranked_requests))

    if task_plan is not None:
        await plan_subagent("icu_agent", "sa_icu_stepdown", {}, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_icu_stepdown", task_plan) and await should_run_task("ta_analyze_icu_status", "sa_icu_stepdown", ta_results, task_plan, sid):
        ta_results["ta_analyze_icu_status"] = await analyze_icu_status(IcuAnalysisInput(
            session_id=sid, icu_admissions=icu_admissions, non_icu_admissions=non_icu_admissions,
            available_beds=available_beds, bed_by_id=census.get("bed_by_id", {})))

    analysis = ta_results.get("ta_analyze_icu_status", {})
    step_down_raw = analysis.get("step_down_candidates", [])
    escalations = analysis.get("escalation_candidates", [])
    critical_vital_ids = analysis.get("critical_vital_ids", [])
    vitals_by_id = analysis.get("vitals_by_admission_id", {})

    id_to_icu_admission = {a["id"][:8]: a for a in icu_admissions}
    id_to_non_icu_admission = {a["id"][:8]: a for a in non_icu_admissions}

    step_down = []
    for sd in step_down_raw:
        prefix = (sd.get("admission_id") or "")[:8]
        admission = id_to_icu_admission.get(prefix, {})
        full_id = admission.get("id") or sd.get("admission_id")
        step_down.append({
            **sd, "admission_id": full_id, "patient_token": _enc_token(admission),
            "source_bed_id": _enc_bed_id(admission), "bed_type_needed": "General",
            "chief_complaint": sd.get("reason", "ICU step-down"), "triage_score": 3,
            "vitals": vitals_by_id.get(full_id),
        })

    escalations_enriched = []
    for esc in escalations:
        prefix = (esc.get("admission_id") or "")[:8]
        admission = id_to_non_icu_admission.get(prefix, {})
        full_id = admission.get("id") or esc.get("admission_id")
        escalations_enriched.append({
            **esc, "admission_id": full_id, "patient_token": _enc_token(admission),
            "source_bed_id": _enc_bed_id(admission), "bed_type_needed": "ICU",
            "chief_complaint": esc.get("reason", "ICU escalation"), "triage_score": 1,
            "vitals": vitals_by_id.get(full_id),
        })

    if not step_down and not escalations_enriched:
        if critical_vital_ids:
            await confirm_icu_actions(IcuConfirmInput(
                session_id=sid, critical_vital_ids=critical_vital_ids, assessments=analysis))
        return {
            "status": "completed", "message": "No transfers recommended",
            "critical_vitals_flagged": len(critical_vital_ids), "summary": analysis.get("summary", ""),
            "icu_occupied": len(icu_admissions), "icu_available": len(available_beds),
            "icu_full": len(available_beds) == 0, **_icu_fc,
        }

    if await should_run_task("ta_create_icu_approval", "sa_icu_stepdown", ta_results, task_plan, sid):
        await create_icu_approval(IcuApprovalInput(
            session_id=sid, step_down_candidates=step_down, escalation_candidates=escalations_enriched,
            summary=analysis.get("summary", "")))
        ta_results["ta_create_icu_approval"] = {"created": True}

    if not ta_results.get("ta_create_icu_approval"):
        return {
            "status": "completed", "message": "No transfers recommended",
            "icu_occupied": len(icu_admissions), "icu_available": len(available_beds),
            "icu_full": len(available_beds) == 0,
            "step_down_candidates": step_down, "escalation_candidates": escalations_enriched, **_icu_fc,
        }

    await hitl.save_pending(sid, base, {
        "ta_results": ta_results,
        "task_plan": task_plan,
        "vars": {
            "step_down": step_down, "escalations_enriched": escalations_enriched,
            "critical_vital_ids": critical_vital_ids, "analysis": analysis,
            "icu_occupied": len(icu_admissions), "icu_available": len(available_beds),
        },
    })
    await emit_step_recommendation(
        sid, agent_id=base, kind="icu_stepdown",
        headline=(f"Step down {len(step_down)} ICU patient(s)"
                  + (f", escalate {len(escalations_enriched)}" if escalations_enriched else "")),
        actions=([f"Step-down transfer for {len(step_down)} stable ICU patient(s)"] if step_down else [])
                + ([f"Escalate {len(escalations_enriched)} deteriorating patient(s) to ICU"]
                   if escalations_enriched else []),
        rationale=analysis.get("summary", ""),
        risk="high" if escalations_enriched else "medium",
    )
    hitl.await_decision({"kind": "icu_approval", "session_id": sid, "agent_id": base,
                         "action_type": "icu_stepdown",
                         "risk": "high" if escalations_enriched else "medium"})  # raises on first run


# -- Staff ---------------------------------------------------------------------

def _staff_upstream_signal(ctx: dict) -> dict:
    """G10: read upstream agent results already placed in ctx by build_ctx so the
    staffing assessment can factor *expected* / *actual* patient load (Q2: "re-balance
    staff based on the expected actual patient load"). Mirrors the bed.py cohort-reading
    pattern (ctx.get(upstream_agent_id, {})). Never raises -- absent upstream = neutral.

    Returns {expected_noshows, er_critical, overflow_risk, surge, sources} where `surge`
    is a coarse "demand is elevated" flag used to keep workload_ok conservative.
    """
    appt_ta = ((ctx.get("appointment_agent") or {}).get("ta_results") or {})
    noshow = appt_ta.get("ta_appt_predict_noshow") or {}
    expected_noshows = int(noshow.get("high_risk_count") or 0)

    bed_pred = ctx.get("bed_prediction_agent") or {}
    overflow_risk = bool(bed_pred.get("overflow_risk"))

    er = ctx.get("er_agent") or {}
    er_critical = int(er.get("critical") or 0) or len(er.get("critical_patients") or [])

    sources = [k for k in ("appointment_agent", "bed_prediction_agent", "er_agent") if ctx.get(k)]
    return {
        "expected_noshows": expected_noshows,
        "er_critical": er_critical,
        "overflow_risk": overflow_risk,
        "surge": overflow_risk or er_critical > 0,
        "sources": sources,
    }


def _workload_ok(analysis: dict, recommendations: list, signal: dict) -> bool:
    """G5: derive an executable boolean gate from the analysis. Nurses are "within
    workload" when no ward is flagged high-pressure, no reallocation is recommended,
    and no upstream surge signal raises expected demand."""
    return (not analysis.get("high_pressure_wards")
            and not recommendations
            and not signal.get("surge"))


async def _staff_finalize(sid: str, pending: dict, decision: str) -> dict:
    v = pending["vars"]
    analysis = v["analysis"]
    recommendations = v["recommendations"]
    signal = v.get("signal", {})
    extras = v.get("extras", {})

    if decision != "approved":
        status = "timeout" if decision == "timeout" else "rejected"
        out = {"status": status, "workload_ok": False,
               "high_pressure_wards": analysis.get("high_pressure_wards", []), **extras}
        if status == "timeout":
            out["error"] = "Approval timed out after 30 min"
        else:
            out["recommendations"] = len(recommendations)
        return out

    ta_results = pending["ta_results"]
    task_plan = pending["task_plan"]
    result: dict = {}
    if await should_run_task("ta_confirm_staff_recommendation", "sa_float_pool", ta_results, task_plan):
        result = await confirm_staff_recommendation(StaffConfirmInput(session_id=sid, analysis=analysis))
        ta_results["ta_confirm_staff_recommendation"] = result

    return {
        "status": "completed",
        "recommendations": result.get("recommendations", len(recommendations)),
        "high_pressure_wards": analysis.get("high_pressure_wards", []),
        "summary": analysis.get("summary", ""),
        # Reallocation was recommended -> some ward was over workload. Gate stays
        # False here; once the float-pool moves are confirmed the ward is being
        # actively rebalanced, but it was NOT already within workload.
        "workload_ok": _workload_ok(analysis, recommendations, signal),
        **extras,
    }


async def run_staff_body(sid: str, ctx: dict) -> dict:
    base = "staff_agent"
    pending = await hitl.load_pending(sid, base)
    if pending is not None:
        decision = hitl.await_decision({"kind": "staff_approval", "session_id": sid, "agent_id": base})
        await hitl.clear_pending(sid, base)
        return await _staff_finalize(sid, pending, decision)

    task_plan: dict = ctx.get("_task_plan", {})
    ta_results: dict = {}

    # G10: factor expected/actual patient load from upstream agents (no-show
    # prediction, bed-prediction surge, ER criticals) into the assessment + gate.
    signal = _staff_upstream_signal(ctx)
    if signal["sources"]:
        logger.info("staff upstream load  session=%s  %s", sid, signal)

    if subagent_in_plan("sa_ratio_monitor", task_plan) and await should_run_task("ta_get_ward_workload", "sa_ratio_monitor", ta_results, task_plan):
        cached = await get_prefetch_cache(GetPrefetchInput(session_id=sid, task_id="ta_get_ward_workload"))
        ta_results["ta_get_ward_workload"] = cached if cached else {"workload": (await get_ward_workload(sid)) or []}

    # G15: hour-bucketed task load -> peak/understaffed hours, threaded downstream
    # (appointment reschedule, G14) via the staff result. Quiet no-op if not planned.
    if subagent_in_plan("sa_ratio_monitor", task_plan) and await should_run_task("ta_get_hourly_workload", "sa_ratio_monitor", ta_results, task_plan):
        ta_results["ta_get_hourly_workload"] = await get_hourly_workload(sid)
    hourly = ta_results.get("ta_get_hourly_workload", {})

    # G11/G20/G24/G28/lab-staff: staffing for non-inpatient-nursing areas (front
    # desk, phlebotomy, OT, recovery/PACU, lab) the ward model can't see. Scoped to
    # the area(s) the goal names (empty -> all areas).
    if subagent_in_plan("sa_ratio_monitor", task_plan) and await should_run_task("ta_get_area_staffing", "sa_ratio_monitor", ta_results, task_plan):
        ta_results["ta_get_area_staffing"] = await get_area_staffing(
            AreaStaffingInput(session_id=sid, areas=requested_staff_areas(ctx.get("_goal", ""))))
    area_staffing = ta_results.get("ta_get_area_staffing", {})

    # G37: staffing documentation gaps (missing/overdue care notes, charting, records).
    if subagent_in_plan("sa_ratio_monitor", task_plan) and await should_run_task("ta_check_documentation_gaps", "sa_ratio_monitor", ta_results, task_plan):
        ta_results["ta_check_documentation_gaps"] = await get_documentation_gaps(sid)
    doc_gaps = ta_results.get("ta_check_documentation_gaps", {})

    # Forward-looking nurse-demand forecast; independent of the ward-workload path,
    # so it is folded into `extras` (spread into every return below).
    _nurse_fc = None
    if subagent_in_plan("sa_nurse_demand", task_plan) and await should_run_task(
            "ta_forecast_nurse_demand", "sa_nurse_demand", ta_results, task_plan):
        ta_results["ta_forecast_nurse_demand"] = await forecast_nurse_demand(sid, ctx.get("_goal", ""))
        _nurse_fc = ta_results["ta_forecast_nurse_demand"]

    _doctor_fc = None
    if subagent_in_plan("sa_doctor_demand", task_plan) and await should_run_task(
            "ta_forecast_doctor_demand", "sa_doctor_demand", ta_results, task_plan):
        ta_results["ta_forecast_doctor_demand"] = await forecast_doctor_demand(sid, ctx.get("_goal", ""))
        _doctor_fc = ta_results["ta_forecast_doctor_demand"]

    _shift_cov_fc = None
    if subagent_in_plan("sa_shift_coverage", task_plan) and await should_run_task(
            "ta_forecast_shift_coverage", "sa_shift_coverage", ta_results, task_plan):
        ta_results["ta_forecast_shift_coverage"] = await forecast_shift_coverage(sid, ctx.get("_goal", ""))
        _shift_cov_fc = ta_results["ta_forecast_shift_coverage"]

    _overtime_fc = None
    if subagent_in_plan("sa_overtime_forecast", task_plan) and await should_run_task(
            "ta_forecast_overtime", "sa_overtime_forecast", ta_results, task_plan):
        ta_results["ta_forecast_overtime"] = await forecast_overtime(sid, ctx.get("_goal", ""))
        _overtime_fc = ta_results["ta_forecast_overtime"]

    _absenteeism_fc = None
    if subagent_in_plan("sa_absenteeism_forecast", task_plan) and await should_run_task(
            "ta_forecast_absenteeism", "sa_absenteeism_forecast", ta_results, task_plan):
        ta_results["ta_forecast_absenteeism"] = await forecast_absenteeism(sid, ctx.get("_goal", ""))
        _absenteeism_fc = ta_results["ta_forecast_absenteeism"]

    _workforce_fc = None
    if subagent_in_plan("sa_workforce_utilization", task_plan) and await should_run_task(
            "ta_forecast_workforce_utilization", "sa_workforce_utilization", ta_results, task_plan):
        ta_results["ta_forecast_workforce_utilization"] = await forecast_workforce_utilization(sid, ctx.get("_goal", ""))
        _workforce_fc = ta_results["ta_forecast_workforce_utilization"]

    _skill_mix_fc = None
    if subagent_in_plan("sa_skill_mix", task_plan) and await should_run_task(
            "ta_forecast_skill_mix", "sa_skill_mix", ta_results, task_plan):
        ta_results["ta_forecast_skill_mix"] = await forecast_skill_mix(sid, ctx.get("_goal", ""))
        _skill_mix_fc = ta_results["ta_forecast_skill_mix"]

    # Common extras attached to every staff result (cross-agent + UI consumers).
    extras = {
        "upstream_load": signal,
        "peak_understaffed_hours": hourly.get("understaffed_hours", []),
        "hourly_workload": hourly,
        "area_staffing": area_staffing,
        "understaffed_areas": area_staffing.get("understaffed_areas", []),
        "documentation_gaps": doc_gaps,
        **({"nurse_demand_forecast": _nurse_fc} if _nurse_fc else {}),
        **({"doctor_demand_forecast": _doctor_fc} if _doctor_fc else {}),
        **({"shift_coverage_forecast": _shift_cov_fc} if _shift_cov_fc else {}),
        **({"overtime_forecast": _overtime_fc} if _overtime_fc else {}),
        **({"absenteeism_forecast": _absenteeism_fc} if _absenteeism_fc else {}),
        **({"workforce_utilization_forecast": _workforce_fc} if _workforce_fc else {}),
        **({"skill_mix_forecast": _skill_mix_fc} if _skill_mix_fc else {}),
    }

    workload_entry = ta_results.get("ta_get_ward_workload", {})
    if "workload" not in workload_entry and "wards" not in workload_entry:
        # No inpatient ward data -- but area staffing (front desk/phlebotomy/etc.) may
        # still answer the goal, so surface it rather than a bare "no data".
        return {"status": "completed", "message": "No ward data available", **extras}

    if "ta_analyze_staff_workload" not in ta_results:
        if subagent_in_plan("sa_ratio_monitor", task_plan) and await should_run_task("ta_analyze_staff_workload", "sa_ratio_monitor", ta_results, task_plan):
            workload_list = workload_entry.get("workload", [])
            if not workload_list:
                return {"status": "completed", "message": "No ward data available", **extras}
            ta_results["ta_analyze_staff_workload"] = await analyze_staff_workload(
                StaffAnalysisInput(session_id=sid, ward_workload=workload_list))

    analysis = ta_results.get("ta_analyze_staff_workload", {})
    recommendations = analysis.get("recommendations", [])
    if not recommendations:
        return {"status": "completed", "message": "No reallocation needed",
                "high_pressure_wards": analysis.get("high_pressure_wards", []),
                "summary": analysis.get("summary", ""),
                "workload_ok": _workload_ok(analysis, recommendations, signal),
                **extras}

    # G20: skip float pool when context reports no nurses available.
    # float_available comes from the HIS session context via the staff_agent manifest.
    # Default True so a missing field never silently blocks deployment.
    if not ctx.get("float_available", True):
        return {"status": "completed", "message": "Float pool is empty — no nurses available",
                "high_pressure_wards": analysis.get("high_pressure_wards", []),
                "summary": analysis.get("summary", ""),
                "workload_ok": _workload_ok(analysis, recommendations, signal),
                **extras}

    if subagent_in_plan("sa_float_pool", task_plan) and await should_run_task("ta_create_staff_approval", "sa_float_pool", ta_results, task_plan):
        await create_staff_approval(StaffApprovalInput(
            session_id=sid, recommendations=recommendations,
            high_pressure_wards=analysis.get("high_pressure_wards", []),
            summary=analysis.get("summary", "")))
        ta_results["ta_create_staff_approval"] = {"created": True}

    if not ta_results.get("ta_create_staff_approval"):
        return {"status": "completed", "message": "No reallocation needed",
                "high_pressure_wards": analysis.get("high_pressure_wards", []),
                "summary": analysis.get("summary", ""),
                "workload_ok": _workload_ok(analysis, recommendations, signal),
                **extras}

    await hitl.save_pending(sid, base, {
        "ta_results": ta_results, "task_plan": task_plan,
        "vars": {"analysis": analysis, "recommendations": recommendations, "signal": signal,
                 "extras": extras},
    })
    _high_pressure = analysis.get("high_pressure_wards", [])
    await emit_step_recommendation(
        sid, agent_id=base, kind="staff_reallocation",
        headline=f"Reallocate staff across {len(_high_pressure)} high-pressure ward(s)",
        actions=[
            (f"Move staff from {r.get('from_ward')} to {r.get('to_ward')}"
             if isinstance(r, dict) and r.get("from_ward") and r.get("to_ward")
             else (r.get("reason") if isinstance(r, dict) else str(r)))
            for r in (recommendations or [])[:3]
        ],
        rationale=analysis.get("summary", ""),
        risk="medium",
    )
    hitl.await_decision({"kind": "staff_approval", "session_id": sid, "agent_id": base,
                         "action_type": "staff_reallocation", "risk": "medium"})
