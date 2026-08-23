"""Agent node factory.

Wraps each ported agent body in an AgentUnit that reproduces the old
api.agents.run_agent contract: cascade-skip guard, agent_started /
agent_completed broadcasts, context construction (incl. _task_plan preplan
seeding from Redis), failure handling that halts the pipeline, and writing the
result into state["results"] under the base agent id.

Each agent is wrapped in an AgentUnit whose ``commit`` is the node body; the
builder adds it directly as a LangGraph node. The flow is whatever the planner
(LLM) emits -- there is no coordination/arbitration layer.
"""

import asyncio
import logging

from langgraph.errors import GraphInterrupt

from api.routes.ws import broadcast
from cache import redis as cache
from config import settings
from db.hasura import hasura
from workflows.graph import hitl
from workflows.graph.conditions import should_agent_run, _base_id
from workflows.graph.errors import TaskExecutionError
from workflows.graph.exec_context import set_exec_ctx
from workflows.graph.state import build_ctx, SessionFatalError
from workflows.graph.trace import record_step

logger = logging.getLogger(__name__)


def _is_resume_protocol_error(exc: BaseException) -> bool:
    """True for LangGraph's interrupt/resume-matching errors (NOT agent failures).

    A node that calls interrupt() consumes its resume value positionally from a
    shared pending-writes list (langgraph.pregel.algo._scratchpad). When a stale
    or duplicate Command(resume=...) is delivered to a thread whose matching
    interrupt was already consumed, langgraph 0.2.74 raises a bare
    ``ValueError("list.remove(x): x not in list")`` (later versions swallow it).
    The patient-verification node is the one body that raises two interrupts in a
    row, so it is the usual victim. Such an error means "resume delivery raced",
    not "the agent failed" -- it must propagate to the driver, never be folded
    into a `{"status": "failed"}` agent result. The companion fix is to serialize
    + kind-guard resume delivery so this stops happening (graph.runner)."""
    return isinstance(exc, ValueError) and "not in list" in str(exc)


async def _dispatch_body(base_id: str, session_id: str, ctx: dict) -> dict:
    # Lazy import to avoid import cycles at module load.
    from workflows.graph.agents.registry import AGENT_BODIES

    fn = AGENT_BODIES.get(base_id)
    if fn is None:
        logger.warning("no body registered for agent=%s -- completed stub", base_id)
        return {"status": "completed", "agent_id": base_id}
    return await fn(session_id, ctx)


async def _bid_for(base_id: str, session_id: str, ctx: dict) -> dict:
    """Compute a no-side-effect bid proposal for an agent.

    Calls the agent body's optional ``propose`` hook (AGENT_BID_HOOKS) if it has
    one; otherwise returns a neutral score so agents that don't bid still play.
    The bid VALUE is per-agent (a bed agent scores on patient acuity, a revenue
    agent on $ impact) but the interface is uniform -- this is what lets a single
    bidding handler work across any flow.
    """
    from workflows.graph.agents.registry import AGENT_BID_HOOKS

    hook = AGENT_BID_HOOKS.get(base_id)
    if hook is None:
        return {"score": 0.0, "reason": "no_bid_hook"}
    try:
        return await hook(session_id, ctx)
    except Exception as exc:  # noqa: BLE001 -- a bad bid must not crash arbitration
        logger.warning("bid hook failed  agent=%s  err=%s", base_id, exc)
        return {"score": 0.0, "reason": f"bid_error: {exc}"}


def make_agent_node(cfg: dict) -> "AgentUnit":
    return AgentUnit(cfg)


class AgentUnit:
    """One agent in an execution level.

    ``commit`` is the node body; ``__call__`` delegates to it so an AgentUnit can
    be added directly as a LangGraph node without a wrapper.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.node_id = cfg["id"]
        self.base = _base_id(self.node_id)
        self.label = cfg.get("label", self.node_id)
        self.order = cfg.get("order", 0)

    async def __call__(self, state: dict) -> dict:
        return await self.commit(state)

    async def propose(self, state: dict) -> dict:
        """Return ``{"score": float, ...}`` for arbitration. No side effects."""
        if state.get("_failed"):
            return {"score": 0.0, "reason": "session_failed"}
        session_id = state["session_id"]
        run, reason = await should_agent_run(self.cfg, state)
        if not run:
            return {"score": 0.0, "reason": reason or "skipped"}
        ctx = build_ctx(state, self.base, self.cfg)
        proposal = await _bid_for(self.base, session_id, ctx)
        # Record the bid in state so the arbiter / observers can see it.
        return {**proposal, "_bids": {self.node_id: proposal}}

    async def skip(self, state: dict, reason: str) -> dict:
        """Skip this unit (e.g. it lost a bid). Reuses the branch_skipped contract."""
        session_id = state["session_id"]
        logger.info("unit skipped  agent=%s  reason=%s  session=%s", self.node_id, reason, session_id)
        await broadcast(session_id, {
            "type":      "branch_skipped",
            "agent_id":  self.node_id,
            "condition": reason,
        })
        return {"_skipped": {self.node_id: reason}}

    async def commit(self, state: dict) -> dict:
        node_id = self.node_id
        base = self.base
        label = self.label
        order = self.order

        if state.get("_failed"):
            return {}

        session_id = state["session_id"]
        # Begin this node's execution scope so a task failure can be attributed to
        # the exact task (set inside should_run_task) -- see graph.exec_context.
        # org_id (multi-tenancy) routes this node's hasura tenant-table calls at
        # the session's Hasura source.
        set_exec_ctx(session_id, node_id, org_id=state.get("org_id", ""))

        # Resume detection: if this agent left a pending-approval record, the node
        # is re-running to deliver the approval decision (interrupt re-execution).
        # Skip the guard + agent_started broadcast so the frontend sees them once.
        resuming = await hitl.load_pending(session_id, base) is not None

        # Edit-resume seeding (checkpoint rewind): this agent already completed
        # successfully in the run we reverted from -- its result was carried into this
        # run's seeded `results`. Don't re-execute: reflect it as done (so downstream
        # conditions + the UI see it) and keep the seeded result (merge_dict preserves
        # it). Failed prior results are NOT skipped -- an edit may be fixing them. Inert
        # in normal runs / plain resume, where completed agents are never re-invoked with
        # their result already in state. HITL-resume path is guarded by `not resuming`.
        if not resuming:
            prior = state.get("results", {}).get(base)
            if isinstance(prior, dict) and prior.get("status") != "failed":
                logger.info("edit-resume: reusing completed agent  agent=%s  session=%s",
                            node_id, session_id)
                await broadcast(session_id, {
                    "type":     "agent_completed",
                    "agent_id": node_id,
                    "label":    label,
                    "order":    order,
                    "result":   prior,
                    "reused":   True,
                })
                await record_step(session_id, kind="agent", title=label,
                                  status="completed", agent_id=node_id, raw_output=prior,
                                  seq=await cache.get_agent_trace_seq(session_id, node_id))
                return {}

        if not resuming:
            run, reason = await should_agent_run(self.cfg, state)
            if not run:
                logger.info("branch skipped  agent=%s  reason=%s  session=%s", node_id, reason, session_id)
                await broadcast(session_id, {
                    "type":      "branch_skipped",
                    "agent_id":  node_id,
                    "condition": reason,
                })
                from flow_log import log_agent_skip
                log_agent_skip(session_id, node_id, reason)
                return {"_skipped": {node_id: reason or "skipped"}}

            await broadcast(session_id, {
                "type":     "agent_started",
                "agent_id": node_id,
                "label":    label,
                "order":    order,
            })
            # Reuse this agent's existing step seq if it already has one. An agent
            # can re-enter commit() several times without an hitl pending record
            # (e.g. patient identification interrupts, so `resuming` is False each
            # round); reusing the seq upserts the SAME running card instead of
            # stacking a new one per re-entry. The terminal record reuses it too.
            running_seq = await record_step(
                session_id, kind="agent", title=label, status="running",
                agent_id=node_id,
                seq=await cache.get_agent_trace_seq(session_id, node_id),
            )
            if running_seq is not None:
                await cache.set_agent_trace_seq(session_id, node_id, running_seq)

        ctx = build_ctx(state, base, self.cfg)
        # Seed _task_plan from the approved plan. Prefer the per-INSTANCE key
        # (e.g. "bed_agent:after_icu") so multi-instance agents bind their own
        # task set; fall back to the BASE key (written by /reorchestrate).
        if ctx.get("_goal"):
            preplan = (
                await cache.get(f"session:{session_id}:subagent_preplan:{node_id}")
                or await cache.get(f"session:{session_id}:subagent_preplan:{base}")
            )
            ctx["_task_plan"] = preplan or {}

        if not resuming:
            from flow_log import log_agent_start
            log_agent_start(session_id, node_id, label, {})

        try:
            result = await _dispatch_body(base, session_id, ctx)
        except GraphInterrupt:
            # interrupt() raised -- propagate so LangGraph suspends + checkpoints.
            # agent_completed is intentionally NOT broadcast until the agent resumes.
            raise
        except ValueError as exc:
            # LangGraph resume-matching error (a stale/duplicate resume hit an
            # already-consumed interrupt). NOT an agent failure -- propagate so the
            # driver sees it, and never fabricate a "failed" agent result that
            # misreports a resume race as a permanent failure. See
            # _is_resume_protocol_error and the serialized resume path in runner.py.
            # Any OTHER ValueError is a genuine agent bug -- fall through to the
            # existing soft-fail handler below (re-raise so it reaches it).
            if _is_resume_protocol_error(exc):
                logger.warning(
                    "resume-protocol error (stale/duplicate resume) agent=%s session=%s: %s",
                    node_id, session_id, exc,
                )
                raise
            logger.exception("agent failed (soft)  agent=%s  session=%s", node_id, session_id)
            result = {"status": "failed", "error": str(exc)}
        except SessionFatalError as exc:
            # Unrecoverable, session-wide -- halt the whole pipeline (rare).
            logger.exception("session-fatal failure  agent=%s  session=%s", node_id, session_id)
            await hasura.update_session_status(session_id, "failed")
            await broadcast(session_id, {"type": "session_failed", "error": str(exc)})
            await record_step(session_id, kind="agent", title=label, status="failed",
                              agent_id=node_id, error=str(exc),
                              seq=await cache.get_agent_trace_seq(session_id, node_id))
            from flow_log import log_agent_fail
            log_agent_fail(session_id, node_id, str(exc))
            return {"_failed": True, "results": {base: {"status": "failed", "error": str(exc)}}}
        except TaskExecutionError as exc:
            # A single task failed after retries. Halt the session (set _failed) and
            # carry the failed-task details so synthesis can branch into
            # failure-reorchestration (recommend a revised plan for the user).
            err = str(exc.cause)
            logger.warning("task failed  agent=%s  task=%s  activity=%s  session=%s",
                           node_id, exc.task_id, exc.activity, session_id)
            failure = {
                "agent_id": node_id, "base": base,
                "task_id": exc.task_id, "activity": exc.activity, "error": err,
            }
            await broadcast(session_id, {
                "type":     "task_failed",
                "agent_id": node_id,
                "label":    label,
                "order":    order,
                "task_id":  exc.task_id,
                "activity": exc.activity,
                "error":    err,
            })
            await record_step(session_id, kind="agent", title=label, status="failed",
                              agent_id=node_id, task_id=exc.task_id, error=err,
                              seq=await cache.get_agent_trace_seq(session_id, node_id))
            from flow_log import log_agent_fail
            log_agent_fail(session_id, node_id, f"task={exc.task_id}  activity={exc.activity}  {err}")
            return {
                "_failed": True,
                "_task_failed": failure,
                "results": {base: {"status": "failed", "error": err, "failed_task": exc.task_id}},
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent failed (soft)  agent=%s  session=%s", node_id, session_id)
            result = {"status": "failed", "error": str(exc)}

        # Preserve the existing event contract: agent_completed fires for every
        # agent (including soft-failed ones, with a failed result). A single agent
        # failing no longer halts the session -- downstream conditional dependents
        # naturally skip (their condition reads the failed/absent result), while
        # independent agents and the final synthesis still run on partial results.
        from flow_log import log_agent_done
        log_agent_done(session_id, node_id, result)
        await broadcast(session_id, {
            "type":     "agent_completed",
            "agent_id": node_id,
            "label":    label,
            "order":    order,
            "result":   result,
        })
        await record_step(
            session_id, kind="agent", title=label, agent_id=node_id,
            status="failed" if (isinstance(result, dict) and result.get("status") == "failed") else "completed",
            raw_output=result,
            seq=await cache.get_agent_trace_seq(session_id, node_id),
        )

        # Pace the handoff to whatever runs next -- without this, independent/fast
        # agents complete back-to-back quickly enough that the pipeline canvas
        # flashes through agent_started/agent_completed too fast to follow. Applies
        # in both assisted and autonomous mode; skipped/reused/halting-failure exits
        # above never reach here, so this only delays an agent that actually ran.
        if settings.agent_step_delay_seconds > 0:
            await asyncio.sleep(settings.agent_step_delay_seconds)

        return {"results": {base: result}}
