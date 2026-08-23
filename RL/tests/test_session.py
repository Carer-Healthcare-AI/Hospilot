"""Multi-shift sessions — the budget lifecycle that a single auction cannot show.

Everything AGENT_BUDGET sections 8-9 describes only exists across a sequence: burn rate,
hourly recovery, exhaustion, the shift roll. These tests are the first that exercise any of
it end to end.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from allocation.contracts import AgentKind, AuctionMode
from allocation.ingest import fixtures as fx
from allocation.profiles.registry import REGISTRY
from allocation.contracts import ResourceType
from allocation.trigger.session import (
    event_schedule,
    run_session,
    with_rounds,
)

PROFILE = REGISTRY.get(ResourceType.ICU_BED)


@pytest.fixture
def tiny_base(config):
    """A Base small enough to floor at exactly zero.

    ``exhausted`` means *nothing left*, not *nearly nothing* — a threshold like "below 1
    point" would be a second, invisible budget rule. At Base 70 ER lands on 0.1 remaining,
    which is effectively spent but is not zero, so the fixture goes lower rather than the
    definition going fuzzier.
    """
    from dataclasses import replace

    return replace(
        config,
        budget={**config.budget, "base": {**config.budget["base"], "common_points": 20}},
    )


def _session(config, count=14, every=timedelta(minutes=45), profile=None):
    return run_session(
        config=config,
        source=fx.FixtureDataSource(),
        candidates=fx.CANDIDATES,
        start=fx.NOW,
        events=event_schedule(fx.NOW, count, every),
        profile=profile or PROFILE,
    )


# -- the lifecycle ------------------------------------------------------------------------


def test_budgets_carry_across_auctions_instead_of_resetting(config):
    """The failure this exists to prevent: every auction opening at full allowance.

    If budgets reset, burn rate is measured against a balance that was never drawn down and
    the number is meaningless — which is the number the whole mechanism is judged by.
    """
    result = _session(config, count=6, every=timedelta(minutes=30))
    spends = [run.outcome.budgets[AgentKind.ER].spent for run in result.runs]
    assert spends == sorted(spends), "spend must be monotone within a shift"
    assert spends[-1] > spends[0]


def test_a_session_crosses_a_shift_boundary_and_recomputes(config):
    result = _session(config, count=14)
    assert len(result.shifts) >= 2
    assert result.shifts[0].shift.shift_id != result.shifts[1].shift.shift_id


def test_the_new_shift_reopens_the_budget(config):
    """advance_shift recomputes B; it does not carry the remainder forward."""
    result = _session(config, count=14)
    first, second = result.shifts[0], result.shifts[1]
    assert second.opened[AgentKind.ER] > first.closed[AgentKind.ER]


def test_recovery_is_credited_between_events(config):
    result = _session(config, count=6, every=timedelta(hours=1))
    assert result.shifts[0].recovered[AgentKind.ER] > 0


def test_recovery_never_exceeds_the_shift_total(config):
    """The min cap is what stops an agent learning to lose deliberately to bank capacity."""
    result = _session(config, count=8, every=timedelta(hours=2))
    for report in result.shifts:
        for agent, remaining in report.closed.items():
            assert remaining <= report.opened[agent] + 1e-9


def test_exhaustion_is_reported_separately_from_a_high_burn_rate(config, tiny_base):
    """A burn of 1.4 is stressed. A remaining balance of 0 is silent — it cannot bid at all.

    Needs a Base small enough to actually run out. RL-Steps' 700 never does under a 0.25
    commitment rate — ER burns ~18% of a shift allowance across six auctions (F-27) — so the
    exhaustion path would go permanently untested against the shipped number.
    """
    result = _session(tiny_base, count=14, every=timedelta(minutes=45))
    exhausted = {a for r in result.shifts for a in r.exhausted}
    assert AgentKind.ER in exhausted, "a Base this small must run ER dry"


def test_running_dry_is_measured_during_the_shift_not_at_its_end(config, tiny_base):
    """Hourly recovery hides exhaustion from the closing balance.

    A department can run out mid-shift, recover a little before the last auction, and close
    solvent. Reading only the closing balance would report no exhaustion ever — the exact
    failure the flag exists to catch.
    """
    result = _session(tiny_base, count=14, every=timedelta(minutes=45))
    report = result.shifts[0]
    assert AgentKind.ER in report.exhausted
    assert report.closed[AgentKind.ER] > 0, "recovery lifts it back off the floor by the close"


def test_exhaustion_does_not_mean_a_zero_balance(config, tiny_base):
    """A correctly-guarded agent never spends to exactly zero.

    The affordability guard clamps each bid to what the budget covers and floors it to a whole
    point, so some remainder always survives. Defining exhaustion as ``remaining == 0`` would
    therefore never fire — and it only used to, because bids were rounded *after* the guard
    and overspent, with ``ledger.settle``'s ``max(0.0, ...)`` quietly absorbing the excess.
    """
    result = _session(tiny_base, count=14, every=timedelta(minutes=45))
    for report in result.shifts:
        for agent in report.exhausted:
            assert report.closed[agent] > 0.0


def test_rl_steps_common_base_leaves_every_department_inert(config):
    """F-27, asserted rather than described.

    RL-Steps' 700 is not calibrated to a 0.25 commitment rate. Every department lands below
    AGENT_BUDGET section 8's 0.40 inert threshold, where "bidding maximum is free, and the RL
    will learn to do exactly that" — the budget stops being a constraint at all.

    This test should FAIL once ``common_points`` is fitted from observed burn. That is the
    point of it.
    """
    result = _session(config, count=14, every=timedelta(minutes=45))
    for report in result.shifts:
        assert all(band == "inert" for band in report.band.values()), report.band


def test_a_session_cannot_run_live(config):
    """One in-memory ledger cannot stand in for many real budget rows."""
    with pytest.raises(ValueError, match="cannot be live"):
        run_session(
            config=config, source=fx.FixtureDataSource(), candidates=fx.CANDIDATES,
            start=fx.NOW, events=event_schedule(fx.NOW, 2, timedelta(hours=1)),
            profile=PROFILE, mode=AuctionMode.LIVE,
        )


def test_budgets_actually_move_in_a_session_despite_simulation_mode(config):
    """The mode gate asks whether the budget is *real*, not whether the auction is live.

    A single simulated run must not charge. A session must, or it reports zero burn forever.
    """
    result = _session(config, count=4, every=timedelta(hours=1))
    assert result.shifts[0].spent[AgentKind.ER] > 0


# -- knobs --------------------------------------------------------------------------------


def test_more_rounds_produce_more_rounds(config):
    result = _session(config, count=1, profile=with_rounds(PROFILE, 6))
    assert result.runs[0].outcome.result.rounds_run > 3


def test_more_rounds_let_the_winner_climb_higher(config):
    """Three rounds is a cadence, not a convergence criterion — the ladder is unfinished."""
    short = _session(config, count=1, profile=with_rounds(PROFILE, 3))
    long = _session(config, count=1, profile=with_rounds(PROFILE, 8))
    assert long.runs[0].outcome.result.winning_bid > short.runs[0].outcome.result.winning_bid


def test_a_bigger_base_lowers_burn_rate(config, config_dir, tmp_path):
    """``common_points`` is the single knob on budget size under RL-Steps section 4.

    Edited through the YAML rather than the object, because that is the route a user takes
    and it is the one that has to keep working.
    """
    import shutil

    from allocation.config import load_config

    alt = tmp_path / "cfg"
    shutil.copytree(config_dir, alt)
    # The ICU-bed pool specifically: budgets are per resource type (D-3), and this session
    # auctions ICU beds. Raising another bed's pool must leave this run untouched.
    budget = alt / "budget_icu_bed.yaml"
    text = budget.read_text(encoding="utf-8")
    assert "common_points: 700" in text
    budget.write_text(text.replace("common_points: 700", "common_points: 2100"), "utf-8")

    base = _session(config, count=6, every=timedelta(minutes=45))
    rich = _session(load_config(alt), count=6, every=timedelta(minutes=45))

    assert rich.shifts[0].opened[AgentKind.ER] == pytest.approx(
        3 * base.shifts[0].opened[AgentKind.ER]
    )
    assert rich.shifts[0].burn_rate[AgentKind.ER] < base.shifts[0].burn_rate[AgentKind.ER]


def test_a_common_base_gives_every_department_the_same_starting_point(config):
    """RL-Steps section 4: Base is identical; the factors carry every difference.

    Criticality is the only factor that differs today (Demand and Fairness are pinned at 1.0
    and Scarcity is global), so the budget ordering is exactly the criticality ordering.
    """
    result = _session(config, count=2, every=timedelta(minutes=45))
    opened = result.shifts[0].opened
    assert opened[AgentKind.ER] > opened[AgentKind.OT] > opened[AgentKind.WARD]
    assert opened[AgentKind.ER] / opened[AgentKind.WARD] == pytest.approx(1.15, rel=1e-6)
    assert opened[AgentKind.OT] / opened[AgentKind.WARD] == pytest.approx(1.05, rel=1e-6)


def test_zero_rounds_is_rejected():
    with pytest.raises(ValueError, match="at least one round"):
        with_rounds(PROFILE, 0)


def test_an_empty_schedule_is_rejected():
    with pytest.raises(ValueError, match="at least one event"):
        event_schedule(fx.NOW, 0, timedelta(hours=1))


# -- what the session reveals about the mechanism -----------------------------------------

def test_a_static_fixture_gives_one_agent_every_win(config):
    """A known limitation, asserted so it is not mistaken for a finding about fairness.

    The same three patients bid in every auction, so ER's utility is highest every time and
    win share is 100% by construction of the fixture — not by anything the mechanism did.
    Measuring allocation fairness needs an arrival process, which is what ``sim/`` would add.
    """
    result = _session(config, count=10)
    assert result.win_share == {AgentKind.ER: 1.0}
