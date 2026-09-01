"""Static checks on the flow catalog itself — no live stack required.

These are the tests that keep the suite honest as the product changes:

  * every plannable agent in the registry appears in at least one themed flow,
    so adding an agent without covering it FAILS rather than silently leaving a
    hole;
  * the all-agent flow really does use them all;
  * every pipeline is structurally valid (edges reference declared agents, no
    cycles, no orphans), so a typo in a flow definition is caught here in plain
    CI instead of surfacing as a confusing timeout in a live run.

They import the registry but never contact a service, so they run in ordinary
CI alongside the unit tests.
"""
import pytest

from _flows import ALL_FLOWS, ALL_PLANNABLE, FLOW_ALL, THEMED_FLOWS


def _ids(flows):
    return [f["name"] for f in flows]


def _agent_ids(flow) -> set[str]:
    return {a["id"] for a in flow["pipeline"]["agents"]}


# ── coverage ─────────────────────────────────────────────────────────────────

def test_themed_flows_cover_every_plannable_agent():
    """The five themed flows must between them touch every agent. If this fails,
    an agent was added to the registry but no flow exercises it."""
    covered = set().union(*(_agent_ids(f) for f in THEMED_FLOWS))
    missing = set(ALL_PLANNABLE) - covered
    assert not missing, (
        f"no themed flow covers {sorted(missing)} — add them to a flow in _flows.py"
    )


def test_all_agents_flow_uses_every_plannable_agent():
    assert _agent_ids(FLOW_ALL) == set(ALL_PLANNABLE), (
        f"FLOW_ALL is out of step with ALL_PLANNABLE: "
        f"missing={sorted(set(ALL_PLANNABLE) - _agent_ids(FLOW_ALL))} "
        f"extra={sorted(_agent_ids(FLOW_ALL) - set(ALL_PLANNABLE))}"
    )


def test_all_plannable_agents_exist_in_the_registry():
    """Guards against a flow naming an agent the planner no longer knows: that
    would make the live run quietly skip it rather than fail."""
    from workflows.planner import SUB_AGENTS

    unknown = set(ALL_PLANNABLE) - set(SUB_AGENTS)
    assert not unknown, (
        f"flows reference agents absent from the registry: {sorted(unknown)}"
    )


def test_registry_agents_are_all_accounted_for():
    """The other direction: an agent in the registry that no flow covers.

    patient_verification_agent is the one deliberate exclusion — it is planner-
    injected and parks on a HITL interrupt, so it has no place in a straight-
    through flow.
    """
    from workflows.planner import SUB_AGENTS

    expected_uncovered = {"patient_verification_agent"}
    uncovered = set(SUB_AGENTS) - set(ALL_PLANNABLE) - expected_uncovered
    assert not uncovered, (
        f"registry agents covered by no flow: {sorted(uncovered)} — either add "
        f"them to a themed flow or document the exclusion in _flows.py"
    )


# ── structural validity of each pipeline ─────────────────────────────────────

@pytest.mark.parametrize("flow", ALL_FLOWS, ids=_ids(ALL_FLOWS))
def test_edges_reference_declared_agents(flow):
    """The builder silently DROPS an edge whose endpoint isn't a declared agent,
    so a typo would quietly change the topology instead of erroring."""
    declared = _agent_ids(flow)
    for edge in flow["pipeline"]["edges"]:
        assert edge["source"] in declared, (
            f"[{flow['name']}] edge source {edge['source']!r} is not a declared agent"
        )
        assert edge["target"] in declared, (
            f"[{flow['name']}] edge target {edge['target']!r} is not a declared agent"
        )


@pytest.mark.parametrize("flow", ALL_FLOWS, ids=_ids(ALL_FLOWS))
def test_no_duplicate_agents(flow):
    ids = [a["id"] for a in flow["pipeline"]["agents"]]
    assert len(ids) == len(set(ids)), f"[{flow['name']}] declares an agent twice: {ids}"


@pytest.mark.parametrize("flow", ALL_FLOWS, ids=_ids(ALL_FLOWS))
def test_pipeline_is_acyclic(flow):
    """A cycle makes topological levelling drop nodes, so the flow would run a
    subset of its agents and still look like it passed."""
    adjacency: dict[str, list[str]] = {a["id"]: [] for a in flow["pipeline"]["agents"]}
    for e in flow["pipeline"]["edges"]:
        adjacency[e["source"]].append(e["target"])

    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(adjacency, WHITE)

    def visit(node: str, trail: list[str]) -> None:
        colour[node] = GREY
        for nxt in adjacency[node]:
            if colour[nxt] == GREY:
                pytest.fail(f"[{flow['name']}] cycle: {' -> '.join(trail + [node, nxt])}")
            if colour[nxt] == WHITE:
                visit(nxt, trail + [node])
        colour[node] = BLACK

    for node in adjacency:
        if colour[node] == WHITE:
            visit(node, [])


@pytest.mark.parametrize("flow", ALL_FLOWS, ids=_ids(ALL_FLOWS))
def test_no_orphan_agents(flow):
    """Every agent must sit on at least one edge. An unconnected agent lands in
    level 0 and runs immediately, which is almost never what the flow intended."""
    if len(flow["pipeline"]["agents"]) == 1:
        return
    on_an_edge = set()
    for e in flow["pipeline"]["edges"]:
        on_an_edge.add(e["source"])
        on_an_edge.add(e["target"])
    orphans = _agent_ids(flow) - on_an_edge
    assert not orphans, f"[{flow['name']}] agents on no edge: {sorted(orphans)}"


@pytest.mark.parametrize("flow", ALL_FLOWS, ids=_ids(ALL_FLOWS))
def test_flow_declares_a_goal_and_expectations(flow):
    assert flow["goal"].strip(), f"[{flow['name']}] has no goal text"
    assert flow["expect_agents"], f"[{flow['name']}] declares no expected agents"
    assert set(flow["expect_agents"]) <= _agent_ids(flow), (
        f"[{flow['name']}] expects agents it does not declare"
    )


def test_flow_names_are_unique():
    names = [f["name"] for f in ALL_FLOWS]
    assert len(names) == len(set(names)), f"duplicate flow names: {names}"


# ── the pipelines actually compile ───────────────────────────────────────────

@pytest.mark.parametrize("flow", ALL_FLOWS, ids=_ids(ALL_FLOWS))
def test_pipeline_levels_match_the_declared_edges(flow):
    """Run the real leveller over each pipeline. This catches a flow whose edges
    produce different levels than intended WITHOUT needing a live stack — the
    same `_plan_levels` the builder uses at runtime."""
    from workflows.graph.builder import _plan_levels

    levels, cfgs = _plan_levels(flow["pipeline"]["agents"], flow["pipeline"]["edges"])

    assert levels, f"[{flow['name']}] levelled to nothing"
    flat = [aid for lvl in levels for aid in lvl]
    assert set(flat) == _agent_ids(flow), (
        f"[{flow['name']}] levelling dropped agents: "
        f"{sorted(_agent_ids(flow) - set(flat))}"
    )

    position = {aid: i for i, lvl in enumerate(levels) for aid in lvl}
    for e in flow["pipeline"]["edges"]:
        assert position[e["source"]] < position[e["target"]], (
            f"[{flow['name']}] {e['source']} -> {e['target']} not levelled in order"
        )
