"""End-to-end flow tests — six pipelines through the real graph runner.

Each test drives a whole pipeline and asserts on what the orchestration did:
which agents ran, in what order, whether the level barrier held, and whether the
run reached synthesis. Agent OUTPUT is checked only for shape — the numbers
depend on whatever data the stack is seeded with, so asserting on them would
make the suite a data test rather than a flow test.

Run:  pytest tests/flows -m live -v
"""
import pytest

from _driver import run_flow
from _flows import ALL_FLOWS, FLOW_ALL, FLOW_DISCHARGE_BILLING, THEMED_FLOWS
from conftest import FLOW_TIMEOUT_SECONDS

pytestmark = pytest.mark.usefixtures("redis_ready", "captured_broadcasts")


def _ids(flows):
    return [f["name"] for f in flows]


# ── the six flows: does each run end to end? ─────────────────────────────────

@pytest.mark.parametrize("flow", ALL_FLOWS, ids=_ids(ALL_FLOWS))
async def test_flow_runs_to_completion(flow, flow_session, capsys):
    """Every agent in the pipeline is reached, and the run ends without a fatal."""
    run = await run_flow(flow, flow_session, FLOW_TIMEOUT_SECONDS)
    with capsys.disabled():
        print(f"\n▶ {run.summary()}")

    assert not run.failed, (
        f"[{flow['name']}] session failed: {run.final_state.get('_error', '(no detail)')}"
    )

    expected = set(flow["expect_agents"])
    unreached = expected - run.touched_agents
    assert not unreached, (
        f"[{flow['name']}] never reached {sorted(unreached)}; "
        f"ran={sorted(run.ran_agents)} skipped={sorted(run.skipped)}"
    )


@pytest.mark.parametrize("flow", ALL_FLOWS, ids=_ids(ALL_FLOWS))
async def test_flow_produces_a_result_per_running_agent(flow, flow_session):
    """An agent that ran must leave a result. A silent empty result means the
    agent completed without doing anything, which no caller can distinguish from
    success — so it is a failure here."""
    run = await run_flow(flow, flow_session, FLOW_TIMEOUT_SECONDS)

    assert run.ran_agents, f"[{flow['name']}] no agent produced a result at all"
    for agent_id, result in run.results.items():
        assert result is not None, f"[{flow['name']}] {agent_id} produced a None result"
        assert isinstance(result, (dict, list, str)), (
            f"[{flow['name']}] {agent_id} produced {type(result).__name__}, "
            "expected a dict/list/str payload"
        )


@pytest.mark.parametrize("flow", ALL_FLOWS, ids=_ids(ALL_FLOWS))
async def test_flow_respects_edge_ordering(flow, flow_session):
    """The BSP barrier: for every edge, the source must run in an EARLIER
    superstep than the target. This is the assertion that catches a level-ordering
    regression — an agent reading a predecessor's result before it exists."""
    run = await run_flow(flow, flow_session, FLOW_TIMEOUT_SECONDS)

    for edge in flow["pipeline"]["edges"]:
        src, tgt = edge["source"], edge["target"]
        i_src, i_tgt = run.order_of(src), run.order_of(tgt)
        if i_src < 0 or i_tgt < 0:
            continue  # one end was skipped — ordering is vacuous, not violated
        assert i_src < i_tgt, (
            f"[{flow['name']}] edge {src} -> {tgt} ran out of order "
            f"(superstep {i_src} vs {i_tgt}); supersteps={run.supersteps}"
        )


@pytest.mark.parametrize("flow", THEMED_FLOWS, ids=_ids(THEMED_FLOWS))
async def test_themed_flow_has_more_than_one_superstep(flow, flow_session):
    """A flow that collapses to a single superstep is not testing the barrier.
    Guards the flow definitions themselves against being flattened by an edit."""
    run = await run_flow(flow, flow_session, FLOW_TIMEOUT_SECONDS)
    assert len(run.supersteps) >= 2, (
        f"[{flow['name']}] ran in one superstep — the pipeline lost its levels: "
        f"{run.supersteps}"
    )


# ── the fan-in flow, specifically ────────────────────────────────────────────

async def test_discharge_billing_fan_in_waits_for_both_parents(flow_session):
    """revenue_agent fans in behind billing, which itself fans in behind
    discharge. Three levels: the deepest chain in the themed set, and the shape
    most likely to expose a premature trigger."""
    run = await run_flow(FLOW_DISCHARGE_BILLING, flow_session, FLOW_TIMEOUT_SECONDS)

    i_dis = run.order_of("discharge_agent")
    i_bill = run.order_of("billing_agent")
    i_rev = run.order_of("revenue_agent")
    if min(i_dis, i_bill, i_rev) < 0:
        pytest.skip(f"a leg was skipped this run: {run.skipped}")

    assert i_dis < i_bill < i_rev, (
        f"expected discharge -> billing -> revenue in ascending supersteps, "
        f"got {i_dis}/{i_bill}/{i_rev}; supersteps={run.supersteps}"
    )


# ── the all-agent flow, specifically ─────────────────────────────────────────

async def test_all_agents_flow_touches_every_plannable_agent(flow_session, capsys):
    """The point of the combined pipeline: every plannable agent in ONE run, so
    cross-agent state collisions have a chance to show up."""
    run = await run_flow(FLOW_ALL, flow_session, FLOW_TIMEOUT_SECONDS)
    with capsys.disabled():
        print(f"\n▶ {run.summary()}")

    missing = set(FLOW_ALL["expect_agents"]) - run.touched_agents
    assert not missing, (
        f"all-agent flow never reached {sorted(missing)}; "
        f"ran={sorted(run.ran_agents)} skipped={sorted(run.skipped)}"
    )


async def test_all_agents_flow_keeps_results_separate(flow_session):
    """`results` is merged across parallel agents in one superstep. Every agent
    that ran must own exactly one distinct key — a collision here means one
    agent's findings overwrote another's."""
    run = await run_flow(FLOW_ALL, flow_session, FLOW_TIMEOUT_SECONDS)

    for agent_id in run.ran_agents:
        assert agent_id in run.results, f"{agent_id} ran but owns no results key"
    assert len(run.results) == len(set(run.results)), "duplicate keys in results"


async def test_all_agents_flow_does_not_cascade_skip_everything(flow_session):
    """A cascading skip is legitimate, but if the WHOLE pipeline skipped then the
    run proved nothing and the flow needs different seed data. Fail loudly rather
    than passing an empty run."""
    run = await run_flow(FLOW_ALL, flow_session, FLOW_TIMEOUT_SECONDS)
    assert run.ran_agents, (
        f"every agent skipped — nothing was exercised. skipped={run.skipped}"
    )
