"""Structural checks that run without a live stack.

Deliberately small. An earlier version of this file also asserted things about
the flow catalog against itself — no duplicate agents, no cycles, every flow has
a goal. Those only ever failed if someone edited `_flows.py` badly, so they were
lint on a constant and were removed as noise.

What is left calls PRODUCTION code and can genuinely fail because of someone
else's change:

  * the two registry-drift checks, which compare the flows against the live
    planner registry;
  * the leveller check, which runs the real `_plan_levels` over each pipeline.

Everything here is cheap and needs no services, so it runs in ordinary CI. None
of it says anything about whether an agent BEHAVES correctly — that is what
`test_flows_live.py` and `tests/e2e` are for.
"""
import pytest

from _flows import ALL_FLOWS, ALL_PLANNABLE, PENDING_FLOW_COVERAGE


def _ids(flows):
    return [f["name"] for f in flows]


def _agent_ids(flow) -> set[str]:
    return {a["id"] for a in flow["pipeline"]["agents"]}


# ── registry drift ───────────────────────────────────────────────────────────

def test_flows_reference_only_agents_the_planner_knows():
    """A flow naming an agent the registry no longer has.

    This is the failure mode that would otherwise be invisible: the graph does
    not error on an unknown agent, it simply never schedules that node — so the
    live flow would quietly run three agents instead of four AND STILL PASS.
    Renaming or removing an agent without updating the flows lands here instead,
    in under a second and with no stack.
    """
    from workflows.planner import SUB_AGENTS

    unknown = set(ALL_PLANNABLE) - set(SUB_AGENTS)
    assert not unknown, (
        f"flows reference agents absent from the planner registry: {sorted(unknown)}. "
        f"If an agent was renamed, update ALL_PLANNABLE and the flow that uses it."
    )


def test_new_registry_agents_are_covered_or_explicitly_pending():
    """A registry agent that no flow covers must be listed in PENDING_FLOW_COVERAGE.

    Adding an agent deliberately does NOT fail CI on its own — the author records
    it as pending in the same PR and the entry comes out when the flow lands.
    That keeps the gap visible and reviewable without blocking the agent work,
    which is the whole point: an author who cannot run the live stack should
    still be able to land an agent.
    """
    from workflows.planner import SUB_AGENTS

    uncovered = set(SUB_AGENTS) - set(ALL_PLANNABLE) - set(PENDING_FLOW_COVERAGE)
    assert not uncovered, (
        f"registry agents with no flow and no PENDING_FLOW_COVERAGE entry: "
        f"{sorted(uncovered)}.\n"
        f"Either add each to a themed flow in _flows.py, or record it in "
        f"PENDING_FLOW_COVERAGE with a one-line reason."
    )


def test_pending_coverage_list_has_no_stale_entries():
    """The pending list must not outlive its usefulness: an entry for an agent
    that is now covered, or that no longer exists, is stale and should go."""
    from workflows.planner import SUB_AGENTS

    for agent_id in PENDING_FLOW_COVERAGE:
        assert agent_id in SUB_AGENTS, (
            f"PENDING_FLOW_COVERAGE lists {agent_id!r}, which is not in the "
            f"registry — remove the entry."
        )
        assert agent_id not in ALL_PLANNABLE, (
            f"{agent_id!r} is covered by a flow now — remove it from "
            f"PENDING_FLOW_COVERAGE."
        )


# ── the real leveller ────────────────────────────────────────────────────────

@pytest.mark.parametrize("flow", ALL_FLOWS, ids=_ids(ALL_FLOWS))
def test_pipeline_levels_match_the_declared_edges(flow):
    """Run the REAL `_plan_levels` — the same topological sort the graph builder
    uses at runtime — over each pipeline shape.

    The pipeline is the input; the assertions are about what the production
    function does with it. The middle assertion is the one that earns its place:
    `_plan_levels` silently DROPS nodes it cannot place (a cycle, an unreachable
    node) rather than raising, so a levelling regression would make every flow
    quietly execute a subset of its agents and still look green.

    Six cases because there are six distinct shapes — fan-out, fan-in, three
    levels, four levels — not because there are six independent behaviours.
    """
    from workflows.graph.builder import _plan_levels

    levels, _cfgs = _plan_levels(flow["pipeline"]["agents"], flow["pipeline"]["edges"])

    assert levels, f"[{flow['name']}] levelled to nothing"

    flat = [aid for lvl in levels for aid in lvl]
    assert set(flat) == _agent_ids(flow), (
        f"[{flow['name']}] levelling dropped agents: "
        f"{sorted(_agent_ids(flow) - set(flat))}"
    )

    position = {aid: i for i, lvl in enumerate(levels) for aid in lvl}
    for e in flow["pipeline"]["edges"]:
        assert position[e["source"]] < position[e["target"]], (
            f"[{flow['name']}] {e['source']} -> {e['target']} not levelled in order "
            f"(levels {position[e['source']]} vs {position[e['target']]})"
        )
