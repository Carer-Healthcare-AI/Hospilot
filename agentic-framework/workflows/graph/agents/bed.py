"""Bed agent body -- the reference port (most complex agent).

Faithful port of temporal.workflow.bed_agent_workflow.BedAgentWorkflow.run,
preserving every branch: bed_cleaning / availability_check / bed_reservation,
dirty-bed fast-track, no-candidate prediction+escalation, batch (ER critical /
ICU step-down / escalation) vs single reservation.

The two approval points (_run_single, _run_batch) use the resume-aware HITL
pattern instead of Temporal's wait_condition + decide signal. The pending record
stores the minimal data finalize needs (mode + top_bed/assignments + task_plan),
with large candidate lists trimmed from ta_results so the checkpoint stays small.
"""

import logging
import re

from workflows.graph import hitl, patient
from workflows.graph.step_rec import emit_step_recommendation
from workflows.graph.agents._activity import execute_activity
from workflows.graph.planning import should_run_task, plan_subagent, run_dynamic_tasks, seed_planned_slots, subagent_in_plan
from workflows.planner import SUB_AGENTS

from agents.bed.activities import (
    find_available_beds, rank_beds_activity, create_bed_approval, confirm_bed_reservation,
    release_bed_lock_activity, find_beds_for_patients, create_batch_bed_approval,
    confirm_batch_reservations, release_batch_locks,
    RankBedsInput, BedApprovalInput, ReleaseLockInput, BatchAssignmentInput,
)
from agents._shared.prefetch_activities import get_prefetch_cache, GetPrefetchInput
from agents.bed.agent_activities import (
    query_beds, check_dirty_icu_beds, check_dirty_soon_to_release, check_overflow_candidates,
    check_temporary_overflow_beds, filter_ventilator_beds, filter_isolation_beds,
    apply_isolation_room_filter, trigger_alternate_ward_search, sync_bed_status,
    hold_bed_temporarily, dispatch_housekeeping_fast_track, create_emergency_cleaning_task,
    escalate_to_floor_supervisor, validate_sanitization, mark_bed_ready, check_room_readiness,
    validate_oxygen_readiness, check_monitor_readiness, sync_ready_status,
    trigger_discharge_coordination, escalate_to_command_center,
    escalate_allocation_conflict,
    QueryBedsInput, FilterBedsInput, HoldBedInput, NotifyInput,
    EmergencyCleaningInput, SyncBedStatusInput, BedReadinessInput,
)

logger = logging.getLogger(__name__)

# G47: the goal explicitly asserts no clean beds are available. In that case the
# clean-bed search is a guaranteed-empty step, so we skip it and inject the
# known-zero counts (see query_beds known_zero_clean) -- the dirty-bed recovery
# conditions still resolve and the flow starts from dirty-bed recovery. Kept
# conservative to unambiguous state assertions so we never skip a real search
# when clean beds might actually exist.
_ZERO_CLEAN_BEDS_RE = re.compile(
    r"\b(?:zero|0)\s+clean\s+beds?\b"
    r"|\bno\s+clean\s+beds?\b"
    r"|\ball\s+(?:the\s+)?beds?\s+(?:are\s+)?dirty\b",
    re.IGNORECASE,
)


def _goal_states_zero_clean_beds(goal: str) -> bool:
    return bool(_ZERO_CLEAN_BEDS_RE.search(goal or ""))


def _is_bed_release_instance(instance_id: str) -> bool:
    """G41: True for the post-discharge bed instance (e.g. 'bed_agent:after_discharge').
    Such an instance RELEASES the vacated bed; it must not forward-reserve for ICU
    step-down / escalation candidates -- that placement belongs to the ICU-side bed
    instance. Reserving for them in a release flow locks a bed for a next patient
    who does not exist in this workflow."""
    suffix = instance_id.split(":", 1)[1] if ":" in (instance_id or "") else ""
    return "discharge" in suffix.lower() or "release" in suffix.lower()

# --- Execution seam ----------------------------------------------------------
# LangGraph orchestrates; Temporal executes. Rebind every imported Temporal
# activity so calls route through run_activity. Exclusions: prefetch cache reads
# (in-process) and create_batch_bed_approval (passed by reference to
# execute_activity at the batch-approval call, which already routes it).
from functools import partial as _partial
from workflows.graph.agents._activity import run_activity as _run_activity
_NO_ROUTE = {"get_prefetch_cache", "create_batch_bed_approval"}
for _n, _f in list(globals().items()):
    if callable(_f) and hasattr(_f, "__temporal_activity_definition") and _n not in _NO_ROUTE:
        globals()[_n] = _partial(_run_activity, _f)

_BED_TASKS = {sa.id: [t.schema() for t in sa.tasks] for sa in SUB_AGENTS.get("bed_agent", [])}


def _has_feature(b: dict, key: str) -> bool:
    f = b.get("features")
    if isinstance(f, list):
        return key in f
    if isinstance(f, dict):
        return bool(f.get(key))
    return False


def _type_counts(candidates: list) -> dict:
    def _wu(b: dict) -> str:
        return (b.get("ward") or "").upper()
    return {
        "icu_count": sum(1 for b in candidates if "ICU" in _wu(b)),
        "hdu_count": sum(1 for b in candidates if "HDU" in _wu(b) or "HIGH" in _wu(b)),
        "general_count": sum(1 for b in candidates if "ICU" not in _wu(b) and "HDU" not in _wu(b) and "HIGH" not in _wu(b)),
        "ventilator_count": sum(1 for b in candidates if b.get("ventilation") or _has_feature(b, "ventilator")),
        "isolation_count": sum(1 for b in candidates if b.get("room_type", "").lower() in ("isolation", "side_room", "negative_pressure") or _has_feature(b, "isolation")),
    }


def _trim_ta(ta_results: dict) -> dict:
    """Drop large list fields so the pending record stays small in Redis/checkpoint."""
    big = {"candidates", "dirty_beds", "beds", "assignments"}
    return {
        k: ({fk: fv for fk, fv in v.items() if fk not in big} if isinstance(v, dict) else v)
        for k, v in ta_results.items()
    }


def _availability_report(candidates: list) -> dict:
    ward_summary: dict = {}
    for b in candidates:
        ward = b.get("ward", "Unknown")
        ward_summary[ward] = ward_summary.get(ward, 0) + 1
    return {
        "status": "completed", "mode": "availability_check",
        "available_beds": len(candidates), "by_ward": ward_summary, "reservation_made": False,
    }


# -- RL bed-auction award (advisory) ------------------------------------------
# An advisory auction (rl_gateway) never holds a bed; it records an AWARD naming the winning
# patient for a resource. Here we let that award BIAS which patient this reservation is for --
# reordering only, so the awarded patient is reserved first and survives the bed_limit cap.
# Approval stays HITL. A no-match / disabled gateway leaves selection unchanged.

def _patient_token(p) -> str | None:
    if isinstance(p, dict):
        return p.get("patient_token") or p.get("token") or p.get("candidate_id")
    return p if isinstance(p, str) else None


def _prioritize_awarded(patients: list, awarded_tokens: set) -> list:
    """Stable-reorder so RL-awarded patients come first. No-op when nothing matches -- never
    fabricates or drops a patient."""
    if not awarded_tokens or not patients:
        return patients
    winners = [p for p in patients if _patient_token(p) in awarded_tokens]
    if not winners:
        return patients
    rest = [p for p in patients if _patient_token(p) not in awarded_tokens]
    return winners + rest


def _award_note(awards: dict) -> str:
    """One-line approver-facing justification from the session's RL awards, or ''."""
    parts = [
        f"patient {a.get('patient_token')} for {res} (bid {a.get('winning_bid')})"
        for res, a in (awards or {}).items() if a.get("patient_token")
    ]
    return ("RL bed auction awarded " + "; ".join(parts) + ".") if parts else ""


async def _run_cleaning_completion(sid: str, ta_results: dict, task_plan: dict | None) -> None:
    if not task_plan:
        return
    # Beds being recovered this flow: vacated (cleaning flow) or dirty (fast-track branch).
    beds = (
        (ta_results.get("ta_clean_vacated_beds") or {}).get("beds")
        or (ta_results.get("ta_check_dirty_icu_beds") or {}).get("dirty_beds")
        or []
    )
    if await should_run_task("ta_escalate_to_floor_supervisor", "sa_dirty_bed_recovery", ta_results, task_plan, sid):
        ta_results["ta_escalate_to_floor_supervisor"] = await escalate_to_floor_supervisor(
            NotifyInput(session_id=sid, message="Housekeeping not available within SLA -- escalating to floor supervisor."))
    if await should_run_task("ta_validate_sanitization", "sa_dirty_bed_recovery", ta_results, task_plan, sid):
        ta_results["ta_validate_sanitization"] = await validate_sanitization(BedReadinessInput(session_id=sid, beds=beds))

    # Only mark/verify beds that passed sanitization; if sanitization didn't run, use all recovered beds.
    sani = ta_results.get("ta_validate_sanitization")
    if sani is not None:
        passed = set(sani.get("passed_ids", []))
        ready_beds = [b for b in beds if (b.get("id") if isinstance(b, dict) else b) in passed]
    else:
        ready_beds = beds

    if await should_run_task("ta_mark_bed_ready", "sa_dirty_bed_recovery", ta_results, task_plan, sid):
        ta_results["ta_mark_bed_ready"] = await mark_bed_ready(BedReadinessInput(session_id=sid, beds=ready_beds))
    if await should_run_task("ta_check_room_readiness", "sa_dirty_bed_recovery", ta_results, task_plan, sid):
        ta_results["ta_check_room_readiness"] = await check_room_readiness(BedReadinessInput(session_id=sid, beds=ready_beds))
    if await should_run_task("ta_validate_oxygen_readiness", "sa_dirty_bed_recovery", ta_results, task_plan, sid):
        ta_results["ta_validate_oxygen_readiness"] = await validate_oxygen_readiness(BedReadinessInput(session_id=sid, beds=ready_beds))
    if await should_run_task("ta_check_monitor_readiness", "sa_dirty_bed_recovery", ta_results, task_plan, sid):
        ta_results["ta_check_monitor_readiness"] = await check_monitor_readiness(BedReadinessInput(session_id=sid, beds=ready_beds))
    if await should_run_task("ta_sync_ready_status", "sa_dirty_bed_recovery", ta_results, task_plan, sid):
        ta_results["ta_sync_ready_status"] = await sync_ready_status(BedReadinessInput(session_id=sid, beds=ready_beds))


# -- Approval finalizers (resume path) ----------------------------------------

async def _finalize_single(sid: str, pending: dict, decision: str) -> dict:
    top_bed = pending["top_bed"]
    if decision != "approved":
        await release_bed_lock_activity(ReleaseLockInput(session_id=sid, bed_id=top_bed))
        if decision == "timeout":
            return {"status": "timeout", "error": "Approval timed out after 30 min"}
        return {"status": "rejected", "bed_id": top_bed}

    ta_results = pending["ta_results"]
    task_plan = pending["task_plan"]
    if await should_run_task("ta_confirm_reservation", "sa_bed_reservation", ta_results, task_plan, sid):
        await confirm_bed_reservation(BedApprovalInput(session_id=sid, bed_id=top_bed))
        ta_results["ta_confirm_reservation"] = {"bed_id": top_bed}
    if task_plan and await should_run_task("ta_sync_bed_status", "sa_bed_reservation", ta_results, task_plan, sid):
        ta_results["ta_sync_bed_status"] = await sync_bed_status(
            SyncBedStatusInput(session_id=sid, bed_ids=[top_bed], status="reserved"))
    return {"status": "completed", "bed_id": top_bed, "recommendation": pending.get("recommendation")}


async def _finalize_batch(sid: str, pending: dict, decision: str) -> dict:
    assignments = pending["assignments"]
    if decision != "approved":
        await release_batch_locks(sid, assignments)
        if decision == "timeout":
            return {"status": "timeout", "error": "Batch approval timed out"}
        return {"status": "rejected", "beds_requested": len(assignments)}

    ta_results = pending["ta_results"]
    task_plan = pending["task_plan"]
    if await should_run_task("ta_confirm_reservation", "sa_bed_reservation", ta_results, task_plan, sid):
        confirmed = await confirm_batch_reservations(sid, assignments)
        ta_results["ta_confirm_reservation"] = confirmed
    if task_plan and await should_run_task("ta_sync_bed_status", "sa_bed_reservation", ta_results, task_plan, sid):
        _bed_ids = (ta_results.get("ta_confirm_reservation") or {}).get("bed_ids") or [
            a.get("bed_id") for a in assignments if a.get("bed_id")
        ]
        ta_results["ta_sync_bed_status"] = await sync_bed_status(
            SyncBedStatusInput(session_id=sid, bed_ids=_bed_ids, status="reserved"))

    confirmed = ta_results.get("ta_confirm_reservation", {})
    return {
        "status": "completed",
        "beds_reserved": confirmed.get("beds_reserved", 0),
        "icu_beds_reserved": confirmed.get("icu_beds_reserved", 0),
        "bed_ids": confirmed.get("bed_ids", []),
        "bed_names": confirmed.get("bed_names", []),
    }


# -- Approval initiators (first-run path) -------------------------------------

async def _start_single(sid: str, candidates: list, ta_results: dict, task_plan: dict,
                        award_note: str = "") -> dict:
    if await should_run_task("ta_rank_beds", "sa_bed_ranking", ta_results, task_plan, sid):
        ranking = await rank_beds_activity(RankBedsInput(session_id=sid, candidates=candidates[:20]))
        ta_results["ta_rank_beds"] = ranking

    ranking = ta_results.get("ta_rank_beds", {})
    ranked_beds = ranking.get("ranked_beds", [])
    if not ranked_beds:
        return {"status": "failed", "error": "No beds returned from ranking"}

    top_bed = ranked_beds[0]["bed_id"]

    # Soft-hold the top bed when the planner selected this task (goal mentions a patient
    # en route) -- keeps the bed off the pool while the approval is pending.
    if await should_run_task("ta_hold_bed_temporarily", "sa_bed_reservation", ta_results, task_plan, sid):
        ta_results["ta_hold_bed_temporarily"] = await hold_bed_temporarily(
            HoldBedInput(session_id=sid, bed_id=top_bed))

    if await should_run_task("ta_create_approval", "sa_bed_reservation", ta_results, task_plan, sid):
        await create_bed_approval(BedApprovalInput(session_id=sid, bed_id=top_bed))
        ta_results["ta_create_approval"] = {"bed_id": top_bed}

    if not ta_results.get("ta_create_approval"):
        return {"status": "completed", "bed_id": top_bed, "recommendation": ranking.get("recommendation")}

    await hitl.save_pending(sid, "bed_agent", {
        "mode": "single", "top_bed": top_bed, "recommendation": ranking.get("recommendation"),
        "ta_results": _trim_ta(ta_results), "task_plan": task_plan,
    })
    await emit_step_recommendation(
        sid, agent_id="bed_agent", kind="bed_reservation",
        headline=f"Reserve bed {top_bed}",
        actions=[f"Reserve bed {top_bed} for the incoming patient"],
        rationale=" ".join(x for x in (
            award_note,
            (ranking.get("recommendation") or (ranked_beds[0].get("reason") if ranked_beds else "")),
        ) if x),
        risk="medium",
        extras={"bed_id": top_bed},
    )
    hitl.await_decision({"kind": "bed_approval", "session_id": sid, "agent_id": "bed_agent",
                         "action_type": "bed_reservation", "risk": "medium", "bed_id": top_bed})


async def _start_batch(sid: str, critical_patients: list, candidates: list, ta_results: dict,
                       task_plan: dict, award_note: str = "") -> dict:
    if await should_run_task("ta_rank_beds", "sa_bed_ranking", ta_results, task_plan, sid):
        assignments = await find_beds_for_patients(BatchAssignmentInput(
            session_id=sid, critical_patients=critical_patients, available_beds=candidates))
        ta_results["ta_rank_beds"] = {"assignments": assignments or []}

    assignments = (ta_results.get("ta_rank_beds") or {}).get("assignments") or []
    if not assignments:
        return {"status": "failed", "error": "No beds could be matched to critical patients"}

    if await should_run_task("ta_create_approval", "sa_bed_reservation", ta_results, task_plan, sid):
        await execute_activity(create_batch_bed_approval, args=[sid, assignments])
        ta_results["ta_create_approval"] = {"count": len(assignments)}

    if not ta_results.get("ta_create_approval"):
        return {"status": "completed", "beds_matched": len(assignments)}

    await hitl.save_pending(sid, "bed_agent", {
        "mode": "batch", "assignments": assignments,
        "ta_results": _trim_ta(ta_results), "task_plan": task_plan,
    })
    await emit_step_recommendation(
        sid, agent_id="bed_agent", kind="bed_reservation",
        headline=f"Reserve {len(assignments)} bed(s) for critical patients",
        actions=[
            (f"Reserve {(a.get('bed') or {}).get('ward', '')} "
             f"{(a.get('bed') or {}).get('bed_number', a.get('bed_id'))}").strip()
            for a in assignments[:3]
        ],
        rationale=" ".join(x for x in (
            award_note,
            f"{len(assignments)} critical patient(s) matched to available beds by triage priority",
        ) if x),
        risk="high",
        extras={"count": len(assignments)},
    )
    hitl.await_decision({"kind": "bed_approval", "session_id": sid, "agent_id": "bed_agent",
                         "action_type": "bed_reservation", "risk": "high", "count": len(assignments)})


# -- Body ----------------------------------------------------------------------

async def bid_bed(sid: str, ctx: dict) -> dict:
    """Bid hook for the bidding execution strategy (services.strategies).

    No side effects -- only reads the upstream context the body would act on and
    returns a contention score. Higher = wants the contested bed(s) more. Scores
    on demand pressure: how many critical/step-down/escalation patients are
    waiting upstream, weighted toward acuity (ER critical > ICU escalation >
    step-down). Returns 0 when this agent has nothing to act on (abstain).
    """
    er_critical   = len((ctx.get("er_agent")  or {}).get("critical_patients", []))
    icu_ctx       = ctx.get("icu_agent") or {}
    escalation    = len(icu_ctx.get("escalation_candidates", []))
    step_down     = len(icu_ctx.get("step_down_candidates", []))

    # Acuity weighting: ER critical patients are the most time-sensitive.
    score = 3.0 * er_critical + 2.0 * escalation + 1.0 * step_down
    return {
        "score":   float(score),
        "demand":  {"er_critical": er_critical, "escalation": escalation, "step_down": step_down},
        "reason":  "bed contention demand score",
    }


async def run_bed_body(sid: str, ctx: dict) -> dict:
    base = "bed_agent"
    pending = await hitl.load_pending(sid, base)
    if pending is not None:
        decision = hitl.await_decision({"kind": "bed_approval", "session_id": sid, "agent_id": base})
        await hitl.clear_pending(sid, base)
        if pending["mode"] == "batch":
            return await _finalize_batch(sid, pending, decision)
        return await _finalize_single(sid, pending, decision)

    task_type = ctx.get("_task_type", "")
    bed_limit = ctx.get("_bed_limit")
    goal = ctx.get("_goal", "")
    release_instance = _is_bed_release_instance(ctx.get("_agent_instance", ""))
    ta_results: dict = {}

    _raw_plan = ctx.get("_task_plan")
    task_plan: dict | None = dict(_raw_plan) if _raw_plan is not None else None
    if task_plan is not None and goal:
        seed_planned_slots(task_plan, _BED_TASKS)

    # -- CLEANING FLOW ----------------------------------------------------------
    if task_type == "bed_cleaning":
        await plan_subagent("bed_agent", "sa_dirty_bed_recovery", {}, task_plan, ta_results, goal, sid)
        if task_plan:
            vacated_beds = (ta_results.get("ta_clean_vacated_beds") or {}).get("beds", [])
            if await should_run_task("ta_dispatch_housekeeping_fast_track", "sa_dirty_bed_recovery", ta_results, task_plan, sid):
                ta_results["ta_dispatch_housekeeping_fast_track"] = await dispatch_housekeeping_fast_track(
                    EmergencyCleaningInput(session_id=sid, dirty_beds=vacated_beds))
            await _run_cleaning_completion(sid, ta_results, task_plan)
        return {"status": "completed", "agent_id": "bed_agent", **ta_results.get("ta_clean_vacated_beds", {})}

    # -- AVAILABILITY -- query beds --------------------------------------------
    if task_plan is not None:
        await plan_subagent("bed_agent", "sa_bed_availability", {}, task_plan, ta_results, goal, sid)
    if task_plan and subagent_in_plan("sa_bed_availability", task_plan):
        if await should_run_task("ta_query_beds", "sa_bed_availability", ta_results, task_plan, sid):
            if _goal_states_zero_clean_beds(goal):
                # G47: skip the guaranteed-empty clean-bed search (and the prefetch
                # cache); inject known-zero counts so dirty-bed recovery still fires.
                ta_results["ta_query_beds"] = await query_beds(QueryBedsInput(session_id=sid, known_zero_clean=True))
            else:
                cached = await get_prefetch_cache(GetPrefetchInput(session_id=sid, task_id="ta_query_beds"))
                if cached and cached.get("candidates") is not None:
                    _c = cached["candidates"]
                    ta_results["ta_query_beds"] = {"candidate_count": len(_c), **_type_counts(_c), "candidates": _c}
                else:
                    ta_results["ta_query_beds"] = await query_beds(QueryBedsInput(session_id=sid))

        if await should_run_task("ta_check_dirty_icu_beds", "sa_bed_availability", ta_results, task_plan, sid):
            ta_results["ta_check_dirty_icu_beds"] = await check_dirty_icu_beds(sid)
        if await should_run_task("ta_check_dirty_soon_to_release", "sa_bed_availability", ta_results, task_plan, sid):
            ta_results["ta_check_dirty_soon_to_release"] = await check_dirty_soon_to_release(sid)
        if await should_run_task("ta_check_overflow_candidates", "sa_bed_availability", ta_results, task_plan, sid):
            ta_results["ta_check_overflow_candidates"] = await check_overflow_candidates(sid)
    else:
        # Legacy path -- no task_plan
        if await should_run_task("ta_find_available_beds", "sa_bed_availability", ta_results, task_plan, sid):
            cached = await get_prefetch_cache(GetPrefetchInput(session_id=sid, task_id="ta_query_beds"))
            candidates_list = cached.get("candidates") if cached else None
            if candidates_list is None:
                candidates_list = await find_available_beds(sid)
            ta_results["ta_find_available_beds"] = {"candidates": candidates_list or []}

    # G16: escalate to command centre as soon as query_beds shows no capacity,
    # while dirty-bed recovery and overflow checks continue below.
    if (ta_results.get("ta_query_beds") or {}).get("candidate_count") == 0:
        if task_plan is not None:
            await plan_subagent("bed_agent", "sa_escalation", _BED_TASKS, task_plan, ta_results, goal, sid)
        if subagent_in_plan("sa_escalation", task_plan):
            if await should_run_task("ta_escalate_to_command_center", "sa_escalation", ta_results, task_plan, sid):
                await escalate_to_command_center(NotifyInput(session_id=sid, message="No available beds -- escalating to command centre while continuing search."))
                ta_results["ta_escalate_to_command_center"] = {"escalated": True}

    # -- DIRTY BED FAST-TRACK ------------------------------------------------
    dirty_beds = (ta_results.get("ta_check_dirty_icu_beds") or {}).get("dirty_beds", [])
    if task_plan is not None:
        await plan_subagent("bed_agent", "sa_dirty_bed_recovery", {}, task_plan, ta_results, goal, sid)
    if subagent_in_plan("sa_dirty_bed_recovery", task_plan, ta_results):
        _cleaning_inp = EmergencyCleaningInput(session_id=sid, dirty_beds=dirty_beds)
        if await should_run_task("ta_create_emergency_cleaning_task", "sa_dirty_bed_recovery", ta_results, task_plan, sid):
            ta_results["ta_create_emergency_cleaning_task"] = await create_emergency_cleaning_task(_cleaning_inp)
        if await should_run_task("ta_dispatch_housekeeping_fast_track", "sa_dirty_bed_recovery", ta_results, task_plan, sid):
            ta_results["ta_dispatch_housekeeping_fast_track"] = await dispatch_housekeeping_fast_track(_cleaning_inp)
        await _run_cleaning_completion(sid, ta_results, task_plan)

    candidates = (
        (ta_results.get("ta_query_beds") or {}).get("candidates")
        or (ta_results.get("ta_find_available_beds") or {}).get("candidates")
        or []
    )
    if dirty_beds:
        candidates = candidates + dirty_beds

    # Emergency fallback: when nothing is free at all, surface temporary surge capacity.
    # Only truly-available surge-area beds are promoted into the placement pool; dirty
    # fast-track beds remain informational in ta_results.
    if not candidates and task_plan and subagent_in_plan("sa_bed_availability", task_plan):
        if await should_run_task("ta_check_temporary_overflow_beds", "sa_bed_availability", ta_results, task_plan, sid):
            overflow = await check_temporary_overflow_beds(sid)
            ta_results["ta_check_temporary_overflow_beds"] = overflow
            usable = [b for b in overflow.get("candidates", []) if b.get("status") == "Available"]
            if usable:
                candidates = usable

    if not candidates:
        if task_plan is not None:
            await plan_subagent("bed_agent", "sa_discharge_coordination", {}, task_plan, ta_results, goal, sid)
            await plan_subagent("bed_agent", "sa_escalation", _BED_TASKS, task_plan, ta_results, goal, sid)
        if task_plan:
            if subagent_in_plan("sa_discharge_coordination", task_plan):
                if await should_run_task("ta_trigger_discharge_coordination", "sa_discharge_coordination", ta_results, task_plan, sid):
                    ta_results["ta_trigger_discharge_coordination"] = await trigger_discharge_coordination(sid)
            if subagent_in_plan("sa_escalation", task_plan):
                if await should_run_task("ta_escalate_allocation_conflict", "sa_escalation", ta_results, task_plan, sid):
                    await escalate_allocation_conflict(NotifyInput(session_id=sid, message="No available beds -- allocation conflict escalated."))
                    ta_results["ta_escalate_allocation_conflict"] = {"escalated": True}
                if "ta_escalate_to_command_center" not in ta_results and await should_run_task("ta_escalate_to_command_center", "sa_escalation", ta_results, task_plan, sid):
                    await escalate_to_command_center(NotifyInput(session_id=sid, message="No available beds -- command center notified."))
                    ta_results["ta_escalate_to_command_center"] = {"escalated": True}
        return {
            "status": "completed", "mode": "availability_check", "available_beds": 0,
            "reservation_made": False, "message": "No available beds found",
        }

    if task_type == "availability_check":
        return _availability_report(candidates)

    # -- Apply bed filters ----------------------------------------------------
    if task_plan is not None:
        await plan_subagent("bed_agent", "sa_bed_ranking", _BED_TASKS, task_plan, ta_results, goal, sid)
        await plan_subagent("bed_agent", "sa_bed_reservation", {}, task_plan, ta_results, goal, sid)
    if task_plan and subagent_in_plan("sa_bed_ranking", task_plan):
        if await should_run_task("ta_filter_ventilator_beds", "sa_bed_ranking", ta_results, task_plan, sid):
            result = await filter_ventilator_beds(FilterBedsInput(session_id=sid, candidates=candidates))
            ta_results["ta_filter_ventilator_beds"] = result
            candidates = result.get("candidates") or candidates
        if await should_run_task("ta_filter_isolation_beds", "sa_bed_ranking", ta_results, task_plan, sid):
            result = await filter_isolation_beds(FilterBedsInput(session_id=sid, candidates=candidates))
            ta_results["ta_filter_isolation_beds"] = result
            candidates = result.get("candidates") or candidates
        if await should_run_task("ta_apply_isolation_room_filter", "sa_bed_ranking", ta_results, task_plan, sid):
            result = await apply_isolation_room_filter(FilterBedsInput(session_id=sid, candidates=candidates))
            ta_results["ta_apply_isolation_room_filter"] = result
            candidates = result.get("candidates") or candidates
        if await should_run_task("ta_trigger_alternate_ward_search", "sa_bed_ranking", ta_results, task_plan, sid):
            result = await trigger_alternate_ward_search(FilterBedsInput(session_id=sid, candidates=candidates))
            ta_results["ta_trigger_alternate_ward_search"] = result
            candidates = result.get("candidates") or candidates

    def _cap(patients: list) -> list:
        return patients[:bed_limit] if bed_limit is not None else patients

    # RL bed auction (advisory): if an auction awarded a bed in this session to a specific
    # patient, prioritize that patient in whichever reservation branch fires (reserved first,
    # survives the bed_limit cap). Approval stays HITL. Best-effort -- a disabled/unavailable
    # gateway or a no-match leaves selection unchanged.
    awarded_tokens: set = set()
    award_note = ""
    try:
        from rl_gateway.award import read_awards
        _awards = await read_awards(sid)
        awarded_tokens = {a.get("patient_token") for a in _awards.values() if a.get("patient_token")}
        award_note = _award_note(_awards)
    except Exception:
        logger.debug("rl award read skipped", exc_info=True)

    def _pick(patients: list) -> list:
        return _cap(_prioritize_awarded(patients, awarded_tokens))

    # -- BATCH FLOW -- ER critical patients -------------------------------------
    critical_patients = (ctx.get("er_agent", {}).get("critical_patients", []) if ctx else [])
    if critical_patients:
        return await _start_batch(sid, _pick(critical_patients), candidates, ta_results, task_plan, award_note)

    # -- BATCH FLOW -- ICU patients (step-down or escalation) -------------------
    # G41: skip on a bed-RELEASE (post-discharge) instance -- ICU step-down /
    # escalation candidates are ICU-side placements, not next patients for a bed
    # being freed by discharge. Reserving for them here would lock a bed for a
    # patient who does not exist in this release flow. The vacated bed's status is
    # already reconciled by the availability / dirty-bed-recovery path above.
    icu_ctx = ctx.get("icu_agent", {})
    if not release_instance and icu_ctx.get("mode") != "capacity_check":
        step_down_patients = icu_ctx.get("step_down_candidates", [])
        if step_down_patients:
            return await _start_batch(sid, _pick(step_down_patients), candidates, ta_results, task_plan, award_note)
        escalation_patients = icu_ctx.get("escalation_candidates", [])
        if escalation_patients:
            return await _start_batch(sid, _pick(escalation_patients), candidates, ta_results, task_plan, award_note)

    # -- BOUND-PATIENT FLOW -- reserve for the flow's specific patient(s) --------
    # No upstream batch patients: this reservation is for the patient(s) the flow
    # is about. Identity is established upstream by patient_verification_agent (single
    # identification point; the planner injects it before any bed_reservation), so read
    # the resolved contexts from cache, then reserve N beds (batch) or one (single).
    if task_type != "bed_reservation":
        return _availability_report(candidates)

    bound_patients = await patient.get_cached(sid)

    _dynamic = await run_dynamic_tasks("bed_agent", task_plan, ta_results, sid)
    if len(bound_patients) > 1:
        _result = await _start_batch(sid, _pick(bound_patients), candidates, ta_results, task_plan, award_note)
    else:
        _result = await _start_single(sid, candidates, ta_results, task_plan, award_note)
    # _start_single / _start_batch raise GraphInterrupt when an approval is created;
    # they only return for the no-approval / failed / no-ranking early exits.
    if _result and _dynamic:
        _result["dynamic_tasks"] = _dynamic
    return _result
