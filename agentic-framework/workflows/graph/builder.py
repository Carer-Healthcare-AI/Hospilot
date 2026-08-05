"""Build a LangGraph StateGraph dynamically from an LLM-generated pipeline.

The pipeline (agents + edges) is produced per session by services.planner.
generate_pipeline. We replicate api.sessions._build_execution_plan's topological
levelling, then wire the graph so that consecutive levels are fully connected
(every node in level k -> every node in level k+1). Because a whole level
completes in one LangGraph superstep, each next-level node sees all its lifted
parents finish simultaneously and therefore triggers exactly once -- this
reproduces the old "group g(k+1) starts only after all of g(k) finishes"
barrier without any Redis counter, and avoids LangGraph's uneven-fan-in
double-trigger pitfall.

Conditional edges and cascading skips are NOT modelled as LangGraph edges; they
are evaluated inside each node's guard (graph.conditions.should_agent_run),
exactly as the old _advance_plan evaluated them at the target's turn.

IMPORTANT: rebuild must be deterministic from the stored pipeline_snapshot
(never a fresh generate_pipeline) so that node names match the checkpoint when
resuming an interrupted (approval-pending) session.
"""

import logging

from langgraph.graph import StateGraph, START, END

from workflows.graph.nodes import make_agent_node
from workflows.graph.state import SessionState
from workflows.graph.synthesis import synthesise_node
from workflows.strategies import get_handler, default_strategy_id

logger = logging.getLogger(__name__)

_SYNTH = "__synthesise__"


def _make_level_node(units: list, handler):
    """Wrap one execution level so the chosen strategy handler drives its units.

    Returns a single LangGraph node that hands the level's AgentUnits to the
    strategy (services.strategies). For ``common_goal`` this awaits-all-and-merges
    -- behaviourally identical to wiring the units as individual nodes -- but it
    lets ``bidding`` (or any future strategy) arbitrate the level as a unit.
    """
    async def level_node(state: dict) -> dict:
        return await handler(units, state)
    return level_node


def _safe(aid: str) -> str:
    """LangGraph forbids ':' in node names -- map instance ids to a safe graph key.

    Only the graph node KEY is sanitised; cfg['id'] keeps the original agent id so
    broadcasts, _skipped cascade keys, and results keying stay unchanged.
    """
    return aid.replace(":", "--")


def _plan_levels(agents: list[dict], edges: list[dict]) -> tuple[list[list[str]], dict[str, dict]]:
    """Topologically level the agents and build a per-node config dict.

    Mirrors api.sessions._build_execution_plan (BFS levels, OR-condition lists,
    required predecessors), returning (levels, cfgs).
    """
    id_map = {a["id"]: a for a in agents}
    in_degree: dict[str, int] = {a["id"]: 0 for a in agents}
    adjacency: dict[str, list[str]] = {a["id"]: [] for a in agents}

    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if src not in adjacency or tgt not in in_degree:
            continue
        adjacency[src].append(tgt)
        in_degree[tgt] = in_degree.get(tgt, 0) + 1

    unconditional_targets: set[str] = {e["target"] for e in edges if not e.get("condition")}

    incoming_conditions: dict[str, list[tuple[str, str]]] = {}
    for edge in edges:
        src, tgt = edge["source"], edge["target"]
        if edge.get("condition") and tgt not in unconditional_targets:
            source = edge.get("condition_source") or src
            incoming_conditions.setdefault(tgt, []).append((edge["condition"], source))

    required_predecessors: dict[str, list[str]] = {}
    for edge in edges:
        if not edge.get("condition"):
            required_predecessors.setdefault(edge["target"], []).append(edge["source"])

    # BFS level-by-level (Kahn): nodes at in_degree 0 at the same time share a level.
    current_level = [aid for aid, deg in in_degree.items() if deg == 0]
    levels: list[list[str]] = []
    while current_level:
        levels.append(current_level)
        next_level: list[str] = []
        for aid in current_level:
            for nxt in adjacency.get(aid, []):
                in_degree[nxt] -= 1
                if in_degree[nxt] == 0:
                    next_level.append(nxt)
        current_level = next_level

    cfgs: dict[str, dict] = {}
    order = 0
    for level in levels:
        for aid in level:
            if aid not in id_map:
                continue
            order += 1
            cond_list = incoming_conditions.get(aid, [])
            cfgs[aid] = {
                "id":         aid,
                "order":      order,
                "label":      id_map[aid].get("label", aid),
                "task_type":  id_map[aid].get("task_type", ""),
                "bed_limit":  id_map[aid].get("bed_limit"),
                "conditions": [{"condition": c, "condition_source": s} for c, s in cond_list],
                "required_predecessors": required_predecessors.get(aid, []),
            }
    # Drop any agent ids that fell outside id_map from the levels
    levels = [[aid for aid in level if aid in cfgs] for level in levels]
    levels = [lvl for lvl in levels if lvl]
    return levels, cfgs


def build_session_graph(pipeline: dict, checkpointer):
    """Compile a per-session StateGraph from a pipeline dict (agents + edges).

    The pipeline's ``strategy`` (chosen by the planner from services.strategies'
    config) decides how each execution level coordinates. ``common_goal`` (the
    default) keeps the original per-agent topology -- one graph node per agent,
    bipartite-wired between levels -- so existing sessions and checkpoints are
    byte-for-byte unchanged. Any other strategy collapses each level into a
    single strategy-driven level node that arbitrates that level's agents.
    """
    agents = pipeline.get("agents", [])
    edges = pipeline.get("edges", [])
    levels, cfgs = _plan_levels(agents, edges)

    strategy_id = pipeline.get("strategy") or default_strategy_id()
    handler = get_handler(strategy_id)
    use_default_topology = strategy_id == default_strategy_id()

    g = StateGraph(SessionState)
    g.add_node(_SYNTH, synthesise_node)

    if not levels:
        g.add_edge(START, _SYNTH)
        g.add_edge(_SYNTH, END)
        return g.compile(checkpointer=checkpointer)

    if use_default_topology:
        # -- Unchanged path: one node per agent, bipartite barrier between levels.
        for aid, cfg in cfgs.items():
            g.add_node(_safe(aid), make_agent_node(cfg))
        for aid in levels[0]:
            g.add_edge(START, _safe(aid))
        for k in range(len(levels) - 1):
            for src in levels[k]:
                for tgt in levels[k + 1]:
                    g.add_edge(_safe(src), _safe(tgt))
        for aid in levels[-1]:
            g.add_edge(_safe(aid), _SYNTH)
    else:
        # -- Strategy path: collapse each level into one strategy-driven node.
        # The level node hands the level's AgentUnits to the handler, which owns
        # the run loop (commit-all / bid-and-arbitrate / ...). Levels are chained
        # sequentially -- the same BSP barrier, now one node per level.
        level_node_ids: list[str] = []
        for k, level in enumerate(levels):
            units = [make_agent_node(cfgs[aid]) for aid in level]
            lid = f"__level_{k}__"
            g.add_node(lid, _make_level_node(units, handler))
            level_node_ids.append(lid)
        g.add_edge(START, level_node_ids[0])
        for k in range(len(level_node_ids) - 1):
            g.add_edge(level_node_ids[k], level_node_ids[k + 1])
        g.add_edge(level_node_ids[-1], _SYNTH)

    g.add_edge(_SYNTH, END)

    order_str = " -> ".join("|".join(lvl) for lvl in levels)
    logger.info("session graph built  strategy=%s  levels=[%s]", strategy_id, order_str)
    return g.compile(checkpointer=checkpointer)
