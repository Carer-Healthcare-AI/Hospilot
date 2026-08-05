"""Session graph driver -- replaces the Kafka consumer loop + A2A self-HTTP.

start_session: build the per-session graph and drive it with astream until it
completes or parks on an approval interrupt.
resume_session: rebuild the SAME graph from the stored pipeline and resume the
parked thread with the approval decision (or "timeout" from the reaper).

WS events are emitted inside the nodes/synthesis (unchanged); astream is only a
pump. The executed pipeline is cached in Redis so resume rebuilds an identical
topology (node names must match the checkpoint).
"""

import asyncio
import logging
from contextlib import aclosing

from langgraph.types import Command

from api.routes.ws import broadcast
from cache import redis as cache
from config import settings
from db.hasura import hasura
from workflows.graph import policy
from workflows.graph.builder import build_session_graph
from workflows.graph.exec_context import set_exec_ctx
from workflows.graph.observability import get_checkpointer, run_config, flush_langfuse, reset_checkpoint_thread
from workflows.graph.planning_graph import build_planning_graph
from workflows.graph.prefetch import run_prefetch
from workflows.materializer import materialize_preplans

logger = logging.getLogger(__name__)

_PIPELINE_TTL = 86400  # 24h -- long enough to outlive an approval window

# Per-session resume lock. A LangGraph thread consumes its interrupt resume value
# positionally from a single shared pending-writes list; two resumes driven
# concurrently on the same thread_id race on that list and raise
# "list.remove(x): x not in list" (langgraph 0.2.74). Resumes are fired
# fire-and-forget from THREE places -- the identify-patient API, the Kafka data
# consumer (registration), and the reaper (approval + registration timeouts) --
# so they can and do overlap. Serialise every resume per session_id so only one
# drive touches a given thread at a time. Process-local (single worker drives a
# given session); a multi-worker deployment would need a Redis lock instead.
_resume_locks: dict[str, asyncio.Lock] = {}


def _resume_lock(session_id: str) -> asyncio.Lock:
    lock = _resume_locks.get(session_id)
    if lock is None:
        lock = _resume_locks[session_id] = asyncio.Lock()
    return lock


# -- Background-execution foundation (Phase 2) --------------------------------
# Flows run fire-and-forget as asyncio.Tasks. The registry maps session_id -> the
# CURRENT driving task so a flow can be listed and (Phase 4) cancelled; a flow's
# lifetime spans several tasks (the initial drive + one per resume), so each new
# drive overwrites the entry and the done-callback only clears its own.
#
# The semaphore bounds how many flows execute at once. Excess flows sit in the
# `queued` index set until a slot frees. Parked flows (suspended at an interrupt)
# release their slot -- they are not "executing" -- so the cap governs active work,
# not flows waiting on a human.

_task_registry: dict[str, asyncio.Task] = {}
_exec_semaphore = asyncio.Semaphore(settings.autonomous_max_concurrency)


def _register_task(session_id: str, task: asyncio.Task) -> None:
    _task_registry[session_id] = task

    def _cleanup(t: asyncio.Task, sid: str = session_id) -> None:
        # Only clear if this is still the mapped task -- a later drive (e.g. a
        # resume, or execution launched after planning) may have replaced it.
        if _task_registry.get(sid) is t:
            _task_registry.pop(sid, None)

    task.add_done_callback(_cleanup)


def get_task(session_id: str) -> asyncio.Task | None:
    """The task currently driving a session, if any (used by Phase 4 cancel)."""
    return _task_registry.get(session_id)


async def _bounded_drive(graph, payload, config, session_id: str,
                         *, _log_session_id: str | None = None) -> None:
    """Drive an execution pass under the concurrency semaphore + queue indexes.

    Marks the flow `queued`, waits for a slot, marks it `running`, drives, then
    always removes it from both index sets (the flow has either completed, failed,
    or parked at an interrupt -- none of which is "actively executing"). Idempotent
    SADD/SREM, so repeated passes (the sibling-interrupt resume loop) are safe."""
    await _enter_org_scope(session_id)   # multi-tenant hasura routing for this drive
    await cache.mark_session_queued(session_id)
    try:
        async with _exec_semaphore:
            await cache.mark_session_running(session_id)
            await _drive(graph, payload, config, session_id, _log_session_id=_log_session_id)
    finally:
        await cache.unmark_session_execution(session_id)
        # Drop any pause signal not honoured by a cooperative park this pass (the flow
        # completed / errored / parked at an interrupt first). _park_on_pause already
        # cleared it on the pause path, so this only reaps a stale flag -- preventing it
        # from spuriously parking the FIRST superstep of a later resume drive.
        await cache.clear_pause_request(session_id)


def _pipeline_key(session_id: str) -> str:
    return f"session:{session_id}:graph_pipeline"


def _org_key(session_id: str) -> str:
    return f"session:{session_id}:org"


async def org_of_session(session_id: str) -> str:
    """The org a session belongs to (multi-tenancy) -- "" = default source (Carer).

    Redis-cached at session start; on a miss (expired key / pre-migration
    session) the session is located across the tenant sources and the mapping is
    re-cached. Never raises -- falls back to "" (default source)."""
    org = await cache.get(_org_key(session_id))
    if org is not None:
        return org or ""
    try:
        if await hasura.get_session(session_id):        # default source (Carer)
            await cache.set(_org_key(session_id), "", ttl=_PIPELINE_TTL)
            return ""
        await hasura.ensure_org_registry()
        for o in hasura.active_orgs():
            if not o.get("root_prefix"):
                continue  # default source, already checked
            if await hasura.get_session(session_id, org_id=o["id"]):
                await cache.set(_org_key(session_id), o["id"], ttl=_PIPELINE_TTL)
                return o["id"]
    except Exception:  # noqa: BLE001
        logger.warning("org lookup failed  session=%s", session_id, exc_info=True)
    return ""


async def _enter_org_scope(session_id: str, org_id: str | None = None) -> str:
    """Bind the session's org into this coroutine's exec context so every hasura
    tenant-table call made anywhere below (park paths, policy engine, agent
    nodes) routes to the right Hasura source. Call at the top of each driving
    coroutine -- contextvars propagate into all its awaits."""
    org = org_id if org_id is not None else await org_of_session(session_id)
    set_exec_ctx(session_id, "", org_id=org or "")
    return org or ""


async def _parked_interrupts(graph, config) -> list[dict]:
    """The full payload dict of every interrupt currently parked on this thread
    (dict-valued interrupts only). Empty when nothing is parked or the snapshot can't
    be read.

    The single source of truth for "what is this thread waiting for" -- used both to
    guard a resume BEFORE it is driven (a stale/duplicate resume whose kind is no
    longer parked becomes a no-op instead of crashing the resume-matcher) and, in
    autonomous mode, to evaluate the policy engine against each parked event."""
    try:
        snap = await graph.aget_state(config)
    except Exception:  # noqa: BLE001
        logger.exception("parked-interrupt state read failed  config=%s", config.get("configurable"))
        return []
    return [
        intr.value
        for task in (snap.tasks or ())
        for intr in (getattr(task, "interrupts", None) or ())
        if isinstance(getattr(intr, "value", None), dict)
    ]


async def _parked_interrupt_kinds(graph, config) -> list[str]:
    """The `kind` of every dict-valued interrupt parked on this thread (see
    _parked_interrupts). Used to kind-guard a resume before it is driven."""
    return [p.get("kind") for p in await _parked_interrupts(graph, config)]


async def _load_pipeline(session_id: str) -> dict:
    pipeline = await cache.get(_pipeline_key(session_id))
    if pipeline:
        return pipeline
    session = await hasura.get_session(session_id)
    if not session:
        raise RuntimeError(f"cannot resume -- session {session_id} not found")
    # pipeline_snapshot holds the executed pipeline pre-completion; fall back to pipeline.
    snap = session.get("pipeline_snapshot") or {}
    if snap.get("agents"):
        return snap
    return session.get("pipeline") or {}


def _is_resume_protocol_error(exc: BaseException) -> bool:
    """LangGraph resume-matcher error -- a resume hit an already-consumed interrupt
    (see graph.nodes._is_resume_protocol_error). Mirrored here to avoid an import
    cycle (nodes imports nothing from runner, but runner already imports heavy
    graph modules; keeping the one-line check local is cheaper than wiring a shared
    util)."""
    return isinstance(exc, ValueError) and "not in list" in str(exc)


async def _park_on_pause(session_id: str) -> None:
    """Cooperative user-pause landing: the drive loop reached a step boundary with a
    pending pause request. Clear the request, move the flow into the paused index set,
    and write a `user_paused` row so it surfaces in the Paused queue. The LangGraph
    checkpoint is intact (we stopped consuming between supersteps), so resume continues
    from here via astream(None)."""
    await cache.clear_pause_request(session_id)
    await cache.mark_session_paused(session_id)   # srem running/queued, sadd paused
    step = await cache.get_current_step(session_id)
    try:
        await hasura.create_approval_task(
            session_id,
            agent_id=(step or {}).get("agent_id") or "",
            action_type="user_paused",
            payload={"reason": "user_paused", "current_step": step},
            kind="user_paused",
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not write user_paused queue row  session=%s", session_id)
    logger.info("flow paused by user at step boundary  session=%s", session_id)


# -- Autonomous policy engine (Phase 5) ---------------------------------------
# In autonomous mode, a flow that parks at a mid-flow approval interrupt is not left for
# a human by default: the policy engine (workflows.graph.policy) decides per parked event
# whether to auto-approve (resume immediately), require a human (leave parked, as assisted),
# or escalate (leave parked + run the ladder). Evaluated at the drive chokepoint AFTER the
# checkpoint commits, so an auto-approve resume_session() can't race the park. Every branch
# is best-effort: a policy/notify/escalation failure must never strand or crash the flow.

async def _notify_policy(session_id: str, verdict, context: dict, severity: str) -> None:
    """Fire a policy notification on the configured channel (Phase 5).

    Reuses the notification agent's delivery surface: a WS broadcast (which routes local
    or via Kafka to every connected client) plus an audit row. `notification_channel`
    gates/labels it -- "none" disables the WS notification; only "websocket" is wired
    today (no SMS/email/Slack infra). Never raises."""
    channel = (settings.notification_channel or "websocket").strip().lower()
    kind = context.get("kind")
    agent_id = context.get("agent_id")
    headline = context.get("headline") or context.get("action_type") or kind or "approval"
    message = f"[{verdict.outcome}] {headline} ({kind})"
    try:
        if channel != "none":
            # Structured policy event for a dedicated UI panel...
            await broadcast(session_id, {
                "type":       "policy_decision",
                "channel":    channel,
                "outcome":    verdict.outcome,
                "kind":       kind,
                "agent_id":   agent_id,
                "action_type": context.get("action_type"),
                "risk":       context.get("risk"),
                "reason":     verdict.reason,
                "message":    message,
            })
            # ...and an `alert`-typed envelope so it flows through the existing alert
            # stream/relay the notification agent already feeds.
            await broadcast(session_id, {
                "type":     "alert",
                "severity": severity,
                "category": "policy",
                "agent_id": agent_id or "policy_engine",
                "message":  message,
                "data":     {"outcome": verdict.outcome, "kind": kind, "risk": context.get("risk")},
            })
    except Exception:  # noqa: BLE001
        logger.warning("policy notification broadcast failed  session=%s", session_id, exc_info=True)
    try:
        await hasura.write_audit(session_id, agent_id="policy_engine",
                                 event_type="policy_notification",
                                 payload={"outcome": verdict.outcome, "kind": kind,
                                          "agent_id": agent_id, "risk": context.get("risk"),
                                          "reason": verdict.reason})
    except Exception:  # noqa: BLE001
        logger.warning("policy notification audit write failed  session=%s", session_id, exc_info=True)


async def _apply_autonomous_policy(graph, config, session_id: str) -> None:
    """Evaluate the autonomy policy against whatever this thread is parked on.

    No-op unless the master gate is on AND this is an autonomous flow AND the thread is
    actually parked at (an) approval interrupt(s). See _notify_policy / policy.evaluate."""
    if not settings.autonomous_policy_enabled:
        return
    try:
        if not await cache.is_session_autonomous(session_id):
            return
        parked = await _parked_interrupts(graph, config)
        if not parked:
            return  # completed, or a cooperative user-pause (raises no interrupt)

        goal = ""
        try:
            snap_vals = (await graph.aget_state(config)).values or {}
            goal = snap_vals.get("goal", "")
        except Exception:  # noqa: BLE001
            pass

        contexts = [{**p, "goal": goal} for p in parked]
        pairs = [(c, policy.evaluate(c)) for c in contexts]
        verdict = policy.dominant([v for _, v in pairs])
        # The context that drove the dominant outcome (so escalate/auto-approve act on the
        # right approval row), falling back to the first parked event.
        primary_ctx = next((c for c, v in pairs if v.outcome == verdict.outcome), contexts[0])
        logger.info("policy verdict  session=%s  outcome=%s  parked=%d  reason=%s",
                    session_id, verdict.outcome, len(parked), verdict.reason)

        if verdict.outcome == policy.AUTO_APPROVE:
            await _policy_auto_approve(session_id, verdict, primary_ctx)
        elif verdict.outcome == policy.ESCALATE:
            await _notify_policy(session_id, verdict, primary_ctx, severity="critical")
            await _policy_escalate(session_id, primary_ctx)
        else:  # require_human -- leave parked exactly as assisted, plus a notification
            if verdict.notify:
                await _notify_policy(session_id, verdict, primary_ctx, severity="warning")
    except Exception:  # noqa: BLE001
        logger.exception("autonomous policy application failed  session=%s", session_id)


async def _policy_auto_approve(session_id: str, verdict, context: dict) -> None:
    """Routine event: stop the escalation ladder, resolve the pending approval row(s),
    notify, then resume the flow with the policy decision. Mirrors the human decide path
    (api/routes/approvals.py) but driven by the engine, not a person."""
    from workflows.temporal.workflow._escalation import signal_escalation_decided

    try:
        pending = await hasura.fetch_pending_approvals(session_id)
    except Exception:  # noqa: BLE001
        pending = []
    for row in pending:
        if row.get("kind", "approval") == "approval":
            await signal_escalation_decided(session_id, row.get("agent_id"), row.get("action_type"))
    try:
        await hasura.resolve_approval_tasks(session_id, kind="approval",
                                            status="approved", decision="auto_approved")
    except Exception:  # noqa: BLE001
        logger.warning("could not resolve auto-approved rows  session=%s", session_id, exc_info=True)

    if verdict.notify:
        await _notify_policy(session_id, verdict, context, severity="warning")
    await resume_session(session_id, verdict.decision)


async def _policy_escalate(session_id: str, context: dict) -> None:
    """High-risk event: ensure the escalation ladder is running for the parked approval
    (idempotent -- ta_create_approval already started it), then leave the flow parked so
    the ladder's timers drive escalation / auto-reject."""
    from workflows.temporal.workflow._escalation import start_escalating_approval

    agent_id = context.get("agent_id")
    try:
        pending = await hasura.fetch_pending_approvals(session_id)
    except Exception:  # noqa: BLE001
        pending = []
    row = next((r for r in pending
                if r.get("kind", "approval") == "approval"
                and (not agent_id or r.get("agent_id") == agent_id)), None)
    if row is None:
        return
    try:
        await start_escalating_approval(
            session_id, row.get("id"), row.get("agent_id") or "",
            row.get("action_type") or "", row.get("payload") or {})
    except Exception:  # noqa: BLE001
        logger.warning("could not (re)start escalation ladder  session=%s", session_id, exc_info=True)


async def _drive(graph, payload, config, session_id: str | None = None,
                 *, _log_session_id: str | None = None) -> None:
    _t0 = asyncio.get_event_loop().time() if _log_session_id else 0.0

    async def _pump() -> None:
        # aclosing() guarantees the astream generator is closed deterministically when
        # we break out early on a pause (rather than leaving it for GC). Each iteration
        # is one superstep boundary (stream_mode="updates"), the cooperative point where
        # a user pause parks the flow with the checkpoint intact.
        async with aclosing(graph.astream(payload, config, stream_mode="updates")) as stream:
            async for _ in stream:
                if session_id and await cache.is_pause_requested(session_id):
                    # Only park if the graph still has pending work. If this was the final
                    # superstep (nothing left in `next`), the flow has effectively finished
                    # -- parking would create a phantom user_paused row on a completed flow.
                    # Let the stream end normally instead and drop the (now moot) signal.
                    snap = await graph.aget_state(config)
                    if getattr(snap, "next", None):
                        await _park_on_pause(session_id)
                        return
                    await cache.clear_pause_request(session_id)

    try:
        await _pump()
        # Autonomous-mode policy engine (Phase 5): the pump has ended -- the flow either
        # completed or parked at an approval interrupt (checkpoint committed). If parked
        # and this is an autonomous flow, evaluate the policy and auto-resolve / notify /
        # escalate. Runs AFTER the checkpoint so an auto-approve resume can't race the
        # park. Assisted flows and cooperative pauses (no interrupt) are a no-op.
        if session_id:
            await _apply_autonomous_policy(graph, config, session_id)
    except Exception as exc:  # noqa: BLE001
        # A resume that raced another resume on the same thread: the matching
        # interrupt was already consumed. Do NOT retry -- retrying re-delivers the
        # same resume and races again. Surface it; the guarded resume loop
        # (_drive_resume_until) re-checks the parked kind and stops cleanly.
        if _is_resume_protocol_error(exc):
            logger.warning("resume raced (already consumed)  config=%s: %s",
                           config.get("configurable"), exc)
            return
        # Stale Postgres connection — pool gave out a connection the server already
        # closed. Retry once; psycopg_pool will open a fresh connection on the next
        # acquire and the second attempt succeeds. NOTE: only safe for a non-resume
        # (initial start) drive, or a resume whose write was not yet consumed; a
        # resume-protocol error is handled above so we never double-deliver here.
        if "consuming input failed" in str(exc) or "server closed the connection" in str(exc):
            logger.warning("checkpointer connection lost, retrying  config=%s", config.get("configurable"))
            try:
                await _pump()
                return
            except Exception as exc2:  # noqa: BLE001
                if _is_resume_protocol_error(exc2):
                    logger.warning("resume raced on retry (already consumed)  config=%s", config.get("configurable"))
                    return
                logger.exception("session graph errored after retry  config=%s", config.get("configurable"))
                return
        logger.exception("session graph errored  config=%s", config.get("configurable"))
    finally:
        flush_langfuse()
        if _log_session_id:
            from flow_log import log_session_end
            log_session_end(_log_session_id, asyncio.get_event_loop().time() - _t0)


async def _drive_resume_until(session_id: str, payload, *, expected_kind: str | None,
                              max_passes: int = 8) -> None:
    """Serialised, kind-guarded resume of a parked session thread.

    Holds the per-session resume lock so no two resumes race on the thread's
    pending-writes list. Before each drive it inspects the parked interrupt(s):

      * expected_kind set (patient_identification / patient_registration):
        drive only while that kind is still parked, then stop. This both delivers
        the resume to every sibling that suspended in the same superstep (each
        re-run consumes from the cache and proceeds) AND makes a stale/duplicate
        trigger -- a late Kafka event, a reaper timeout racing a real resume -- a
        clean no-op instead of a `.remove()` crash.
      * expected_kind None (approval / generic resume): drive once if ANY
        interrupt is parked; skip entirely if the thread is not parked.

    Bounded by max_passes so a stuck state can never loop forever."""
    await _enter_org_scope(session_id)   # multi-tenant hasura routing for this resume
    pipeline = await _load_pipeline(session_id)
    graph = build_session_graph(pipeline, get_checkpointer())
    config = run_config(session_id)

    async with _resume_lock(session_id):
        for _ in range(max_passes):
            kinds = await _parked_interrupt_kinds(graph, config)
            if expected_kind is None:
                if not kinds:
                    logger.info("resume skipped -- nothing parked  session=%s", session_id)
                    return
                await _bounded_drive(graph, payload, config, session_id)
                return
            if expected_kind not in kinds:
                # Either already resumed (first trigger won the race) or never
                # parked on this kind. Nothing to do -- not an error.
                return
            await _bounded_drive(graph, payload, config, session_id)


async def start_session(session_id: str, pipeline: dict, goal: str,
                        autonomous: bool = False, org_id: str = "") -> None:
    """Build + launch the session graph (fire-and-forget). Also fires prefetch.

    `autonomous` stamps a per-session Redis flag the drive loop reads on park to decide
    whether to apply the autonomy policy engine (Phase 5); the execution graph state does
    not carry it (it stops at the planning graph).

    `org_id` (multi-tenancy) is checkpointed in the graph state and cached in
    Redis so every later drive/resume routes hasura calls at the right tenant
    source ("" = default source / Carer)."""
    await cache.set(_pipeline_key(session_id), pipeline, ttl=_PIPELINE_TTL)
    await cache.set(_org_key(session_id), org_id or "", ttl=_PIPELINE_TTL)
    if autonomous:
        await cache.mark_session_autonomous(session_id)
    graph = build_session_graph(pipeline, get_checkpointer())
    config = run_config(session_id, goal)
    init = {"session_id": session_id, "goal": goal, "org_id": org_id or "",
            "results": {}, "_skipped": {}, "_failed": False}

    prefetch = pipeline.get("prefetch", [])
    if prefetch:
        asyncio.create_task(run_prefetch(session_id, prefetch))

    from flow_log import log_session_start
    agent_names = [a.get("id", "") for a in pipeline.get("agents", [])]
    log_session_start(session_id, goal, agent_names)

    task = asyncio.create_task(
        _bounded_drive(graph, init, config, session_id, _log_session_id=session_id))
    _register_task(session_id, task)


async def resume_patient_identification(session_id: str, mobiles: list[str]) -> None:
    """Resume a session parked on the patient-identification interrupt.

    Patient-dependent agents that suspended together in the same superstep (e.g. ER
    and ICU running in parallel) each leave their own interrupt; LangGraph delivers a
    single resume value to one at a time. The guarded driver re-drives while a
    patient_identification interrupt remains: the first pass resolves + caches the
    patient context (graph.patient), and every sibling re-run then hits the cache and
    proceeds without re-interrupting. It stops once that kind is no longer parked
    (leaving any later interrupt -- e.g. patient_registration or an approval -- for
    its own resume path), and is serialised per session so a concurrent trigger
    can't race the pending-writes list."""
    task = asyncio.create_task(_drive_resume_until(
        session_id, Command(resume={"mobiles": mobiles}),
        expected_kind="patient_identification",
    ))
    _register_task(session_id, task)


async def resume_patient_registration(session_id: str, payload: dict) -> None:
    """Resume a session parked on the patient-registration interrupt (graph.patient).

    Triggered by the Kafka data consumer when Fabric reports the newly-created
    patient(s) back, or by the reaper on a registration timeout -- which can fire
    nearly together, so the serialised + kind-guarded driver is what stops the
    second trigger from racing the first. The resume payload is only a wake-up
    signal -- the node re-resolves the patient(s) from Fabric on resume."""
    task = asyncio.create_task(_drive_resume_until(
        session_id, Command(resume=payload),
        expected_kind="patient_registration",
    ))
    _register_task(session_id, task)


async def resume_session(session_id: str, decision: str | dict) -> None:
    """Resume a parked session with the resume payload.

    `decision` is forwarded verbatim to Command(resume=...) -- a string for approval
    interrupts ("approved"/"rejected"/"timeout"). Driven through the serialised
    resume path (expected_kind=None: drive once if anything is parked) so an
    approval decision and a reaper timeout -- which can fire for the same approval
    at nearly the same moment -- can't both drive the thread at once, and a resume
    that arrives after the thread already advanced is a clean no-op rather than a
    pending-writes `.remove()` crash."""
    task = asyncio.create_task(_drive_resume_until(
        session_id, Command(resume=decision), expected_kind=None,
    ))
    _register_task(session_id, task)


async def resume_paused_session(session_id: str) -> None:
    """Resume a flow the user cooperatively paused (Phase 4).

    A user pause did NOT raise interrupt() -- the drive loop simply stopped consuming
    between supersteps, leaving the checkpoint's pending `next` tasks intact. So this is
    NOT the Command(resume=...) path used for approvals/patient interrupts: it re-drives
    with a None payload, which makes LangGraph continue those pending tasks from the
    checkpoint. Serialised on the same per-session resume lock so it can't race any other
    drive on the thread."""
    await _enter_org_scope(session_id)   # multi-tenant hasura routing
    await cache.clear_pause_request(session_id)     # defensive: don't re-park instantly
    await cache.unmark_session_paused(session_id)   # srem paused (bounded-drive re-adds running)
    try:
        await hasura.resolve_approval_tasks(session_id, kind="user_paused",
                                            status="resolved", decision="resumed")
    except Exception:  # noqa: BLE001
        logger.exception("could not resolve user_paused rows on resume  session=%s", session_id)

    pipeline = await _load_pipeline(session_id)
    graph = build_session_graph(pipeline, get_checkpointer())
    config = run_config(session_id)

    async def _run() -> None:
        async with _resume_lock(session_id):
            await _bounded_drive(graph, None, config, session_id)

    task = asyncio.create_task(_run())
    _register_task(session_id, task)


def _checkpoint_config(session_id: str, checkpoint_id: str | None = None) -> dict:
    cfg = run_config(session_id)
    if checkpoint_id:
        cfg["configurable"]["checkpoint_id"] = checkpoint_id
    return cfg


async def list_checkpoints(session_id: str) -> list[dict]:
    """The revert points of a flow -- one per LangGraph superstep already executed.

    Reads the execution thread's checkpoint history (graph built from the CURRENT pipeline
    so the topology matches the stored checkpoints). Each entry names the checkpoint_id to
    pass to edit_resume_session, the agents completed as of that point, and the nodes that
    were pending next. Newest first (LangGraph history order). Never raises."""
    pipeline = await _load_pipeline(session_id)
    graph = build_session_graph(pipeline, get_checkpointer())
    config = run_config(session_id)
    out: list[dict] = []
    try:
        async for snap in graph.aget_state_history(config):
            vals = snap.values or {}
            cid = (snap.config or {}).get("configurable", {}).get("checkpoint_id")
            out.append({
                "checkpoint_id":     cid,
                "step":              (snap.metadata or {}).get("step"),
                "completed_agents":  sorted((vals.get("results") or {}).keys()),
                "skipped":           list((vals.get("_skipped") or {}).keys()),
                "next":              list(snap.next or ()),
                "created_at":        snap.created_at,
            })
    except Exception:  # noqa: BLE001
        logger.exception("checkpoint history read failed  session=%s", session_id)
    return out


async def edit_resume_session(session_id: str, edited_pipeline: dict,
                              checkpoint_id: str | None = None) -> None:
    """Revert a paused flow to a chosen checkpoint, swap in an edited pipeline, and re-run
    from that point -- preserving the agents completed up to the checkpoint.

    Mechanism (topology-safe): the LangGraph checkpoint is bound to the OLD graph's node
    names, so we can't resume it on the edited topology. Instead we (1) read the chosen
    checkpoint's completed `results`/`_skipped` from the old thread, (2) RESET the thread
    (adelete_thread) so the edited topology starts clean on the same thread_id (the whole
    system keys resumes on thread_id == session_id), (3) start a FRESH seeded run on the
    edited graph carrying those completed results forward. The nodes.py completion guard
    then skips the carried-forward agents (they don't re-execute), so only the steps after
    the checkpoint -- plus any edited/added agents -- run.

    NOTE: resetting the thread starts a new checkpoint lineage; checkpoints from before this
    edit are no longer revertable (documented v1 tradeoff)."""
    await _enter_org_scope(session_id)   # multi-tenant hasura routing
    await cache.clear_pause_request(session_id)
    await cache.unmark_session_paused(session_id)
    try:
        await hasura.resolve_approval_tasks(session_id, kind="user_paused",
                                            status="resolved", decision="edited")
    except Exception:  # noqa: BLE001
        logger.exception("could not resolve user_paused rows on edit-resume  session=%s", session_id)

    # 1. Extract the chosen checkpoint's completed state from the OLD thread/topology.
    old_pipeline = await _load_pipeline(session_id)
    old_graph = build_session_graph(old_pipeline, get_checkpointer())
    prior: dict = {}
    try:
        snap = await old_graph.aget_state(_checkpoint_config(session_id, checkpoint_id))
        prior = snap.values or {}
    except Exception:  # noqa: BLE001
        logger.exception("could not read checkpoint state  session=%s  cid=%s", session_id, checkpoint_id)
    carry_results = prior.get("results", {}) or {}
    carry_skipped = prior.get("_skipped", {}) or {}
    goal = prior.get("goal", "") or ""
    org_id = prior.get("org_id", "") or ""

    # 2. Reset the execution thread so the edited topology isn't fighting the old
    #    checkpoint's node names / pending writes -- and so the seeded init below is the
    #    authoritative state (otherwise LangGraph inherits the old checkpoint's results and
    #    reuses agents we meant to re-run).
    try:
        await reset_checkpoint_thread(session_id)
    except Exception:  # noqa: BLE001
        logger.exception("could not reset checkpoint thread  session=%s", session_id)

    # 3. Bind the edited plan (sweep + materialize preplans, re-cache, snapshot) -- mirrors
    #    _launch_execution_from_plan, but drives a SEEDED init instead of a fresh one.
    await cache.delete_pattern(f"session:{session_id}:subagent_preplan:*")
    for node_id, preplan in materialize_preplans(edited_pipeline).items():
        await cache.set(f"session:{session_id}:subagent_preplan:{node_id}", preplan, ttl=_PIPELINE_TTL)
    await cache.delete(f"session:{session_id}:awaiting_reorchestration")
    await cache.set(_pipeline_key(session_id), edited_pipeline, ttl=_PIPELINE_TTL)
    await hasura.update_session_status(session_id, "running", pipeline_snapshot=edited_pipeline,
                                       org_id=org_id or None)
    await broadcast(session_id, {
        "type":          "session_started",
        "total_agents":  len(edited_pipeline.get("agents", [])),
        "reverted_to":   checkpoint_id,
    })

    # 4. Fresh seeded run on the edited graph -- SAME thread_id, carried-forward results.
    graph = build_session_graph(edited_pipeline, get_checkpointer())
    config = run_config(session_id, goal)
    init = {"session_id": session_id, "goal": goal, "org_id": org_id,
            "results": carry_results, "_skipped": carry_skipped, "_failed": False}

    async def _run() -> None:
        async with _resume_lock(session_id):
            await _bounded_drive(graph, init, config, session_id, _log_session_id=session_id)

    task = asyncio.create_task(_run())
    _register_task(session_id, task)


async def cancel_session(session_id: str) -> None:
    """Stop a flow outright (Phase 4). Cancels the driving task via the Phase 2 registry,
    clears every index-set membership + pause signal + current step, resolves all pending
    approval-task rows (so it leaves the Paused queue), and marks the session cancelled.
    Idempotent -- safe to call on a flow that has already finished or was never running."""
    await _enter_org_scope(session_id)   # multi-tenant hasura routing
    await cache.clear_pause_request(session_id)
    task = get_task(session_id)
    if task is not None and not task.done():
        task.cancel()
    await cache.unmark_session_execution(session_id)   # srem running/queued
    await cache.unmark_session_paused(session_id)       # srem paused
    await cache.delete(f"session:{session_id}:current_step")
    try:
        await hasura.resolve_approval_tasks(session_id, status="cancelled", decision="cancelled")
    except Exception:  # noqa: BLE001
        logger.exception("could not resolve pending rows on cancel  session=%s", session_id)
    try:
        await hasura.update_session_status(session_id, "cancelled")
    except Exception:  # noqa: BLE001
        logger.exception("could not mark session cancelled  session=%s", session_id)
    logger.info("flow cancelled by user  session=%s", session_id)


# -- Planning graph (3-stage planner + approval pause) ------------------------
# The planning graph runs on its own thread ({sid}:plan) so its approval interrupt
# never collides with the execution graph's interrupts. On approval the driver
# materializes the task-level preplan and launches the execution graph.

def _plan_config(session_id: str, goal: str = "") -> dict:
    cfg = run_config(session_id, goal)
    cfg["configurable"]["thread_id"] = f"{session_id}:plan"
    cfg["recursion_limit"] = 50  # allow several reorchestrate rounds
    return cfg


async def _launch_execution_from_plan(session_id: str, pipeline: dict, goal: str,
                                      autonomous: bool = False, org_id: str = "") -> None:
    """Bind the approved plan to execution and start the execution graph."""
    # Sweep any stale preplan keys (re-run safety) then write the approved plan.
    await cache.delete_pattern(f"session:{session_id}:subagent_preplan:*")
    for node_id, preplan in materialize_preplans(pipeline).items():
        await cache.set(f"session:{session_id}:subagent_preplan:{node_id}", preplan, ttl=_PIPELINE_TTL)
    # Clear the failure-reorchestration flag now that a (re-)run is launching.
    await cache.delete(f"session:{session_id}:awaiting_reorchestration")

    await hasura.update_session_status(session_id, "running", pipeline_snapshot=pipeline,
                                       org_id=org_id or None)
    await broadcast(session_id, {"type": "session_started", "total_agents": len(pipeline.get("agents", []))})
    await start_session(session_id, pipeline, goal or pipeline.get("understood_goal", ""),
                        autonomous=autonomous, org_id=org_id)


async def _drive_planning(graph, payload, config, session_id: str) -> None:
    await _enter_org_scope(session_id)   # multi-tenant hasura routing
    try:
        async for _ in graph.astream(payload, config, stream_mode="updates"):
            pass
    except Exception as exc:  # noqa: BLE001
        if "consuming input failed" in str(exc) or "server closed the connection" in str(exc):
            logger.warning("planning checkpointer connection lost, retrying  session=%s", session_id)
            try:
                async for _ in graph.astream(payload, config, stream_mode="updates"):
                    pass
            except Exception:
                logger.exception("planning graph errored after retry  session=%s", session_id)
                flush_langfuse()
                return
        else:
            logger.exception("planning graph errored  session=%s", session_id)
            flush_langfuse()
            return
    flush_langfuse()

    # Parked at the approval interrupt? -> snapshot.next is non-empty; wait for resume.
    snapshot = await graph.aget_state(config)
    if snapshot.next:
        return
    values = snapshot.values or {}
    if values.get("approved") and values.get("pipeline"):
        await _launch_execution_from_plan(session_id, values["pipeline"], values.get("goal", ""),
                                          autonomous=bool(values.get("autonomous")),
                                          org_id=values.get("org_id", ""))


async def start_planning(session_id: str, goal: str, constraints: str = "",
                         feedback: str = "", attempt: int = 0,
                         autonomous: bool = False, org_id: str = "",
                         feedback_history: list[str] | None = None,
                         prior_plan: dict | None = None) -> None:
    """Launch the 3-stage planning graph (fire-and-forget).

    Assisted (default): parks at the plan-approval interrupt for a human decision.
    Autonomous: the terminal node auto-approves (no interrupt) so the driver
    launches execution immediately -- see planning_graph._await_plan_approval.

    `org_id` (multi-tenancy) is checkpointed in the planning state and carried
    into the execution graph on approval ("" = default source / Carer).

    `feedback_history` / `prior_plan` re-enter planning on a session the user has
    already revised (failure replan): planning is stateless apart from the goal
    string, so without them a re-plan silently reverts every earlier revision.
    `feedback` stays for transient one-off notes (the failure context itself)."""
    await cache.set(_org_key(session_id), org_id or "", ttl=_PIPELINE_TTL)
    graph = build_planning_graph(get_checkpointer())
    config = _plan_config(session_id, goal)
    init = {
        "session_id": session_id, "goal": goal, "constraints": constraints,
        "feedback": feedback, "attempt": attempt, "autonomous": autonomous,
        "org_id": org_id or "",
        "feedback_history": list(feedback_history or []),
        "prior_plan": prior_plan or {},
    }
    task = asyncio.create_task(_drive_planning(graph, init, config, session_id))
    _register_task(session_id, task)


async def resume_planning(session_id: str, decision: dict) -> None:
    """Resume the parked planning graph with the user's plan decision payload
    ({action: approve|edit|reorchestrate, pipeline?, feedback?})."""
    graph = build_planning_graph(get_checkpointer())
    config = _plan_config(session_id)
    task = asyncio.create_task(_drive_planning(graph, Command(resume=decision), config, session_id))
    _register_task(session_id, task)
