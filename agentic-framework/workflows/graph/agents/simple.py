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
from agents.revenue.activities import (
    identify_revenue_leakage, optimize_package_utilization, analyze_resource_utilization,
    analyze_dept_profitability, predict_denial_risk_rev, presubmission_validation_rev,
    payer_rule_compliance_rev, detect_missing_docs_rev, escalation_recommendations_rev,
    RevAnalysisInput,
)
from agents.lab.sample_prioritization import (
    check_stat_status, apply_icu_er_priority, check_analyzer_available, escalate_tat_risk,
)
from agents.lab.sample_tracking import (
    check_sample_collection, check_sample_transport, verify_sample_receipt, trigger_sample_search,
)
from agents.lab.tat import (
    check_tat_threshold, analyze_tat_bottleneck, prioritize_stat_queue, escalate_tat_supervisor,
)
from agents.lab.analyzer_utilization import (
    check_analyzer_utilization, identify_alternate_analyzer,
    rebalance_analyzer_workload, trigger_maintenance_alert,
)
from agents.lab.analyzer_routing import (
    check_analyzer_overload, validate_alternate_analyzer,
    execute_sample_routing, restore_routing_capacity,
)
from agents.lab.quality_control import (
    check_qc_status, trigger_recalibration, repeat_qc_check, compliance_alert,
)
from agents.lab.test_validation import (
    validate_result_rules, check_delta_flag, check_critical_value_flag, release_validated_report,
)
from agents.lab.critical_result import (
    detect_critical_results, notify_physician_critical,
    escalate_icu_er_critical, log_critical_action,
)
from agents.lab.test_recommendation import (
    detect_abnormal_result, evaluate_reflex_rules,
    recommend_additional_test, create_reflex_order,
)
from agents.pharmacy.prioritization import (
    check_stat_medication_orders, apply_critical_patient_priority,
    check_stat_availability, escalate_stat_shortage,
)
from agents.pharmacy.fulfillment import (
    check_prescription_received, check_medication_availability,
    track_dispensing_progress, close_fulfilled_orders,
)
from agents.pharmacy.drug_availability import (
    check_stock_levels, search_alternate_location,
    reserve_inventory, escalate_critical_shortage,
)
from agents.pharmacy.prescription_validation import (
    validate_prescription_completeness, validate_dosage_range,
    detect_duplicate_medications, approve_or_hold_prescription,
)
from agents.pharmacy.clinical_interaction import (
    check_polypharmacy, run_interaction_check,
    check_allergy_conflict, approve_safe_dispense,
)
from agents.pharmacy.dispensing_validation import (
    verify_patient_identity, match_medication_prescription,
    validate_dispensing_dosage, release_or_hold_dispensing,
)
from agents.pharmacy.substitution import (
    check_unavailable_medications, search_formulary_alternatives,
    request_physician_approval, update_substitution_order,
)
from agents.pharmacy.queue import (
    check_queue_length, analyze_queue_bottleneck,
    prioritize_stat_medications, escalate_tat_breach,
)
from agents.pharmacy.controlled_drug import (
    identify_controlled_orders, verify_controlled_authorization,
    check_inventory_variance, escalate_compliance_issue,
)
from agents.pharmacy.activities import (
    get_discharge_ready_patients, check_medication_reconciliation,
    save_pharmacy_report, PharmacyCheckInput,
)
from agents.housekeeping.activities import (
    get_vacated_beds, dispatch_housekeeping, HousekeepingDispatchInput,
)
from agents.ot.activities import (
    get_ot_census,
    check_ot_room_cleaning, OtRoomInput,
    check_ot_instrument_readiness, OtInstrumentInput,
    track_ot_turnaround,
    score_ot_efficiency, OtEfficiencyInput,
    predict_ot_delays, OtDelayInput,
    coordinate_ot_staff, OtStaffInput,
    detect_ot_conflicts, OtScheduleInput,
    find_ot_emergencies, check_ot_resource_availability,
    handle_ot_emergencies, OtEmergencyInput,
    optimise_ot_slots, OtSlotInput,
    balance_ot_load, OtLoadInput,
    analyze_ot_capacity, OtCapacityInput,
    find_ot_theatre_slots, OtSlotSearchInput,
    reschedule_ot_surgery, OtRescheduleInput,
    defer_ot_electives, OtDeferInput,
)
from agents.billing.activities import (
    detect_claim_discrepancies, validate_insurance_eligibility,
    check_billing_compliance, track_pending_payments, detect_revenue_leakage,
    generate_billing_recommendations, prioritize_payments, trigger_payment_reminder,
    notify_followup_team, BillingOptimizationInput,
    get_patient_billing, create_billing_request,
    PatientBillingInput, InitiateBillingInput,
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
_BILLING_TASKS = {sa.id: [t.schema() for t in sa.tasks] for sa in SUB_AGENTS.get("billing_agent", [])}
_LAB_TASKS     = {sa.id: [t.schema() for t in sa.tasks] for sa in SUB_AGENTS.get("lab_agent", [])}
_PHARM_TASKS   = {sa.id: [t.schema() for t in sa.tasks] for sa in SUB_AGENTS.get("pharmacy_agent", [])}


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
                    "sa_er_boarding"):
            await plan_subagent("er_agent", _sa, _ER_TASKS, task_plan, ta_results, goal, sid)

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
        return {"status": "completed", "message": "No active ER patients"}

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


# -- Revenue -------------------------------------------------------------------

async def run_revenue_body(sid: str, ctx: dict) -> dict:
    # task_type = ctx.get("_task_type", "")  # (revenue/billing split 2026-06) billing task_types moved to run_billing_body
    goal = ctx.get("_goal", "")
    ta_results: dict = {}
    _raw_plan = ctx.get("_task_plan")
    task_plan: dict | None = dict(_raw_plan) if _raw_plan is not None else None
    if task_plan is not None and goal:
        seed_planned_slots(task_plan, _REVENUE_TASKS)

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
        **(_dynamic and {"dynamic_tasks": _dynamic} or {}),
    }


# -- Pharmacy ------------------------------------------------------------------

async def run_pharmacy_body(sid: str, ctx: dict) -> dict:
    goal = ctx.get("_goal", "")
    ta_results: dict = {}
    _raw_plan = ctx.get("_task_plan")
    task_plan: dict | None = dict(_raw_plan) if _raw_plan is not None else None
    if task_plan is not None and goal:
        seed_planned_slots(task_plan, _PHARM_TASKS)

    # sa_medication_prioritization
    if task_plan is not None:
        await plan_subagent("pharmacy_agent", "sa_medication_prioritization", _PHARM_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_medication_prioritization", task_plan):
        if await should_run_task("ta_check_stat_medication_orders", "sa_medication_prioritization", ta_results, task_plan, sid):
            ta_results["ta_check_stat_medication_orders"] = await check_stat_medication_orders(sid)
        if await should_run_task("ta_apply_critical_patient_priority", "sa_medication_prioritization", ta_results, task_plan, sid):
            ta_results["ta_apply_critical_patient_priority"] = await apply_critical_patient_priority(sid)
        if await should_run_task("ta_check_stat_availability", "sa_medication_prioritization", ta_results, task_plan, sid):
            ta_results["ta_check_stat_availability"] = await check_stat_availability(sid)
        if await should_run_task("ta_escalate_stat_shortage", "sa_medication_prioritization", ta_results, task_plan, sid):
            ta_results["ta_escalate_stat_shortage"] = await escalate_stat_shortage(sid)

    # sa_medication_fulfillment
    if task_plan is not None:
        await plan_subagent("pharmacy_agent", "sa_medication_fulfillment", _PHARM_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_medication_fulfillment", task_plan):
        if await should_run_task("ta_check_prescription_received", "sa_medication_fulfillment", ta_results, task_plan, sid):
            ta_results["ta_check_prescription_received"] = await check_prescription_received(sid)
        if await should_run_task("ta_check_medication_availability", "sa_medication_fulfillment", ta_results, task_plan, sid):
            ta_results["ta_check_medication_availability"] = await check_medication_availability(sid)
        if await should_run_task("ta_track_dispensing_progress", "sa_medication_fulfillment", ta_results, task_plan, sid):
            ta_results["ta_track_dispensing_progress"] = await track_dispensing_progress(sid)
        if await should_run_task("ta_close_fulfilled_orders", "sa_medication_fulfillment", ta_results, task_plan, sid):
            ta_results["ta_close_fulfilled_orders"] = await close_fulfilled_orders(sid)

    # sa_drug_availability
    if task_plan is not None:
        await plan_subagent("pharmacy_agent", "sa_drug_availability", _PHARM_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_drug_availability", task_plan):
        if await should_run_task("ta_check_stock_levels", "sa_drug_availability", ta_results, task_plan, sid):
            ta_results["ta_check_stock_levels"] = await check_stock_levels(sid)
        if await should_run_task("ta_search_alternate_location", "sa_drug_availability", ta_results, task_plan, sid):
            ta_results["ta_search_alternate_location"] = await search_alternate_location(sid)
        if await should_run_task("ta_reserve_inventory", "sa_drug_availability", ta_results, task_plan, sid):
            ta_results["ta_reserve_inventory"] = await reserve_inventory(sid)
        if await should_run_task("ta_escalate_critical_shortage", "sa_drug_availability", ta_results, task_plan, sid):
            ta_results["ta_escalate_critical_shortage"] = await escalate_critical_shortage(sid)

    # sa_prescription_validation
    if task_plan is not None:
        await plan_subagent("pharmacy_agent", "sa_prescription_validation", _PHARM_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_prescription_validation", task_plan):
        if await should_run_task("ta_validate_prescription_completeness", "sa_prescription_validation", ta_results, task_plan, sid):
            ta_results["ta_validate_prescription_completeness"] = await validate_prescription_completeness(sid)
        if await should_run_task("ta_validate_dosage_range", "sa_prescription_validation", ta_results, task_plan, sid):
            ta_results["ta_validate_dosage_range"] = await validate_dosage_range(sid)
        if await should_run_task("ta_detect_duplicate_medications", "sa_prescription_validation", ta_results, task_plan, sid):
            ta_results["ta_detect_duplicate_medications"] = await detect_duplicate_medications(sid)
        if await should_run_task("ta_approve_or_hold_prescription", "sa_prescription_validation", ta_results, task_plan, sid):
            ta_results["ta_approve_or_hold_prescription"] = await approve_or_hold_prescription(sid)

    # sa_clinical_interaction
    if task_plan is not None:
        await plan_subagent("pharmacy_agent", "sa_clinical_interaction", _PHARM_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_clinical_interaction", task_plan):
        if await should_run_task("ta_check_polypharmacy", "sa_clinical_interaction", ta_results, task_plan, sid):
            ta_results["ta_check_polypharmacy"] = await check_polypharmacy(sid)
        if await should_run_task("ta_run_interaction_check", "sa_clinical_interaction", ta_results, task_plan, sid):
            ta_results["ta_run_interaction_check"] = await run_interaction_check(sid)
        if await should_run_task("ta_check_allergy_conflict", "sa_clinical_interaction", ta_results, task_plan, sid):
            ta_results["ta_check_allergy_conflict"] = await check_allergy_conflict(sid)
        if await should_run_task("ta_approve_safe_dispense", "sa_clinical_interaction", ta_results, task_plan, sid):
            ta_results["ta_approve_safe_dispense"] = await approve_safe_dispense(sid)

    # sa_dispensing_validation
    if task_plan is not None:
        await plan_subagent("pharmacy_agent", "sa_dispensing_validation", _PHARM_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_dispensing_validation", task_plan):
        if await should_run_task("ta_verify_patient_identity", "sa_dispensing_validation", ta_results, task_plan, sid):
            ta_results["ta_verify_patient_identity"] = await verify_patient_identity(sid)
        if await should_run_task("ta_match_medication_prescription", "sa_dispensing_validation", ta_results, task_plan, sid):
            ta_results["ta_match_medication_prescription"] = await match_medication_prescription(sid)
        if await should_run_task("ta_validate_dispensing_dosage", "sa_dispensing_validation", ta_results, task_plan, sid):
            ta_results["ta_validate_dispensing_dosage"] = await validate_dispensing_dosage(sid)
        if await should_run_task("ta_release_or_hold_dispensing", "sa_dispensing_validation", ta_results, task_plan, sid):
            ta_results["ta_release_or_hold_dispensing"] = await release_or_hold_dispensing(sid)

    # sa_medication_substitution
    if task_plan is not None:
        await plan_subagent("pharmacy_agent", "sa_medication_substitution", _PHARM_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_medication_substitution", task_plan):
        if await should_run_task("ta_check_unavailable_medications", "sa_medication_substitution", ta_results, task_plan, sid):
            ta_results["ta_check_unavailable_medications"] = await check_unavailable_medications(sid)
        if await should_run_task("ta_search_formulary_alternatives", "sa_medication_substitution", ta_results, task_plan, sid):
            ta_results["ta_search_formulary_alternatives"] = await search_formulary_alternatives(sid)
        if await should_run_task("ta_request_physician_approval", "sa_medication_substitution", ta_results, task_plan, sid):
            ta_results["ta_request_physician_approval"] = await request_physician_approval(sid)
        if await should_run_task("ta_update_substitution_order", "sa_medication_substitution", ta_results, task_plan, sid):
            ta_results["ta_update_substitution_order"] = await update_substitution_order(sid)

    # sa_pharmacy_queue
    if task_plan is not None:
        await plan_subagent("pharmacy_agent", "sa_pharmacy_queue", _PHARM_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_pharmacy_queue", task_plan):
        if await should_run_task("ta_check_queue_length", "sa_pharmacy_queue", ta_results, task_plan, sid):
            ta_results["ta_check_queue_length"] = await check_queue_length(sid)
        if await should_run_task("ta_analyze_queue_bottleneck", "sa_pharmacy_queue", ta_results, task_plan, sid):
            ta_results["ta_analyze_queue_bottleneck"] = await analyze_queue_bottleneck(sid)
        if await should_run_task("ta_prioritize_stat_medications", "sa_pharmacy_queue", ta_results, task_plan, sid):
            ta_results["ta_prioritize_stat_medications"] = await prioritize_stat_medications(sid)
        if await should_run_task("ta_escalate_tat_breach", "sa_pharmacy_queue", ta_results, task_plan, sid):
            ta_results["ta_escalate_tat_breach"] = await escalate_tat_breach(sid)

    # sa_controlled_drug_compliance
    if task_plan is not None:
        await plan_subagent("pharmacy_agent", "sa_controlled_drug_compliance", _PHARM_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_controlled_drug_compliance", task_plan):
        if await should_run_task("ta_identify_controlled_orders", "sa_controlled_drug_compliance", ta_results, task_plan, sid):
            ta_results["ta_identify_controlled_orders"] = await identify_controlled_orders(sid)
        if await should_run_task("ta_verify_controlled_authorization", "sa_controlled_drug_compliance", ta_results, task_plan, sid):
            ta_results["ta_verify_controlled_authorization"] = await verify_controlled_authorization(sid)
        if await should_run_task("ta_check_inventory_variance", "sa_controlled_drug_compliance", ta_results, task_plan, sid):
            ta_results["ta_check_inventory_variance"] = await check_inventory_variance(sid)
        if await should_run_task("ta_escalate_compliance_issue", "sa_controlled_drug_compliance", ta_results, task_plan, sid):
            ta_results["ta_escalate_compliance_issue"] = await escalate_compliance_issue(sid)

    # sa_stock_monitor
    if task_plan is not None:
        await plan_subagent("pharmacy_agent", "sa_stock_monitor", _PHARM_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_stock_monitor", task_plan):
        if await should_run_task("ta_get_discharge_patients", "sa_stock_monitor", ta_results, task_plan, sid):
            admissions = await get_discharge_ready_patients(sid)
            ta_results["ta_get_discharge_patients"] = {"patients": admissions}
        else:
            admissions = (ta_results.get("ta_get_discharge_patients") or {}).get("patients", [])
        if await should_run_task("ta_check_medication_reconciliation", "sa_stock_monitor", ta_results, task_plan, sid):
            ta_results["ta_check_medication_reconciliation"] = await check_medication_reconciliation(
                PharmacyCheckInput(session_id=sid, admissions=admissions)
            )
        if await should_run_task("ta_save_pharmacy_report", "sa_stock_monitor", ta_results, task_plan, sid):
            ta_results["ta_save_pharmacy_report"] = await save_pharmacy_report(
                PharmacyCheckInput(session_id=sid, admissions=admissions)
            )

    _dynamic = await run_dynamic_tasks("pharmacy_agent", task_plan, ta_results, sid)
    return {
        "status": "completed",
        "agent_id": "pharmacy_agent",
        "prioritization": {
            "stat_count": (ta_results.get("ta_check_stat_medication_orders") or {}).get("stat_count", 0),
            "critical_patients_prioritized": (ta_results.get("ta_apply_critical_patient_priority") or {}).get("prioritized_count", 0),
            "stat_unavailable": (ta_results.get("ta_check_stat_availability") or {}).get("stat_unavailable_count", 0),
        },
        "fulfillment": {
            "in_progress": (ta_results.get("ta_track_dispensing_progress") or {}).get("in_progress_count", 0),
            "completed": (ta_results.get("ta_track_dispensing_progress") or {}).get("completed_count", 0),
            "closed": (ta_results.get("ta_close_fulfilled_orders") or {}).get("closed_count", 0),
        },
        "stock": {
            "low_stock": (ta_results.get("ta_check_stock_levels") or {}).get("low_stock_count", 0),
            "critical_shortage": (ta_results.get("ta_check_stock_levels") or {}).get("critical_shortage_count", 0),
        },
        "clinical_safety": {
            "interaction_count": (ta_results.get("ta_run_interaction_check") or {}).get("interaction_count", 0),
            "allergy_conflicts": (ta_results.get("ta_check_allergy_conflict") or {}).get("conflict_count", 0),
            "polypharmacy_risks": (ta_results.get("ta_check_polypharmacy") or {}).get("polypharmacy_risk_count", 0),
        },
        "compliance": {
            "controlled_orders": (ta_results.get("ta_identify_controlled_orders") or {}).get("controlled_count", 0),
            "inventory_variance": (ta_results.get("ta_check_inventory_variance") or {}).get("variance_count", 0),
        },
        **(_dynamic and {"dynamic_tasks": _dynamic} or {}),
    }


# -- Lab -----------------------------------------------------------------------

async def run_lab_body(sid: str, ctx: dict) -> dict:
    goal = ctx.get("_goal", "")
    ta_results: dict = {}
    _raw_plan = ctx.get("_task_plan")
    task_plan: dict | None = dict(_raw_plan) if _raw_plan is not None else None
    if task_plan is not None and goal:
        seed_planned_slots(task_plan, _LAB_TASKS)

    # sa_sample_prioritization
    if task_plan is not None:
        await plan_subagent("lab_agent", "sa_sample_prioritization", _LAB_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_sample_prioritization", task_plan):
        if await should_run_task("ta_check_stat_status", "sa_sample_prioritization", ta_results, task_plan, sid):
            ta_results["ta_check_stat_status"] = await check_stat_status(sid)
        if await should_run_task("ta_apply_icu_er_priority", "sa_sample_prioritization", ta_results, task_plan, sid):
            ta_results["ta_apply_icu_er_priority"] = await apply_icu_er_priority(sid)
        if await should_run_task("ta_check_analyzer_available", "sa_sample_prioritization", ta_results, task_plan, sid):
            ta_results["ta_check_analyzer_available"] = await check_analyzer_available(sid)
        if await should_run_task("ta_escalate_tat_risk", "sa_sample_prioritization", ta_results, task_plan, sid):
            ta_results["ta_escalate_tat_risk"] = await escalate_tat_risk(sid)

    # sa_sample_tracking
    if task_plan is not None:
        await plan_subagent("lab_agent", "sa_sample_tracking", _LAB_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_sample_tracking", task_plan):
        if await should_run_task("ta_check_sample_collection", "sa_sample_tracking", ta_results, task_plan, sid):
            ta_results["ta_check_sample_collection"] = await check_sample_collection(sid)
        if await should_run_task("ta_check_sample_transport", "sa_sample_tracking", ta_results, task_plan, sid):
            ta_results["ta_check_sample_transport"] = await check_sample_transport(sid)
        if await should_run_task("ta_verify_sample_receipt", "sa_sample_tracking", ta_results, task_plan, sid):
            ta_results["ta_verify_sample_receipt"] = await verify_sample_receipt(sid)
        if await should_run_task("ta_trigger_sample_search", "sa_sample_tracking", ta_results, task_plan, sid):
            ta_results["ta_trigger_sample_search"] = await trigger_sample_search(sid)

    # sa_tat_optimization
    if task_plan is not None:
        await plan_subagent("lab_agent", "sa_tat_optimization", _LAB_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_tat_optimization", task_plan):
        if await should_run_task("ta_check_tat_threshold", "sa_tat_optimization", ta_results, task_plan, sid):
            ta_results["ta_check_tat_threshold"] = await check_tat_threshold(sid)
        if await should_run_task("ta_analyze_tat_bottleneck", "sa_tat_optimization", ta_results, task_plan, sid):
            ta_results["ta_analyze_tat_bottleneck"] = await analyze_tat_bottleneck(sid)
        if await should_run_task("ta_prioritize_stat_queue", "sa_tat_optimization", ta_results, task_plan, sid):
            ta_results["ta_prioritize_stat_queue"] = await prioritize_stat_queue(sid)
        if await should_run_task("ta_escalate_tat_supervisor", "sa_tat_optimization", ta_results, task_plan, sid):
            ta_results["ta_escalate_tat_supervisor"] = await escalate_tat_supervisor(sid)

    # sa_analyzer_utilization
    if task_plan is not None:
        await plan_subagent("lab_agent", "sa_analyzer_utilization", _LAB_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_analyzer_utilization", task_plan):
        if await should_run_task("ta_check_analyzer_utilization", "sa_analyzer_utilization", ta_results, task_plan, sid):
            ta_results["ta_check_analyzer_utilization"] = await check_analyzer_utilization(sid)
        if await should_run_task("ta_identify_alternate_analyzer", "sa_analyzer_utilization", ta_results, task_plan, sid):
            ta_results["ta_identify_alternate_analyzer"] = await identify_alternate_analyzer(sid)
        if await should_run_task("ta_rebalance_analyzer_workload", "sa_analyzer_utilization", ta_results, task_plan, sid):
            ta_results["ta_rebalance_analyzer_workload"] = await rebalance_analyzer_workload(sid)
        if await should_run_task("ta_trigger_maintenance_alert", "sa_analyzer_utilization", ta_results, task_plan, sid):
            ta_results["ta_trigger_maintenance_alert"] = await trigger_maintenance_alert(sid)

    # sa_analyzer_routing
    if task_plan is not None:
        await plan_subagent("lab_agent", "sa_analyzer_routing", _LAB_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_analyzer_routing", task_plan):
        if await should_run_task("ta_check_analyzer_overload", "sa_analyzer_routing", ta_results, task_plan, sid):
            ta_results["ta_check_analyzer_overload"] = await check_analyzer_overload(sid)
        if await should_run_task("ta_validate_alternate_analyzer", "sa_analyzer_routing", ta_results, task_plan, sid):
            ta_results["ta_validate_alternate_analyzer"] = await validate_alternate_analyzer(sid)
        if await should_run_task("ta_execute_sample_routing", "sa_analyzer_routing", ta_results, task_plan, sid):
            ta_results["ta_execute_sample_routing"] = await execute_sample_routing(sid)
        if await should_run_task("ta_restore_routing_capacity", "sa_analyzer_routing", ta_results, task_plan, sid):
            ta_results["ta_restore_routing_capacity"] = await restore_routing_capacity(sid)

    # sa_quality_control
    if task_plan is not None:
        await plan_subagent("lab_agent", "sa_quality_control", _LAB_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_quality_control", task_plan):
        if await should_run_task("ta_check_qc_status", "sa_quality_control", ta_results, task_plan, sid):
            ta_results["ta_check_qc_status"] = await check_qc_status(sid)
        if await should_run_task("ta_trigger_recalibration", "sa_quality_control", ta_results, task_plan, sid):
            ta_results["ta_trigger_recalibration"] = await trigger_recalibration(sid)
        if await should_run_task("ta_repeat_qc_check", "sa_quality_control", ta_results, task_plan, sid):
            ta_results["ta_repeat_qc_check"] = await repeat_qc_check(sid)
        if await should_run_task("ta_compliance_alert", "sa_quality_control", ta_results, task_plan, sid):
            ta_results["ta_compliance_alert"] = await compliance_alert(sid)

    # sa_test_validation
    if task_plan is not None:
        await plan_subagent("lab_agent", "sa_test_validation", _LAB_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_test_validation", task_plan):
        if await should_run_task("ta_validate_result_rules", "sa_test_validation", ta_results, task_plan, sid):
            ta_results["ta_validate_result_rules"] = await validate_result_rules(sid)
        if await should_run_task("ta_check_delta_flag", "sa_test_validation", ta_results, task_plan, sid):
            ta_results["ta_check_delta_flag"] = await check_delta_flag(sid)
        if await should_run_task("ta_check_critical_value_flag", "sa_test_validation", ta_results, task_plan, sid):
            ta_results["ta_check_critical_value_flag"] = await check_critical_value_flag(sid)
        if await should_run_task("ta_release_validated_report", "sa_test_validation", ta_results, task_plan, sid):
            ta_results["ta_release_validated_report"] = await release_validated_report(sid)

    # sa_critical_result_escalation
    if task_plan is not None:
        await plan_subagent("lab_agent", "sa_critical_result_escalation", _LAB_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_critical_result_escalation", task_plan):
        if await should_run_task("ta_detect_critical_results", "sa_critical_result_escalation", ta_results, task_plan, sid):
            ta_results["ta_detect_critical_results"] = await detect_critical_results(sid)
        if await should_run_task("ta_notify_physician_critical", "sa_critical_result_escalation", ta_results, task_plan, sid):
            ta_results["ta_notify_physician_critical"] = await notify_physician_critical(sid)
        if await should_run_task("ta_escalate_icu_er_critical", "sa_critical_result_escalation", ta_results, task_plan, sid):
            ta_results["ta_escalate_icu_er_critical"] = await escalate_icu_er_critical(sid)
        if await should_run_task("ta_log_critical_action", "sa_critical_result_escalation", ta_results, task_plan, sid):
            ta_results["ta_log_critical_action"] = await log_critical_action(sid)

    # sa_test_recommendation
    if task_plan is not None:
        await plan_subagent("lab_agent", "sa_test_recommendation", _LAB_TASKS, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_test_recommendation", task_plan):
        if await should_run_task("ta_detect_abnormal_result", "sa_test_recommendation", ta_results, task_plan, sid):
            ta_results["ta_detect_abnormal_result"] = await detect_abnormal_result(sid)
        if await should_run_task("ta_evaluate_reflex_rules", "sa_test_recommendation", ta_results, task_plan, sid):
            ta_results["ta_evaluate_reflex_rules"] = await evaluate_reflex_rules(sid)
        if await should_run_task("ta_recommend_additional_test", "sa_test_recommendation", ta_results, task_plan, sid):
            ta_results["ta_recommend_additional_test"] = await recommend_additional_test(sid)
        if await should_run_task("ta_create_reflex_order", "sa_test_recommendation", ta_results, task_plan, sid):
            ta_results["ta_create_reflex_order"] = await create_reflex_order(sid)

    _dynamic = await run_dynamic_tasks("lab_agent", task_plan, ta_results, sid)
    return {
        "status": "completed",
        "agent_id": "lab_agent",
        "prioritization": {
            "stat_count": (ta_results.get("ta_check_stat_status") or {}).get("stat_count", 0),
            "prioritized": (ta_results.get("ta_apply_icu_er_priority") or {}).get("prioritized_count", 0),
        },
        "tracking": {
            "pending_collection": (ta_results.get("ta_check_sample_collection") or {}).get("pending_count", 0),
            "in_transit": (ta_results.get("ta_check_sample_transport") or {}).get("in_transit", 0),
            "missing": (ta_results.get("ta_verify_sample_receipt") or {}).get("missing_count", 0),
        },
        "tat": {
            "overdue": (ta_results.get("ta_check_tat_threshold") or {}).get("overdue_count", 0),
            "bottleneck": (ta_results.get("ta_analyze_tat_bottleneck") or {}).get("bottleneck_stage"),
        },
        "quality_control": {
            "qc_failed": (ta_results.get("ta_check_qc_status") or {}).get("qc_failed", False),
            "failed_count": (ta_results.get("ta_check_qc_status") or {}).get("failed_count", 0),
        },
        "critical_results": {
            "critical_count": (ta_results.get("ta_detect_critical_results") or {}).get("critical_count", 0),
            "notified": (ta_results.get("ta_notify_physician_critical") or {}).get("notified_count", 0),
        },
        **(_dynamic and {"dynamic_tasks": _dynamic} or {}),
    }


# -- Housekeeping --------------------------------------------------------------

async def run_housekeeping_body(sid: str, ctx: dict) -> dict:
    beds = await get_vacated_beds(sid)
    if not beds:
        return {"status": "completed", "message": "No beds currently require cleaning"}
    result = await dispatch_housekeeping(HousekeepingDispatchInput(session_id=sid, beds=beds))
    return {"status": "completed", "dispatched": result["dispatched"]}


# -- OT ------------------------------------------------------------------------

async def run_ot_body(sid: str, ctx: dict) -> dict:
    task_plan: dict = ctx.get("_task_plan", {})
    ta: dict = {}

    # -- sa_ot_census: single OT fetch (schedule, theatres, equipment, post-op beds)
    if await should_run_task("ta_get_ot_census", "sa_ot_census", ta, task_plan):
        cached = await get_prefetch_cache(GetPrefetchInput(session_id=sid, task_id="ta_get_ot_census"))
        ta["ta_get_ot_census"] = cached if cached else await get_ot_census(sid)

    census = ta.get("ta_get_ot_census", {})
    schedule = census.get("upcoming_surgeries", [])
    rooms = census.get("rooms", [])
    rs = census.get("room_status", [])
    equip = census.get("equipment_by_surgery", {})

    if not schedule and not rs:
        return {"status": "completed", "message": "No OT data available"}

    # -- sa_ot_turnaround: theatre readiness, delays, staff (consumes census) -----
    if await should_run_task("ta_ot_check_cleaning", "sa_ot_turnaround", ta, task_plan):
        ta["ta_ot_check_cleaning"] = await check_ot_room_cleaning(OtRoomInput(sid, rs))

    if await should_run_task("ta_ot_check_instruments", "sa_ot_turnaround", ta, task_plan):
        ta["ta_ot_check_instruments"] = await check_ot_instrument_readiness(OtInstrumentInput(sid, schedule, equip))

    if await should_run_task("ta_ot_track_turnaround", "sa_ot_turnaround", ta, task_plan):
        ta["ta_ot_track_turnaround"] = await track_ot_turnaround(OtRoomInput(sid, rs))

    if await should_run_task("ta_ot_predict_delays", "sa_ot_turnaround", ta, task_plan):
        ta["ta_ot_predict_delays"] = await predict_ot_delays(OtDelayInput(
            sid, rs, schedule, ta.get("ta_ot_track_turnaround", {}).get("rooms_active", [])))

    if await should_run_task("ta_ot_coordinate_staff", "sa_ot_turnaround", ta, task_plan):
        ta["ta_ot_coordinate_staff"] = await coordinate_ot_staff(OtStaffInput(
            sid,
            ta.get("ta_ot_predict_delays", {}).get("delay_risks", []),
            ta.get("ta_ot_check_cleaning", {}).get("rooms_to_clean", []),
            ta.get("ta_ot_check_instruments", {}).get("gaps", []),
        ))

    # -- sa_ot_scheduling: elective plan -- conflicts, resources, slot/load opt ---
    if await should_run_task("ta_ot_detect_conflicts", "sa_ot_scheduling", ta, task_plan):
        ta["ta_ot_detect_conflicts"] = await detect_ot_conflicts(OtScheduleInput(sid, schedule, rooms))

    if await should_run_task("ta_ot_check_resources", "sa_ot_scheduling", ta, task_plan):
        ta["ta_ot_check_resources"] = await check_ot_resource_availability(OtScheduleInput(sid, schedule, rooms))

    conflicts = ta.get("ta_ot_detect_conflicts", {})

    if await should_run_task("ta_ot_optimise_slots", "sa_ot_scheduling", ta, task_plan):
        ta["ta_ot_optimise_slots"] = await optimise_ot_slots(OtSlotInput(sid, schedule, rooms, conflicts))

    if await should_run_task("ta_ot_balance_load", "sa_ot_scheduling", ta, task_plan):
        ta["ta_ot_balance_load"] = await balance_ot_load(OtLoadInput(
            sid, schedule, rooms, ta.get("ta_ot_check_resources", {}).get("cases_by_room", {})))

    # G32: derived open theatre slots + executable surgical reschedule. `census["schedule"]`
    # is the FULL booked list (not today-only) so future free windows are visible.
    all_bookings = census.get("schedule", [])
    if await should_run_task("ta_ot_find_theatre_slots", "sa_ot_scheduling", ta, task_plan):
        ta["ta_ot_find_theatre_slots"] = await find_ot_theatre_slots(OtSlotSearchInput(
            session_id=sid, rooms=rooms, booked_schedule=all_bookings))

    if await should_run_task("ta_ot_reschedule_surgery", "sa_ot_scheduling", ta, task_plan):
        ta["ta_ot_reschedule_surgery"] = await reschedule_ot_surgery(OtRescheduleInput(
            session_id=sid, booked_schedule=all_bookings, rooms=rooms, goal=ctx.get("_goal", "")))

    # -- sa_ot_emergency: acuity-reactive -- detect non-elective cases and respond
    if await should_run_task("ta_ot_find_emergencies", "sa_ot_emergency", ta, task_plan):
        ta["ta_ot_find_emergencies"] = await find_ot_emergencies(OtScheduleInput(sid, schedule, rooms))

    emergencies = ta.get("ta_ot_find_emergencies", {})

    if await should_run_task("ta_ot_handle_emergencies", "sa_ot_emergency", ta, task_plan):
        ta["ta_ot_handle_emergencies"] = await handle_ot_emergencies(OtEmergencyInput(
            sid, emergencies.get("emergency_cases", []), rooms))

    # -- sa_ot_analysis: terminal synthesis -- runs AFTER turnaround + scheduling
    # so efficiency scoring sees the real conflict_count instead of a placeholder.
    if await should_run_task("ta_ot_score_efficiency", "sa_ot_analysis", ta, task_plan):
        maintenance = sum(1 for r in rooms if (r.get("status") or "") == "Maintenance")
        ta["ta_ot_score_efficiency"] = await score_ot_efficiency(OtEfficiencyInput(
            sid,
            maintenance_rooms=maintenance,
            high_risk_delays=ta.get("ta_ot_predict_delays", {}).get("high_risk_count", 0),
            instrument_gaps=ta.get("ta_ot_check_instruments", {}).get("gap_count", 0),
            conflict_count=conflicts.get("conflict_count", 0),
        ))

    if await should_run_task("ta_analyze_ot_capacity", "sa_ot_analysis", ta, task_plan):
        ta["ta_analyze_ot_capacity"] = await analyze_ot_capacity(OtCapacityInput(
            sid, schedule, rooms, conflicts, emergencies.get("emergency_cases", []),
            ta.get("ta_ot_check_resources", {})))

    # OT reprioritisation (executable): move electives flagged 'delay' to a later slot.
    if await should_run_task("ta_ot_defer_electives", "sa_ot_analysis", ta, task_plan):
        ta["ta_ot_defer_electives"] = await defer_ot_electives(OtDeferInput(
            session_id=sid, booked_schedule=all_bookings, rooms=rooms,
            case_recommendations=ta.get("ta_analyze_ot_capacity", {}).get("case_recommendations", [])))

    return {
        "status": "completed",
        "upcoming_today": len(schedule),
        "scheduled_cases": census.get("case_count", len(schedule)),
        "post_op_beds_available": census.get("post_op_beds_available"),
        "capacity_recommendations": ta.get("ta_analyze_ot_capacity", {}).get("recommendation_count", 0),
        "efficiency_score": ta.get("ta_ot_score_efficiency", {}).get("efficiency_score"),
        "delay_risks": len(ta.get("ta_ot_predict_delays", {}).get("delay_risks", [])),
        "instrument_gaps": ta.get("ta_ot_check_instruments", {}).get("gap_count", 0),
        "conflicts": conflicts.get("conflict_count", 0),
        "emergency_actions": len(ta.get("ta_ot_handle_emergencies", {}).get("emergency_actions", [])),
        "slot_optimizations": len(ta.get("ta_ot_optimise_slots", {}).get("slot_optimizations", [])),
        "open_ot_slots": ta.get("ta_ot_find_theatre_slots", {}).get("open_slot_count", 0),
        "reschedules_staged": ta.get("ta_ot_reschedule_surgery", {}).get("rescheduled", 0),
        "electives_deferred": ta.get("ta_ot_defer_electives", {}).get("deferred", 0),
        "turnaround_summary": ta.get("ta_ot_predict_delays", {}).get("summary", ""),
        "scheduling_summary": ta.get("ta_ot_balance_load", {}).get("summary", ""),
    }


# -- Billing -------------------------------------------------------------------

async def run_billing_body(sid: str, ctx: dict) -> dict:
    task_type = ctx.get("_task_type", "")
    goal = ctx.get("_goal", "")
    ta_results: dict = {}
    _raw_plan = ctx.get("_task_plan")
    task_plan: dict | None = dict(_raw_plan) if _raw_plan is not None else None
    if task_plan is not None and goal:
        seed_planned_slots(task_plan, _BILLING_TASKS)

    # (revenue/billing split 2026-06) billing_agent now executes billing ops:
    # single-patient invoice lookup (patient_billing) and bill generation
    # (initiate_billing). Ported from run_revenue_body.
    if task_type == "patient_billing":
        if task_plan is not None:
            await plan_subagent("billing_agent", "sa_rev_patient_billing", {}, task_plan, ta_results, goal, sid)
        if await should_run_task("ta_get_patient_invoices", "sa_rev_patient_billing", ta_results, task_plan, sid):
            ta_results["ta_get_patient_invoices"] = await get_patient_billing(
                PatientBillingInput(session_id=sid, goal=goal))
        return {"status": "completed", "mode": "patient_billing", **ta_results.get("ta_get_patient_invoices", {})}

    if task_type == "initiate_billing":
        if task_plan is not None:
            await plan_subagent("billing_agent", "sa_rev_initiate_billing", {}, task_plan, ta_results, goal, sid)
        if await should_run_task("ta_create_billing_request", "sa_rev_initiate_billing", ta_results, task_plan, sid):
            ta_results["ta_create_billing_request"] = await create_billing_request(
                InitiateBillingInput(session_id=sid, goal=goal))
        return {"status": "completed", "mode": "initiate_billing", "agent_id": "billing_agent",
                **ta_results.get("ta_create_billing_request", {})}

    if task_plan is not None:
        await plan_subagent("billing_agent", "sa_claim_validation", {}, task_plan, ta_results, goal, sid)

    if subagent_in_plan("sa_claim_validation", task_plan):
        if await should_run_task("ta_detect_claim_discrepancies", "sa_claim_validation", ta_results, task_plan, sid):
            ta_results["ta_detect_claim_discrepancies"] = await detect_claim_discrepancies(sid)
        if await should_run_task("ta_validate_insurance_eligibility", "sa_claim_validation", ta_results, task_plan, sid):
            ta_results["ta_validate_insurance_eligibility"] = await validate_insurance_eligibility(sid)
        if await should_run_task("ta_check_billing_compliance", "sa_claim_validation", ta_results, task_plan, sid):
            ta_results["ta_check_billing_compliance"] = await check_billing_compliance(sid)

    if task_plan is not None:
        await plan_subagent("billing_agent", "sa_billing_optimization", {}, task_plan, ta_results, goal, sid)

    if subagent_in_plan("sa_billing_optimization", task_plan):
        if await should_run_task("ta_track_pending_payments", "sa_billing_optimization", ta_results, task_plan, sid):
            ta_results["ta_track_pending_payments"] = await track_pending_payments(sid)
        if await should_run_task("ta_detect_revenue_leakage", "sa_billing_optimization", ta_results, task_plan, sid):
            ta_results["ta_detect_revenue_leakage"] = await detect_revenue_leakage(sid)
        if await should_run_task("ta_generate_billing_recommendations", "sa_billing_optimization", ta_results, task_plan, sid):
            ta_results["ta_generate_billing_recommendations"] = await generate_billing_recommendations(
                BillingOptimizationInput(session_id=sid, goal=goal))
        if await should_run_task("ta_prioritize_payments", "sa_billing_optimization", ta_results, task_plan, sid):
            ta_results["ta_prioritize_payments"] = await prioritize_payments(sid)
        if await should_run_task("ta_trigger_payment_reminder", "sa_billing_optimization", ta_results, task_plan, sid):
            ta_results["ta_trigger_payment_reminder"] = await trigger_payment_reminder(sid)
        if await should_run_task("ta_notify_followup_team", "sa_billing_optimization", ta_results, task_plan, sid):
            ta_results["ta_notify_followup_team"] = await notify_followup_team(sid)

    # (revenue/billing split 2026-06) bill-generation moved in from revenue_agent.
    # Default-flow dispatch (non task_type runs) so staged billing requests surface here too.
    if task_plan is not None:
        await plan_subagent("billing_agent", "sa_rev_initiate_billing", {}, task_plan, ta_results, goal, sid)

    if subagent_in_plan("sa_rev_initiate_billing", task_plan):
        if await should_run_task("ta_create_billing_request", "sa_rev_initiate_billing", ta_results, task_plan, sid):
            ta_results["ta_create_billing_request"] = await create_billing_request(
                InitiateBillingInput(session_id=sid, goal=goal))

    billing = ta_results.get("ta_create_billing_request", {})
    _dynamic = await run_dynamic_tasks("billing_agent", task_plan, ta_results, sid)
    return {
        "status": "completed",
        "agent_id": "billing_agent",
        **({"billing_requests": {
            "patient_count":   billing.get("patient_count", 0),
            "patients_billed": billing.get("patients_billed", []),
            "status":          billing.get("status", ""),
        }} if billing else {}),
        "validation": {
            "discrepancies": (ta_results.get("ta_detect_claim_discrepancies") or {}).get("discrepancy_count", 0),
            "eligibility_issues": (ta_results.get("ta_validate_insurance_eligibility") or {}).get("eligibility_issues", 0),
            "compliance_issues": (ta_results.get("ta_check_billing_compliance") or {}).get("total_compliance_issues", 0),
        },
        "optimization": {
            "overdue_count": (ta_results.get("ta_track_pending_payments") or {}).get("overdue_count", 0),
            "overdue_amount": (ta_results.get("ta_track_pending_payments") or {}).get("overdue_amount", 0),
            "leakage_amount": (ta_results.get("ta_detect_revenue_leakage") or {}).get("estimated_leakage", 0),
            "recommendations": (ta_results.get("ta_generate_billing_recommendations") or {}).get("recommendations", []),
        },
        **(_dynamic and {"dynamic_tasks": _dynamic} or {}),
    }

