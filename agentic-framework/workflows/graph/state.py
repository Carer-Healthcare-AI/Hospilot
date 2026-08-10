"""Shared graph state for a Hospilot session.

`SessionState` replaces the `context` dict that the old Kafka/A2A dispatcher
threaded agent->agent. The crucial difference: `results` and `_skipped` carry a
merge reducer so that agents running concurrently in the same LangGraph
superstep do not clobber each other's writes (a bare dict write from two
parallel nodes raises InvalidUpdateError).

`ta_results` (the large per-task result dict that holds candidate/bed lists)
is deliberately NOT a state channel -- it stays node-local so checkpoints stay
small. Only the compact agent `result` summary enters `results`.
"""

from typing import Annotated, Any, TypedDict


class SessionFatalError(RuntimeError):
    """Raise from an agent body ONLY for an unrecoverable, session-wide failure
    that should halt the whole pipeline (sets _failed). Ordinary task/agent
    failures should NOT raise this -- they soft-fail and the session proceeds on
    partial results (see graph.nodes)."""


def merge_dict(left: dict, right: dict) -> dict:
    """Reducer: shallow-merge concurrent dict writes (last-writer-wins per key).

    Each agent node writes under its own base-agent-id key, so concurrent
    agents never collide on the same key.
    """
    if not left:
        return dict(right or {})
    if not right:
        return dict(left)
    return {**left, **right}


class SessionState(TypedDict, total=False):
    session_id: str
    goal: str                                       # was context["_goal"]
    # Multi-tenancy: the org this session belongs to ("" = default source / Carer).
    # Checkpointed, so every resume path inherits it for free; routes hasura
    # tenant-table queries and is exposed to agents as ctx["_org_id"].
    org_id: str
    # {base_agent_id: agent_result_dict} -- replaces context[agent_id]
    results: Annotated[dict[str, Any], merge_dict]
    # {agent_node_id: skip_reason} -- cascading-skip flags, replaces context["_skipped"]
    # reason="predecessor_skipped" means a true cascade; other values are condition labels
    _skipped: Annotated[dict[str, str], merge_dict]
    # set True when any agent fails -- halts the pipeline (was run_agent's early return)
    _failed: Annotated[bool, lambda a, b: bool(a) or bool(b)]
    # set when a single agent TASK fails after retries -- carries the failed task's
    # identity so synthesis can branch into failure-reorchestration (see graph.errors
    # TaskExecutionError, graph.nodes). Merge keeps the last writer if two nodes in one
    # superstep both fail (synthesis recommends off the merged one).
    _task_failed: Annotated[dict, merge_dict]
    # final synthesis (single writer -- the synthesise node)
    recommendation: dict


def build_ctx(state: SessionState, base_id: str, agent_cfg: dict) -> dict:
    """Reconstruct the per-agent `context` dict the ported workflow bodies expect.

    Mirrors api.agents._run_workflow's enriched_context: prior agent results by
    base id, plus the _goal / _agent_id / _task_type / _bed_limit seeds.
    """
    ctx: dict = dict(state.get("results", {}))
    ctx["_goal"] = state.get("goal", "")
    ctx["_org_id"] = state.get("org_id", "")
    ctx["_agent_id"] = base_id
    # Full node id incl. any multi-instance suffix (e.g. "bed_agent:after_discharge").
    # _agent_id is the BASE id; bodies that behave differently per instance read this.
    ctx["_agent_instance"] = agent_cfg.get("id", base_id)
    task_type = agent_cfg.get("task_type", "")
    if task_type:
        ctx["_task_type"] = task_type
    if agent_cfg.get("bed_limit") is not None:
        ctx["_bed_limit"] = agent_cfg["bed_limit"]
    return ctx
