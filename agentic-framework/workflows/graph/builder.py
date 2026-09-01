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
from workflows.strategies import get_handler, default_strategy_id, is_valid_strategy

logger = logging.getLogger(__name__)

_SYNTH = "__synthesise__"

# Tie-break order if a level ever carries >1 distinct non-default strategy tag
# (most-arbitrating first). With only `bidding` defined today this is effectively
# a no-op, but it keeps _resolve_level_strategy correct as strategies are added.
_STRATEGY_PRECEDENCE = ["bidding", "common_goal"]


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
                "strategy":   id_map[aid].get("strategy"),   # per-agent execution strategy (None = inherit default)
                "conditions": [{"condition": c, "condition_source": s} for c, s in cond_list],
                "required_predecessors": required_predecessors.get(aid, []),
            }
    # Drop any agent ids that fell outside id_map from the levels
    levels = [[aid for aid in level if aid in cfgs] for level in levels]
    levels = [lvl for lvl in levels if lvl]
    return levels, cfgs


def build_session_graph(pipeline: dict, checkpointer):
    """Compile a per-session StateGraph from a pipeline dict (agents + edges).

    Execution strategy is now resolved PER LEVEL from the agents' own ``strategy``
    tags (planner-set; None = inherit). A level whose resolved strategy is the
    default (``common_goal``) keeps the original per-agent topology -- one graph
    node per agent, bipartite-wired -- so a fully-default pipeline is byte-for-byte
    unchanged. A non-default level collapses into a single strategy-driven level
    node that arbitrates that level's agents. Mixed pipelines wire the two shapes
    together (fan-in / fan-out); the BSP barrier between levels holds throughout.
    """
    agents = pipeline.get("agents", [])
    edges = pipeline.get("edges", [])
    levels, cfgs = _plan_levels(agents, edges)

    default_id = default_strategy_id()
    # Backward-compat: an old snapshot (or a stray LLM field) may still carry a
    # top-level pipeline["strategy"] -- honour it as the fallback default for any
    # level whose agents carry no tag. New plans have no top-level strategy, so this
    # is just default_id.
    fallback = pipeline.get("strategy") if is_valid_strategy(pipeline.get("strategy")) else default_id

    def _resolve_level_strategy(level: list[str]) -> str:
        tags = [cfgs[aid].get("strategy") for aid in level]
        tags = [s for s in tags if is_valid_strategy(s) and s != default_id]
        distinct = list(dict.fromkeys(tags))          # de-dup, keep order
        if not distinct:
            return fallback
        if len(distinct) == 1:
            return distinct[0]
        chosen = next((s for s in _STRATEGY_PRECEDENCE if s in distinct), distinct[0])
        logger.warning("level %s carries conflicting strategies %s -- using %r by precedence",
                       level, distinct, chosen)
        return chosen

    level_strategy = [_resolve_level_strategy(lvl) for lvl in levels]

    g = StateGraph(SessionState)
    g.add_node(_SYNTH, synthesise_node)

    if not levels:
        g.add_edge(START, _SYNTH)
        g.add_edge(_SYNTH, END)
        return g.compile(checkpointer=checkpointer)

    if all(s == default_id for s in level_strategy):
        # -- Fast path (unchanged): one node per agent, bipartite barrier between levels.
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
        # -- Mixed path: each level is EITHER per-agent nodes (default strategy) OR one
        # collapsed strategy node. `level_nodes[k]` is the graph node id(s) standing for
        # level k. Wiring is exits x entries between consecutive levels, and since a
        # level's node id(s) serve as both its entry and exit set, one cross-product
        # loop yields every shape combination: per-agent<->per-agent = NxM bipartite,
        # per-agent->collapsed = fan-in, collapsed->per-agent = fan-out, collapsed->
        # collapsed = a plain chain. LangGraph's BSP barrier holds in every case.
        level_nodes: list[list[str]] = []
        for k, level in enumerate(levels):
            if level_strategy[k] == default_id:
                for aid in level:
                    g.add_node(_safe(aid), make_agent_node(cfgs[aid]))
                level_nodes.append([_safe(aid) for aid in level])
            else:
                units = [make_agent_node(cfgs[aid]) for aid in level]
                lid = f"__level_{k}__"
                g.add_node(lid, _make_level_node(units, get_handler(level_strategy[k])))
                level_nodes.append([lid])
        for nid in level_nodes[0]:
            g.add_edge(START, nid)
        for k in range(len(level_nodes) - 1):
            for src in level_nodes[k]:
                for tgt in level_nodes[k + 1]:
                    g.add_edge(src, tgt)
        for nid in level_nodes[-1]:
            g.add_edge(nid, _SYNTH)

    g.add_edge(_SYNTH, END)

    order_str = " -> ".join(
        "|".join(lvl) + (f"[{level_strategy[i]}]" if level_strategy[i] != default_id else "")
        for i, lvl in enumerate(levels)
    )
    logger.info("session graph built  levels=[%s]", order_str)
    return g.compile(checkpointer=checkpointer)
