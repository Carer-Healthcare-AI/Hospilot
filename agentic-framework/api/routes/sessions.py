import logging
import uuid

from fastapi import APIRouter, HTTPException, Depends
from api.routes.auth import AuthContext, require_active_user, require_role
from api.routes._authz import authorized_session

from schemas.models import (
    CreateSessionRequest, ExecuteSessionRequest, ReorchestrationRequest,
    PlanDecisionRequest, UpdateSessionPipelineRequest, IdentifyPatientRequest,
    RenameSessionRequest,
    EditResumeRequest,
)
from agents._shared.guardrail import validate_prompt
from workflows.planner import (
    generate_pipeline_staged, select_subagents, plan_subagent_tasks, PlanSubagentInput, fetch_registry,
    build_graph_context, goal_with_feedback_history, _synthesize_display_edges,
    _agents_requiring_patient, _PV_AGENT_ID,
)
from workflows.materializer import materialize_preplans
from db.hasura import hasura
from db.fabric import fpost, fpatch
from cache import redis as cache
from api.routes.ws import broadcast
from workflows.graph.runner import (
    start_session, start_planning, resume_planning, resume_patient_identification,
    resume_paused_session, cancel_session, list_checkpoints, edit_resume_session,
)

logger = logging.getLogger("sessions")
router = APIRouter()




def _build_pipeline_context(pipeline: dict, exclude_agent_id: str) -> str:
    """Graph-aware context for reorchestration prompts: the agent graph plus the other
    agents grouped upstream/downstream by hop distance. At reorchestration the approved
    pipeline is fully populated, so include each agent's selected sub-agents too."""
    return build_graph_context(pipeline, exclude_agent_id, include_subagents=True)


def _org_for(ctx: AuthContext, org_id: str | None = None) -> str | None:
    """Effective tenant for hasura routing: org users are pinned to their own
    org; super_admin may target another via the ?org_id= query param."""
    return org_id if ctx.is_super() else ctx.org_id


@router.post("/sessions")
async def create_session(
    body: CreateSessionRequest,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_role("doctor", "admin")),
):
    logger.info('POST /sessions  goal="%s"  user=%s', body.goal[:80], ctx.username)
    # Org users are pinned to their own tenant; a platform super_admin targets one
    # via ?org_id= (same convention as every other session write route).
    org = _org_for(ctx, org_id)
    if not org:
        raise HTTPException(status_code=400,
                            detail="super_admin must target an org (?org_id=) to run workflows")

    # 1. Guardrail check (Claude Haiku -- fast)
    guard = await validate_prompt(body.goal, body.constraints)
    if not guard["valid"]:
        logger.warning('blocked  reason="%s"', guard["reason"])
        raise HTTPException(
            status_code=400,
            detail={"error": guard["reason"], "blocked": True},
        )

    # 2. Create the session row in the caller's tenant source (pipeline is
    #    produced inside the planning graph).
    session_id = str(uuid.uuid4())
    await hasura.create_session(
        session_id=session_id,
        goal=body.goal,
        constraints=body.constraints,
        pipeline={},
        user_id=ctx.user_id,
        autonomous=body.autonomous,
        org_id=org,
    )
    # create_session inserts status="pending"; planning progress is conveyed via the
    # WS plan_stage_* / plan_awaiting_approval events (the DB status enum is fixed).

    # 3. Plan INSIDE the LangGraph flow: 3 focused stages (agents -> sub-agents ->
    #    tasks) then an approval pause -- UNLESS autonomous, in which case the
    #    planning graph auto-approves its own plan and launches execution in the
    #    background with no human wait (Phase 3). Either way this returns instantly;
    #    the frontend listens on the WS for plan_stage_* events.
    await start_planning(session_id, body.goal, body.constraints,
                         autonomous=body.autonomous, org_id=org)
    logger.info("[ok] session created -- planning started  id=%s  autonomous=%s",
                session_id, body.autonomous)

    return {"session_id": session_id, "status": "planning", "autonomous": body.autonomous}


@router.post("/sessions/{session_id}/plan-decision")
async def plan_decision(
    session_id: str, body: PlanDecisionRequest,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Resume the parked planning graph: approve / edit / reorchestrate.

    approve  -> execution auto-starts on the approved plan.
    edit     -> execution auto-starts on the user's edited pipeline.
    reorchestrate -> planning re-runs (with feedback) and re-pauses for approval.
    """
    org = _org_for(ctx, org_id)
    session = await authorized_session(session_id, ctx, owner_or_admin=True, org_id_hint=org)
    if body.action not in ("approve", "edit", "reorchestrate"):
        raise HTTPException(status_code=400, detail="action must be approve|edit|reorchestrate")

    decision: dict = {"action": body.action}
    if body.pipeline is not None:
        decision["pipeline"] = body.pipeline
    if body.feedback:
        decision["feedback"] = body.feedback

    # Persist an explicit patient-verification removal so it survives reorchestration.
    # If the user's edited plan drops patient_verification while still holding a task
    # that would otherwise force it back, honor that removal on future re-plans; if
    # they keep/add it, clear the suppression.
    if body.action == "edit" and body.pipeline is not None:
        _agents = body.pipeline.get("agents", [])
        _has_pv = any(a.get("id") == _PV_AGENT_ID for a in _agents)
        _pv_key = f"session:{session_id}:pv_suppressed"
        if _agents_requiring_patient(_agents, body.pipeline.get("edges", [])) and not _has_pv:
            await cache.set(_pv_key, True, ttl=86400)
        elif _has_pv:
            await cache.delete(_pv_key)

    if body.action == "reorchestrate":
        # Audit the feedback so the log stays the single source of the user's revisions:
        # this path keeps its history in the planning checkpoint (PlanState.feedback_history),
        # which /reorchestrate cannot see -- without the row a later reorchestration would
        # replan without the revisions asked for here.
        if body.feedback:
            await hasura.write_audit(session_id, "system", "reorchestrated",
                                     {"feedback": body.feedback, "scope": "plan_approval"}, org_id=org)
        # ...and hand the graph the same two things back, because the traffic also flows the
        # other way: rounds done through /reorchestrate never reached the planning checkpoint,
        # so its own feedback_history and pipeline are stale. The audit log (now including this
        # round) and the persisted pipeline are the complete picture.
        decision["feedback_history"] = await hasura.list_reorchestration_feedback(session_id, org_id=org)
        decision["prior_plan"] = session.get("pipeline") or {}

    await resume_planning(session_id, decision)
    logger.info("[ok] plan decision  session=%s  action=%s", session_id, body.action)
    return {
        "session_id": session_id,
        "action":     body.action,
        "status":     "planning" if body.action == "reorchestrate" else "running",
    }


@router.post("/sessions/{session_id}/pause")
async def pause_session(
    session_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Cooperatively pause a running flow (Phase 4).

    Sets a Redis pause signal; the runner's drive loop parks the flow at its next step
    boundary (checkpoint intact) and moves it into the Paused queue. Rejects flows that
    are not currently running (nothing to park)."""
    await authorized_session(session_id, ctx, owner_or_admin=True,
                             org_id_hint=_org_for(ctx, org_id))
    if session_id not in await cache.get_running_session_ids():
        raise HTTPException(status_code=409, detail="Session is not currently running")

    await cache.request_pause(session_id)
    await broadcast(session_id, {"type": "session_pause_requested", "session_id": session_id})
    logger.info("[ok] pause requested  session=%s  user=%s", session_id, ctx.username)
    return {"session_id": session_id, "status": "pause_requested"}


@router.post("/sessions/{session_id}/resume")
async def resume_session_endpoint(
    session_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Resume a user-paused flow from its LangGraph checkpoint (Phase 4)."""
    await authorized_session(session_id, ctx, owner_or_admin=True,
                             org_id_hint=_org_for(ctx, org_id))
    if session_id not in await cache.get_paused_session_ids():
        raise HTTPException(status_code=409, detail="Session is not paused")

    await resume_paused_session(session_id)
    await broadcast(session_id, {"type": "session_resumed", "session_id": session_id})
    logger.info("[ok] resume  session=%s  user=%s", session_id, ctx.username)
    return {"session_id": session_id, "status": "resuming"}


@router.post("/sessions/{session_id}/cancel")
async def cancel_session_endpoint(
    session_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Stop a flow outright (Phase 4): cancel the driving task, drop it from both queues,
    resolve pending rows, and mark the session cancelled. Idempotent."""
    await authorized_session(session_id, ctx, owner_or_admin=True,
                             org_id_hint=_org_for(ctx, org_id))

    await cancel_session(session_id)
    await broadcast(session_id, {"type": "session_cancelled", "session_id": session_id})
    logger.info("[ok] cancel  session=%s  user=%s", session_id, ctx.username)
    return {"session_id": session_id, "status": "cancelled"}


@router.get("/sessions/{session_id}/checkpoints")
async def session_checkpoints(
    session_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """List the flow's revert points (one per executed superstep) for the edit-resume UI.

    Each entry carries the checkpoint_id to pass to /edit-resume, the agents completed as
    of that point, and the nodes pending next."""
    await authorized_session(session_id, ctx, owner_or_admin=True,
                             org_id_hint=_org_for(ctx, org_id))
    return {"checkpoints": await list_checkpoints(session_id)}


@router.post("/sessions/{session_id}/edit-resume")
async def edit_resume_endpoint(
    session_id: str,
    body: EditResumeRequest,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Revert a PAUSED flow to a chosen checkpoint, swap in an edited pipeline, and re-run
    from that point -- preserving the agents completed up to the checkpoint (they are not
    re-executed). checkpoint_id omitted -> latest paused checkpoint."""
    await authorized_session(session_id, ctx, owner_or_admin=True,
                             org_id_hint=_org_for(ctx, org_id))
    if session_id not in await cache.get_paused_session_ids():
        raise HTTPException(status_code=409, detail="Session is not paused")
    if not isinstance(body.pipeline, dict) or not body.pipeline.get("agents"):
        raise HTTPException(status_code=400, detail="pipeline must be a dict with a non-empty 'agents' list")

    await edit_resume_session(session_id, body.pipeline, body.checkpoint_id)
    await broadcast(session_id, {"type": "session_resumed", "session_id": session_id,
                                 "reverted_to": body.checkpoint_id})
    logger.info("[ok] edit-resume  session=%s  cid=%s  user=%s",
                session_id, body.checkpoint_id, ctx.username)
    return {"session_id": session_id, "status": "resuming", "checkpoint_id": body.checkpoint_id}


@router.post("/sessions/{session_id}/identify-patient")
async def identify_patient(
    session_id: str, body: IdentifyPatientRequest,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Provide the patient(s) for a flow parked on the patient-identification
    interrupt, then resume execution. The resolved records (vitals/admission/name)
    are bound to the session by the graph on resume (see graph.patient)."""
    await authorized_session(session_id, ctx, owner_or_admin=True,
                             org_id_hint=_org_for(ctx, org_id))
    if not body.mobiles:
        raise HTTPException(status_code=400, detail="mobiles must not be empty")

    # Guard: the resume payload (a dict) must only reach a parked patient interrupt.
    # If a NON-patient interrupt (e.g. an approval) is currently parked, refuse --
    # both interrupt kinds share resume on the same thread_id. Best-effort:
    # if we can't determine the parked kind, proceed.
    if await _parked_non_patient_interrupt(session_id):
        raise HTTPException(
            status_code=409,
            detail="Session is not awaiting patient identification (a different "
                   "interrupt is parked). Resolve that first.",
        )

    await resume_patient_identification(session_id, body.mobiles)
    await broadcast(session_id, {
        "type": "patients_identified",
        "session_id": session_id,
        "mobiles": body.mobiles,
    })
    logger.info("[ok] patient(s) identified  session=%s  count=%d", session_id, len(body.mobiles))
    return {"session_id": session_id, "mobiles": body.mobiles, "status": "running"}


async def _parked_non_patient_interrupt(session_id: str) -> bool:
    """True only if we can positively determine that a parked interrupt exists and
    NONE of them are patient_identification. False on any uncertainty (best-effort)."""
    try:
        from workflows.graph.observability import get_checkpointer, run_config
        from workflows.graph.builder import build_session_graph
        from workflows.graph.runner import _load_pipeline, _parked_interrupt_kinds
        pipeline = await _load_pipeline(session_id)
        graph = build_session_graph(pipeline, get_checkpointer())
        kinds = await _parked_interrupt_kinds(graph, run_config(session_id))
        if kinds and "patient_identification" not in kinds:
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not inspect parked interrupt  session=%s: %s", session_id, exc)
    return False


def _overlay_stored_tasks(client_pipeline: dict, stored_pipeline: dict) -> None:
    """Overlay the server-persisted sub-agent task lists onto the client-sent pipeline.

    Every /reorchestrate scope persists the authoritative pipeline (incl. task-level
    conditions/selection) via update_session_pipeline, but execute_session binds from
    body.pipeline. With no UI to merge conditions client-side, a task-level edit made
    over the API would be dropped when the caller re-sends a pre-reorchestration
    pipeline. Server is authoritative: for every (agent, sub-agent) present in both,
    replace the client's tasks with the stored ones. No-op when nothing was reorchestrated.
    """
    stored_by_sa: dict[tuple, list] = {
        (a.get("id"), sa.get("id")): sa["tasks"]
        for a in stored_pipeline.get("agents", [])
        for sa in a.get("sub_agents", [])
        if sa.get("id") and sa.get("tasks") is not None
    }
    if not stored_by_sa:
        return
    for a in client_pipeline.get("agents", []):
        for sa in a.get("sub_agents", []):
            tasks = stored_by_sa.get((a.get("id"), sa.get("id")))
            if tasks is not None:
                sa["tasks"] = tasks


@router.post("/sessions/{session_id}/execute")
async def execute_session(
    session_id: str, body: ExecuteSessionRequest,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    logger.info("POST /sessions/%s/execute  overrides=%s", session_id, list(body.agent_task_overrides.keys()))
    org = _org_for(ctx, org_id)
    session = await authorized_session(session_id, ctx, owner_or_admin=True, org_id_hint=org)

    # 0. Reconcile task-level modifications: overlay the server-persisted pipeline
    #    (carrying conditions/selection set via /reorchestrate) onto body.pipeline,
    #    since execute binds from body.pipeline and there is no UI to merge them in.
    _overlay_stored_tasks(body.pipeline, (session or {}).get("pipeline") or {})

    # 1. Save task overrides to Hasura + Redis
    for agent_id, tasks in body.agent_task_overrides.items():
        await hasura.save_agent_overrides(session_id, agent_id, tasks, org_id=org)
        await cache.set_session_overrides(session_id, agent_id, tasks)
        logger.info("saved overrides  agent=%s  tasks=%d", agent_id, len(tasks))

    # 2. Update session status
    await hasura.update_session_status(
        session_id,
        status="running",
        pipeline_snapshot=body.pipeline,
        org_id=org,
    )

    # 2b. Bind the plan down to the tasks: materialize the pipeline's sub-agent/task
    #     selection into the per-instance subagent_preplan keys (Feature A). Sweep
    #     stale keys first so a re-run with a changed plan doesn't leak old bindings.
    await cache.delete_pattern(f"session:{session_id}:subagent_preplan:*")
    for node_id, preplan in materialize_preplans(body.pipeline).items():
        await cache.set(f"session:{session_id}:subagent_preplan:{node_id}", preplan, ttl=86400)

    agents = body.pipeline.get("agents", [])
    logger.info("session graph  session=%s  agents=%d", session_id, len(agents))

    await broadcast(session_id, {
        "type": "session_started",
        "total_agents": len(agents),
    })

    # 3. Build + launch the LangGraph session graph (fires prefetch internally).
    #    Replaces the topological execution plan + Redis barriers + Kafka dispatch.
    goal = body.pipeline.get("understood_goal", "")
    await start_session(session_id, body.pipeline, goal, org_id=org or "")

    return {"session_id": session_id, "status": "running"}


@router.post("/sessions/{session_id}/commit")
async def commit_session(
    session_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    org = _org_for(ctx, org_id)
    session = await authorized_session(session_id, ctx, owner_or_admin=True, org_id_hint=org)
    if await cache.get(f"session:{session_id}:awaiting_reorchestration"):
        raise HTTPException(
            status_code=409,
            detail="Session is awaiting reorchestration after a task failure -- "
                   "approve or edit the recommended plan and re-run before committing.",
        )
    snapshot = session.get("pipeline_snapshot") or {}

    # Idempotency -- _committed flag written into the snapshot on first commit
    if snapshot.get("_committed"):
        return {"message": "already committed", "session_id": session_id}

    # -- Beds: reserve (from pipeline snapshot) --------------------------------
    bed_ctx = snapshot.get("bed_agent", {})
    bed_ids: list[str] = []
    if bed_ctx.get("bed_ids"):
        raw = bed_ctx["bed_ids"]
        bed_ids = raw if isinstance(raw, list) else [raw]
    elif bed_ctx.get("bed_id"):
        bed_ids = [bed_ctx["bed_id"]]

    reserved_beds: list[str] = []
    failed_beds: list[str] = []
    for bed_id in bed_ids:
        change_id = str(uuid.uuid4())
        old_bed = await cache.get(f"bed:{bed_id}")
        await cache.save_commit_snapshot(change_id, f"bed:{bed_id}", old_bed, cache.BED_TTL)
        if old_bed:
            await cache.set(f"bed:{bed_id}", {**old_bed, "status": "reserved"}, ttl=cache.BED_TTL)
        try:
            await hasura.update_bed_status(bed_id, "reserved")
            reserved_beds.append(bed_id)
            # Snapshot stays alive -- ack consumer confirms/reverts when Fabric acks the DB write
        except Exception as exc:
            logger.warning("commit: failed to reserve bed %s: %s", bed_id, exc)
            failed_beds.append(bed_id)
            await cache.restore_and_delete_snapshot(change_id)

    # -- ICU: vacate source beds (from pipeline snapshot) ----------------------
    icu_ctx = snapshot.get("icu_agent", {})
    vacated_beds: list[str] = []
    failed_vacate: list[str] = []
    for candidate in icu_ctx.get("step_down_candidates", []):
        source_bed_id = candidate.get("source_bed_id")
        if source_bed_id:
            change_id = str(uuid.uuid4())
            old_bed = await cache.get(f"bed:{source_bed_id}")
            await cache.save_commit_snapshot(change_id, f"bed:{source_bed_id}", old_bed, cache.BED_TTL)
            if old_bed:
                await cache.set(f"bed:{source_bed_id}", {**old_bed, "status": "vacating"}, ttl=cache.BED_TTL)
            try:
                await hasura.update_bed_status(source_bed_id, "vacating")
                vacated_beds.append(source_bed_id)
                # Snapshot stays alive -- ack consumer confirms/reverts
            except Exception as exc:
                logger.warning("commit: failed to vacate ICU bed %s: %s", source_bed_id, exc)
                failed_vacate.append(source_bed_id)
                await cache.restore_and_delete_snapshot(change_id)

    # -- Discharge: update discharge_ready + free beds (from Redis staging) ----
    discharge_items = await cache.get_staged(session_id, "discharge") or []
    discharged_admissions: list[dict] = []
    failed_discharges: list[str] = []
    for item in discharge_items:
        admission_id = item["admission_id"]

        adm_change_id = str(uuid.uuid4())
        old_adm = await cache.get(f"admission:{admission_id}")
        await cache.save_commit_snapshot(adm_change_id, f"admission:{admission_id}", old_adm, cache.ADMISSION_TTL)
        if old_adm:
            await cache.set(f"admission:{admission_id}", {**old_adm, "discharge_ready": item["discharge_ready"]}, ttl=cache.ADMISSION_TTL)

        bed_change_id = None
        if item["discharge_ready"] and item.get("bed_id"):
            bed_change_id = str(uuid.uuid4())
            old_bed = await cache.get(f"bed:{item['bed_id']}")
            await cache.save_commit_snapshot(bed_change_id, f"bed:{item['bed_id']}", old_bed, cache.BED_TTL)
            if old_bed:
                await cache.set(f"bed:{item['bed_id']}", {**old_bed, "status": "Available"}, ttl=cache.BED_TTL)

        try:
            await hasura.update_discharge_ready(
                admission_id=admission_id,
                ready=item["discharge_ready"],
                blocked_reason=item.get("blocked_reason"),
            )
            if item["discharge_ready"] and item.get("bed_id"):
                await hasura.update_bed_status(item["bed_id"], "Available")
            discharged_admissions.append({
                "admission_id": admission_id,
                "discharge_ready": item["discharge_ready"],
                "bed_freed": item.get("bed_id") if item["discharge_ready"] else None,
                "blocked_reason": item.get("blocked_reason"),
            })
            # Snapshots stay alive -- ack consumer confirms/reverts
        except Exception as exc:
            logger.warning("commit: failed discharge %s: %s", admission_id, exc)
            failed_discharges.append(admission_id)
            await cache.restore_and_delete_snapshot(adm_change_id)
            if bed_change_id:
                await cache.restore_and_delete_snapshot(bed_change_id)

    # -- ICU transfers: set transfer_pending (from Redis staging) --------------
    icu_staged = await cache.get_staged(session_id, "icu") or {}
    transfer_ids = icu_staged.get("transfer_admission_ids", [])
    transferred_admissions: list[str] = []
    if transfer_ids:
        icu_change_ids: dict[str, str] = {}
        for adm_id in transfer_ids:
            change_id = str(uuid.uuid4())
            old_adm = await cache.get(f"admission:{adm_id}")
            await cache.save_commit_snapshot(change_id, f"admission:{adm_id}", old_adm, cache.ADMISSION_TTL)
            if old_adm:
                await cache.set(f"admission:{adm_id}", {**old_adm, "status": "transfer_pending"}, ttl=cache.ADMISSION_TTL)
            icu_change_ids[adm_id] = change_id
        try:
            await hasura.set_admissions_transfer_pending(transfer_ids)
            transferred_admissions = transfer_ids
            # Snapshots stay alive -- ack consumer confirms/reverts
        except Exception as exc:
            logger.warning("commit: failed ICU transfers: %s", exc)
            for cid in icu_change_ids.values():
                await cache.restore_and_delete_snapshot(cid)

    # -- Appointments: create + book slot (from Redis staging) -----------------
    # Fabric is async: snapshot is kept alive until hospilot.sync.ack confirms/rejects.
    appt_staged = await cache.get_staged(session_id, "appointments")
    # G4: confirm_booking stages a LIST of bookings; tolerate a legacy single dict.
    appt_bookings = appt_staged if isinstance(appt_staged, list) else ([appt_staged] if appt_staged else [])
    committed_appointments = 0
    for b in appt_bookings:
        slot_id = b.get("slot_id")
        if not slot_id:
            continue
        appt_change_id = str(uuid.uuid4())
        old_slot = await cache.get(f"doctor_slot:{slot_id}")
        await cache.save_commit_snapshot(appt_change_id, f"doctor_slot:{slot_id}", old_slot, cache.DOCTOR_SLOT_TTL)
        if old_slot:
            await cache.set(f"doctor_slot:{slot_id}", {**old_slot, "status": "booked"}, ttl=cache.DOCTOR_SLOT_TTL)
        try:
            created = await fpost("/appointments", body={
                "patient_id":       b.get("patient_id"),
                "provider_id":      b.get("provider_id"),
                "department_id":    None,
                "appointment_time": b.get("appt_time"),
                "status":           "Scheduled",
                "type":             "New Consultation",
                "patient_name":     b.get("patient_name"),
                "phone":            b.get("phone"),
                "email":            b.get("email"),
                "specialization":   b.get("specialization"),
                "change_id":        appt_change_id,
            })
            if created.get("id"):
                await fpatch(f"/appointments/slots/{slot_id}/book", body={"change_id": appt_change_id})
                committed_appointments += 1
                # Snapshot stays alive -- ack_consumer will delete or restore it
            else:
                await cache.restore_and_delete_snapshot(appt_change_id)
        except Exception as exc:
            logger.warning("commit: failed appointment: %s", exc)
            await cache.restore_and_delete_snapshot(appt_change_id)

    # -- Appointment reschedules (G14): book the new off-peak slot --------------
    # Fabric exposes create + book-slot but NO cancel/free endpoint, so we create
    # the appointment at the new slot and audit the original for manual release.
    resched_staged = await cache.get_staged(session_id, "appointment_reschedules") or []
    committed_reschedules = 0
    for p in resched_staged:
        new_slot_id = p.get("to_slot_id")
        if not new_slot_id:
            continue
        rs_change_id = str(uuid.uuid4())
        old_slot = await cache.get(f"doctor_slot:{new_slot_id}")
        await cache.save_commit_snapshot(rs_change_id, f"doctor_slot:{new_slot_id}", old_slot, cache.DOCTOR_SLOT_TTL)
        if old_slot:
            await cache.set(f"doctor_slot:{new_slot_id}", {**old_slot, "status": "booked"}, ttl=cache.DOCTOR_SLOT_TTL)
        try:
            created = await fpost("/appointments", body={
                "patient_id":       p.get("patient_id"),
                "provider_id":      p.get("to_provider_id"),
                "department_id":    None,
                "appointment_time": p.get("to_time"),
                "status":           "Scheduled",
                "type":             "Rescheduled",
                "patient_name":     p.get("patient_name"),
                "phone":            p.get("phone"),
                "email":            p.get("email"),
                "specialization":   p.get("specialization"),
                "change_id":        rs_change_id,
            })
            if created.get("id"):
                await fpatch(f"/appointments/slots/{new_slot_id}/book", body={"change_id": rs_change_id})
                committed_reschedules += 1
                await hasura.write_audit(session_id, "appointment_agent", "appointment_reschedule_committed",
                                         {"appointment_id": p.get("appointment_id"),
                                          "from_time": p.get("from_time"), "to_time": p.get("to_time"),
                                          "note": "original slot needs manual release in HIS (no Fabric cancel endpoint)"},
                                         org_id=org)
            else:
                await cache.restore_and_delete_snapshot(rs_change_id)
        except Exception as exc:
            logger.warning("commit: failed reschedule: %s", exc)
            await cache.restore_and_delete_snapshot(rs_change_id)

    # -- Service bookings (G23/G39): sample-collection / pharmacy-pickup --------
    # Service slots are not in Fabric, so create the appointment via Fabric and mark
    # the service slot booked via Hasura (no Fabric slot-book call).
    svc_staged = await cache.get_staged(session_id, "service_bookings") or []
    committed_service = 0
    for b in svc_staged:
        slot_id = b.get("slot_id")
        if not slot_id:
            continue
        sc_change_id = str(uuid.uuid4())
        try:
            created = await fpost("/appointments", body={
                "patient_id":       b.get("patient_id"),
                "provider_id":      None,
                "department_id":    None,
                "appointment_time": b.get("appt_time"),
                "status":           "Scheduled",
                "type":             b.get("slot_type") or "Service",
                "patient_name":     b.get("patient_name"),
                "phone":            b.get("phone"),
                "email":            b.get("email"),
                "specialization":   b.get("specialization"),
                "change_id":        sc_change_id,
            })
            if created.get("id"):
                await hasura.appt_book_service_slot(slot_id)
                committed_service += 1
        except Exception as exc:
            logger.warning("commit: failed service booking: %s", exc)

    # -- OT reschedules (G32): move a surgery to a derived open theatre slot -----
    # A reschedule is a single UPDATE to the surgery record (new date/time/room); moving
    # it frees the original slot automatically. Fabric queues a `surgery_reschedule`
    # PendingChange (approval_needed) that CarerOS pulls + applies to ot_surgeries.
    ot_resched = await cache.get_staged(session_id, "ot_reschedules") or []
    committed_ot_reschedules = 0
    for p in ot_resched:
        surgery_id = p.get("surgery_id")
        to = p.get("to") or {}
        if not surgery_id or not to.get("date"):
            continue
        ot_change_id = str(uuid.uuid4())
        try:
            resp = await fpost(f"/ot/surgery-schedule/{surgery_id}/reschedule", body={
                "scheduled_date":       to.get("date"),
                "scheduled_start_time": to.get("start"),
                "scheduled_end_time":   to.get("end"),
                "ot_room_id":           to.get("ot_room_id"),
                "status":               "Scheduled",
                "change_id":            ot_change_id,
            })
            if resp and resp.get("ok", True):
                committed_ot_reschedules += 1
                await hasura.write_audit(session_id, "ot_agent", "ot_reschedule_committed",
                                         {"surgery_id": surgery_id, "from": p.get("from"), "to": to},
                                         org_id=org)
        except Exception as exc:
            logger.warning("commit: failed OT reschedule: %s", exc)

    # -- Cleaned beds: mark Available (from Redis staging by mark_bed_ready) ----
    cleaned_bed_ids = await cache.get_staged(session_id, "cleaned_beds") or []
    cleaned_beds: list[str] = []
    failed_cleaned: list[str] = []
    for bed_id in cleaned_bed_ids:
        change_id = str(uuid.uuid4())
        old_bed = await cache.get(f"bed:{bed_id}")
        await cache.save_commit_snapshot(change_id, f"bed:{bed_id}", old_bed, cache.BED_TTL)
        if old_bed:
            await cache.set(f"bed:{bed_id}", {**old_bed, "status": "Available"}, ttl=cache.BED_TTL)
        try:
            await hasura.update_bed_status(bed_id, "Available")
            cleaned_beds.append(bed_id)
            # Snapshot stays alive -- ack consumer confirms/reverts when Fabric acks the DB write
        except Exception as exc:
            logger.warning("commit: failed to mark cleaned bed %s available: %s", bed_id, exc)
            failed_cleaned.append(bed_id)
            await cache.restore_and_delete_snapshot(change_id)

    # -- Billing: push bill-generation requests to the HIS (from Redis staging) -
    # Initiate-billing staged draft requests during the flow; on commit we hand them
    # to the DB side (hospilot.billing_requests, status 'pending') which creates the
    # actual bill for the patient. The DB side writes back invoice_id / status.
    billing_items = await cache.get_staged(session_id, "billing") or []
    created_billing_requests: list[str] = []
    failed_billing: list[str] = []
    if billing_items:
        try:
            rows = await hasura.create_billing_requests(session_id, billing_items)
            created_billing_requests = [r.get("id") for r in rows]
        except Exception as exc:
            logger.warning("commit: failed to create billing requests: %s", exc)
            failed_billing = [b.get("patient_token") for b in billing_items]

    # Clear all staged keys now that Fabric calls are done
    await cache.clear_staged(session_id)

    # Mark committed inside pipeline_snapshot
    await hasura.update_session_status(
        session_id,
        session["status"],
        pipeline_snapshot={**snapshot, "_committed": True},
        org_id=org,
    )
    logger.info(
        "[ok] commit  session=%s  beds=%s  icu_vacated=%s  discharges=%d  cleaned=%s  transfers=%s  appointments=%d  reschedules=%d  service=%d  billing=%d",
        session_id, reserved_beds, vacated_beds, len(discharged_admissions), cleaned_beds, transferred_admissions, committed_appointments, committed_reschedules, committed_service, len(created_billing_requests),
    )
    return {
        "session_id": session_id,
        "reserved_beds": reserved_beds,
        "failed_beds": failed_beds,
        "icu_vacated_beds": vacated_beds,
        "failed_vacate": failed_vacate,
        "discharged_admissions": discharged_admissions,
        "failed_discharges": failed_discharges,
        "cleaned_beds": cleaned_beds,
        "failed_cleaned": failed_cleaned,
        "transfer_pending_admissions": transferred_admissions,
        "appointments": appt_bookings if committed_appointments else [],
        "appointment_reschedules": resched_staged if committed_reschedules else [],
        "service_bookings": svc_staged if committed_service else [],
        "ot_reschedules": ot_resched if committed_ot_reschedules else [],
        "billing_requests": created_billing_requests,
        "failed_billing": failed_billing,
    }


@router.post("/sessions/{session_id}/reorchestrate")
async def reorchestrate_session(
    session_id: str, body: ReorchestrationRequest,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    org = _org_for(ctx, org_id)
    session = await authorized_session(session_id, ctx, owner_or_admin=True, org_id_hint=org)

    # Reorchestration REPLANS from the goal, so every earlier round's feedback has to be
    # replayed or round N silently reverts rounds 1..N-1 (e.g. "ICU is at 57%" snapping
    # back to the 92% in the original query). Replay the durable audit history, oldest
    # first, with this round's feedback appended last and marked as authoritative.
    history = await hasura.list_reorchestration_feedback(session_id, org_id=org)
    if body.feedback and (not history or history[-1] != body.feedback.strip()):
        history = history + [body.feedback.strip()]
    goal = goal_with_feedback_history(session["goal"], history)

    if body.agent_id is None:
        # -- Pipeline reorchestration ------------------------------------------
        # Revise the plan the user is currently looking at (which already carries the
        # earlier rounds) instead of rebuilding from the bare goal.
        pipeline = await generate_pipeline_staged(
            goal, session.get("constraints", ""),
            prior_plan=session.get("pipeline") or None, session_id=session_id,
        )
        await hasura.update_session_pipeline(session_id, pipeline, org_id=org)
        await hasura.write_audit(session_id, "system", "reorchestrated", {"feedback": body.feedback, "scope": "pipeline"}, org_id=org)
        await broadcast(session_id, {"type": "session_reorchestrated", "scope": "pipeline", "pipeline": pipeline})
        logger.info("[ok] reorchestrated pipeline  session=%s", session_id)
        return {"session_id": session_id, "scope": "pipeline", "pipeline": pipeline}

    agent_base_id = body.agent_id.split(":")[0]
    _, db_sub_agents, _, _ = await fetch_registry()
    subagents = db_sub_agents.get(agent_base_id)
    if not subagents:
        raise HTTPException(status_code=400, detail=f"Unknown agent_id: {agent_base_id}")

    # -- Task-level reorchestration --------------------------------------------
    if body.subagent_id:
        subagent = next((sa for sa in subagents if sa.id == body.subagent_id), None)
        if not subagent:
            raise HTTPException(status_code=400, detail=f"Unknown subagent_id: {body.subagent_id}")

        # Sub-goal was set on the sub-agent during stage-2 selection; read it from the
        # persisted pipeline so task selection knows what this sub-agent must achieve.
        pipeline = session.get("pipeline", {})
        cur_agent = next((a for a in pipeline.get("agents", []) if a["id"] == body.agent_id), None)
        cur_sa = next((sa for sa in (cur_agent or {}).get("sub_agents", []) if sa.get("id") == body.subagent_id), None)
        subgoal = (cur_sa or {}).get("subgoal", "")

        task_plan = await plan_subagent_tasks(PlanSubagentInput(
            agent_id=agent_base_id,
            subagent_id=body.subagent_id,
            available_tasks=[{"id": t.id, "label": t.label, "outputs": t.outputs} for t in subagent.tasks],
            goal=goal,
            session_id=session_id,
            subgoal=subgoal,
        ))

        # Merge into existing preplan so other subagents are unaffected
        preplan = await cache.get(f"session:{session_id}:subagent_preplan:{agent_base_id}") or {
            sa.id: {} for sa in subagents
        }
        preplan[body.subagent_id] = task_plan or {"__planned__": True}
        await cache.set(f"session:{session_id}:subagent_preplan:{agent_base_id}", preplan, ttl=3600)

        # Persist the selected tasks AND their conditions onto the pipeline. This is
        # the only carrier that survives to execution -- execute_session rebuilds the
        # preplan from body.pipeline via materialize_preplans (which reads
        # task["condition"]). Broadcasting/returning bare ids dropped the condition,
        # so a task-level "add a condition" edit never took effect.
        selected_tasks = [
            {"id": tid,
             "label": (entry.get("label") or tid),
             "condition": entry.get("condition"),
             "outputs": entry.get("outputs", [])}
            for tid, entry in (task_plan or {}).items()
        ]
        if cur_sa is not None:
            cur_sa["tasks"] = selected_tasks
            await hasura.update_session_pipeline(session_id, pipeline, org_id=org)

        await hasura.update_session_status(session_id, "pending", org_id=org)
        await hasura.write_audit(session_id, agent_base_id, "reorchestrated", {"feedback": body.feedback, "scope": "tasks", "subagent_id": body.subagent_id, "selected": [t["id"] for t in selected_tasks]}, org_id=org)
        await broadcast(session_id, {"type": "session_reorchestrated", "scope": "tasks", "agent_id": agent_base_id, "subagent_id": body.subagent_id, "selected_tasks": selected_tasks})
        logger.info("[ok] reorchestrated tasks  session=%s  agent=%s  subagent=%s  selected=%s", session_id, agent_base_id, body.subagent_id, [t["id"] for t in selected_tasks])
        return {"session_id": session_id, "scope": "tasks", "agent_id": agent_base_id, "subagent_id": body.subagent_id, "selected_tasks": selected_tasks}

    # -- Sub-agent selection / reordering ------------------------------------
    pipeline = session.get("pipeline", {})

    # Get current subagent order from pipeline for this exact agent_id (preserves :after_icu etc.)
    current_agent = next(
        (a for a in pipeline.get("agents", []) if a["id"] == body.agent_id),
        None,
    )
    current_sub_agents = (current_agent or {}).get("sub_agents", [])
    current_ids = [sa["id"] for sa in current_sub_agents]
    current_conditions = {sa["id"]: sa["condition"] for sa in current_sub_agents if sa.get("condition")}

    # Include current order and conditions so the LLM can see the full current state.
    # When feedback implies a branch, the LLM must select BOTH branch sub-agents from
    # the available list and assign them opposite registered condition tokens.
    augmented_goal = goal
    if current_ids:
        augmented_goal += (
            f"\n\nCurrent subagent order for {body.agent_id}: {current_ids}"
            f"\nApply the user feedback to reorder or adjust this list."
        )
    if current_conditions:
        augmented_goal += f"\n\nCurrent conditions: {current_conditions}"

    pipeline_context = _build_pipeline_context(pipeline, agent_base_id)
    selected_ids, subgoals, conditions = await select_subagents(agent_base_id, subagents, augmented_goal, pipeline_context)

    # Update pipeline in-place so frontend gets the new order immediately. Carry the
    # regenerated subgoals and any runtime conditions so materializer can pick them up.
    if current_agent is not None:
        current_agent["sub_agents"] = [
            {"id": sid, "subgoal": subgoals.get(sid, ""),
             **({"condition": conditions[sid]} if sid in conditions else {})}
            for sid in selected_ids
        ]
        _synthesize_display_edges(pipeline)
        await hasura.update_session_pipeline(session_id, pipeline, org_id=org)

    selected_set = set(selected_ids)
    subagent_preplan = {}
    for sa in subagents:
        if sa.id not in selected_set:
            subagent_preplan[sa.id] = {"__planned__": True}
        elif sa.id in conditions:
            subagent_preplan[sa.id] = {"__condition__": conditions[sa.id]}
        else:
            subagent_preplan[sa.id] = {}
    subagent_preplan["__subagent_order__"] = selected_ids

    await cache.set(f"session:{session_id}:subagent_preplan:{agent_base_id}", subagent_preplan, ttl=3600)
    await hasura.update_session_status(session_id, "pending", org_id=org)
    await hasura.write_audit(session_id, agent_base_id, "reorchestrated", {"feedback": body.feedback, "scope": "subagents", "selected": selected_ids}, org_id=org)
    sub_agent_edges = (current_agent or {}).get("sub_agent_edges", [])
    await broadcast(session_id, {"type": "session_reorchestrated", "scope": "subagents", "agent_id": body.agent_id, "selected_subagents": selected_ids, "conditions": conditions, "sub_agent_edges": sub_agent_edges})
    logger.info("[ok] reorchestrated subagents  session=%s  agent=%s  order=%s  conditions=%s", session_id, body.agent_id, selected_ids, conditions)
    return {"session_id": session_id, "scope": "subagents", "agent_id": body.agent_id, "selected_subagents": selected_ids, "conditions": conditions, "sub_agent_edges": sub_agent_edges}


@router.patch("/sessions/{session_id}")
async def update_session_pipeline(
    session_id: str, body: UpdateSessionPipelineRequest,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    org = _org_for(ctx, org_id)
    session = await authorized_session(session_id, ctx, owner_or_admin=True, org_id_hint=org)
    if session["status"] not in ("pending",):
        raise HTTPException(status_code=400, detail="Pipeline can only be updated on pending sessions")
    await hasura.update_session_pipeline(session_id, body.pipeline, org_id=org)
    logger.info("[ok] pipeline updated  session=%s", session_id)
    return {"session_id": session_id, "status": "ok"}


@router.get("/sessions")
async def list_sessions(
    limit: int = 50,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    # Scoping: doctors/approvers see their own sessions; admins the whole org;
    # super_admin one org via ?org_id= or all orgs merged.
    if ctx.is_super():
        if org_id:
            sessions = await hasura.list_sessions(limit, org_id=org_id)
        else:
            sessions = await hasura.list_sessions_all_orgs(limit)
    elif ctx.role == "admin":
        sessions = await hasura.list_sessions(limit, org_id=ctx.org_id)
    else:
        sessions = await hasura.list_sessions(limit, user_id=ctx.user_id, org_id=ctx.org_id)
    logger.info("GET /sessions  returned=%d  user=%s", len(sessions), ctx.username)
    return {"sessions": sessions}


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    return await authorized_session(session_id, ctx, org_id_hint=_org_for(ctx, org_id))


@router.patch("/sessions/{session_id}/name")
async def rename_session(
    session_id: str, body: RenameSessionRequest,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Rename a session (Workflows page). Owner or admin only. A blank name clears
    it back to null so the UI falls back to its "New Workflow" default."""
    org = _org_for(ctx, org_id)
    await authorized_session(session_id, ctx, owner_or_admin=True, org_id_hint=org)
    updated = await hasura.update_session_name(session_id, body.name, org_id=org)
    if not updated:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"session_id": session_id, "name": updated.get("name")}


@router.get("/sessions/{session_id}/pending-approvals")
async def get_pending_approvals(
    session_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    org = _org_for(ctx, org_id)
    await authorized_session(session_id, ctx, org_id_hint=org)
    return await hasura.fetch_pending_approvals(session_id, org_id=org)


@router.get("/sessions/{session_id}/trace")
async def get_session_trace(
    session_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Human-readable execution trace for the session: the ordered list of steps
    (agent / task / decision) with readable titles, summaries, and input/output
    fields. Mirrors the live `trace_step` WebSocket events, so a reconnecting or
    late-joining client can replay everything that happened."""
    await authorized_session(session_id, ctx, org_id_hint=_org_for(ctx, org_id))
    return {"session_id": session_id, "steps": await cache.get_trace(session_id)}


@router.get("/sessions/{session_id}/step-recommendations")
async def get_step_recommendations(
    session_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Mid-flow per-step recommendations for the session: the ordered list of
    recommendations emitted at each blocking step when it requested human input.
    Mirrors the live `step_recommendation` WebSocket events for replay."""
    await authorized_session(session_id, ctx, org_id_hint=_org_for(ctx, org_id))
    return {"session_id": session_id, "steps": await cache.get_step_recs(session_id)}
