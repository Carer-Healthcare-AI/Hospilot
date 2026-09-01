"""Drive one pipeline end to end and report what the graph actually did.

`run_flow()` builds the session graph the same way the runner does — the real
`build_session_graph` over the real checkpointer — and drives it to a terminal
state, returning a `FlowRun` the tests assert against.

Why not call `runner.start_session()`? It is fire-and-forget: it spawns a task
and returns, so a test would have to reach into the private task registry and
then guess at completion. Driving `astream` here gives an explicit terminal
state, a real timeout, and the per-superstep order — which is the thing most of
these tests are actually about. Everything below the graph (nodes, agents, task
activities, Hasura, Fabric, the LLM) is untouched and fully live.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from workflows.graph.builder import build_session_graph
from workflows.graph.observability import get_checkpointer, run_config


@dataclass
class FlowRun:
    """What one end-to-end pipeline run produced."""

    name: str
    session_id: str
    final_state: dict
    supersteps: list[list[str]] = field(default_factory=list)
    duration_s: float = 0.0

    # ── what ran ─────────────────────────────────────────────────────────────
    @property
    def results(self) -> dict:
        return self.final_state.get("results", {}) or {}

    @property
    def skipped(self) -> dict:
        return self.final_state.get("_skipped", {}) or {}

    @property
    def failed(self) -> bool:
        return bool(self.final_state.get("_failed"))

    @property
    def task_failed(self) -> dict:
        return self.final_state.get("_task_failed", {}) or {}

    @property
    def ran_agents(self) -> set[str]:
        """Agents that produced a result (i.e. actually executed)."""
        return set(self.results)

    @property
    def touched_agents(self) -> set[str]:
        """Agents the graph reached at all — ran, or was deliberately skipped."""
        return set(self.results) | set(self.skipped)

    def order_of(self, agent_id: str) -> int:
        """Index of the superstep an agent ran in (-1 if it never ran).

        Node keys are the sanitised agent ids (':' -> '--'), so compare on the
        base id to stay correct for instanced agents like `bed_agent:2`.
        """
        for i, step in enumerate(self.supersteps):
            for node in step:
                if node == agent_id or node.split("--")[0] == agent_id:
                    return i
        return -1

    def summary(self) -> str:
        return (f"[{self.name}] {len(self.ran_agents)} ran, {len(self.skipped)} skipped, "
                f"{len(self.supersteps)} supersteps, {self.duration_s:.1f}s")


async def run_flow(flow: dict, session_id: str, timeout_s: float) -> FlowRun:
    """Build and drive `flow`'s pipeline to a terminal state.

    Raises `asyncio.TimeoutError` if the flow does not finish in `timeout_s`,
    and `FlowInterrupted` if it parks on a HITL interrupt — a straight-through
    flow that parks is a real finding, not something to silently wait out.
    """
    from cache import redis as cache
    from workflows.graph import runner

    pipeline = flow["pipeline"]
    goal = flow["goal"]

    # The drive loop re-reads the pipeline from Redis on every step, so it must be
    # cached before the first one — this is what start_session() does for us in
    # production.
    await cache.set(runner._pipeline_key(session_id), pipeline, ttl=3600)
    await cache.set(runner._org_key(session_id), "", ttl=3600)

    graph = build_session_graph(pipeline, get_checkpointer())
    config = run_config(session_id, goal)
    init = {
        "session_id": session_id, "goal": goal, "org_id": "",
        "results": {}, "_skipped": {}, "_failed": False,
    }

    supersteps: list[list[str]] = []
    started = time.monotonic()

    async def _drive() -> None:
        async for chunk in graph.astream(init, config):
            # astream yields {node_name: node_output} per superstep; "__interrupt__"
            # is LangGraph's signal that the graph parked for a human.
            if "__interrupt__" in chunk:
                raise FlowInterrupted(
                    f"[{flow['name']}] parked on a HITL interrupt after "
                    f"supersteps={supersteps}"
                )
            supersteps.append(sorted(chunk))

    await asyncio.wait_for(_drive(), timeout=timeout_s)

    snapshot = await graph.aget_state(config)
    return FlowRun(
        name=flow["name"],
        session_id=session_id,
        final_state=dict(snapshot.values or {}),
        supersteps=supersteps,
        duration_s=time.monotonic() - started,
    )


class FlowInterrupted(AssertionError):
    """The pipeline parked for a human when the test expected it to run through."""
