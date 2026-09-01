"""Regression tests for the budget layer.

These tests pin the budget derivation and burn-band behavior so the allocation engine remains
consistent across different config states and shift scenarios.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from allocation.budget import (
    burn_band,
    compute_cost,
    contention,
    derive_base,
    max_affordable_bid,
    open_shift,
    recover,
    resolve_shift,
    settle,
)
from allocation.budget.factors import (
    BudgetFactors,
    demand_factor,
    fairness_factor,
    scarcity_factor,
)
from allocation.contracts import AgentKind, BudgetSource

TOL = 0.5  # the reference rounds Base to whole points (80 / 13 / 18)

AGENTS = (AgentKind.ER, AgentKind.OT, AgentKind.WARD)


@pytest.fixture(name="config", scope="module")
def derived_config():
    """The shipped config with ``base.mode`` forced to ``derived``.

    Shadows the session-wide fixture for this module only. Without it these tests would
    silently start measuring RL-Steps' common Base while claiming to verify AGENT_BUDGET's
    worked example.
    """
    from dataclasses import replace

    from allocation.config import load_config

    shipped = load_config()
    budget = {**shipped.budget, "base": {**shipped.budget["base"], "mode": "derived"}}
    return replace(shipped, budget=budget)


# ---------------------------------------------------------------------------------------
# Base — AGENT_BUDGET section 4
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "expected_bid", "cost_win", "cost_loss", "base"),
    [
        (AgentKind.ER, 69.6, 19.1, 1.9, 80.0),
        (AgentKind.OT, 22.2, 6.1, 0.6, 13.0),
        (AgentKind.WARD, 29.7, 8.2, 0.8, 18.0),
    ],
)
def test_base_derivation(config, agent, expected_bid, cost_win, cost_loss, base):
    """Section 4's table, reproduced from E[utility] x E[alpha] and the target win counts."""
    derived = derive_base(config, agent)
    assert derived.expected_bid == pytest.approx(expected_bid, abs=0.1)
    assert derived.cost_per_win == pytest.approx(cost_win, abs=0.1)
    assert derived.cost_per_loss == pytest.approx(cost_loss, abs=0.1)
    assert derived.base == pytest.approx(base, abs=TOL)


def test_base_binds_by_construction(config):
    """Section 8: a department hitting its targets exactly exhausts its budget.

    This is the test RL-Steps' own 1000/800/700 fails at ~8 % burn. Here it is 100 % because
    Base is *defined* as the spend implied by the targets.
    """
    for agent in AGENTS:
        assert derive_base(config, agent).theoretical_burn == pytest.approx(1.0, abs=0.01)


def test_incoherent_targets_are_rejected(config):
    """More target wins than expected requests is not a budget, it is a typo."""
    broken = dict(config.budget)
    broken["targets"] = {"er": {"n_win": 9, "n_req": 6}}
    from dataclasses import replace

    with pytest.raises(ValueError, match="exceeds expected requests"):
        derive_base(replace(config, budget=broken), AgentKind.ER)


# ---------------------------------------------------------------------------------------
# Factors — AGENT_BUDGET section 5
# ---------------------------------------------------------------------------------------


def test_demand_uses_forecast_over_its_own_median(config):
    """RL-Steps' own example: ER forecast 6 against a normal 5."""
    value, source = demand_factor(config, forecast=6.0, median_30d=5.0)
    assert value == pytest.approx(1.20)
    assert "/icu/demand" in source


def test_demand_is_clamped(config):
    assert demand_factor(config, 20.0, 5.0)[0] == pytest.approx(1.3)
    assert demand_factor(config, 1.0, 5.0)[0] == pytest.approx(0.8)


def test_demand_falls_back_when_history_is_short(config):
    """F-18 — the 30-day median cannot be built retroactively."""
    value, source = demand_factor(config, forecast=6.0, median_30d=None)
    assert value == pytest.approx(1.0)
    assert "F-18" in source


def test_fairness_is_one_until_the_log_exists(config):
    """v1. B.12 needs ~10 shifts of win/loss history that nothing currently records."""
    for agent in AGENTS:
        value, source = fairness_factor(config, agent)
        assert value == pytest.approx(1.0)
        assert "B.12" in source


def test_fairness_v2_corrects_in_the_right_direction(config):
    """Checked against RL-Steps' own two cases, using the v2 formula directly.

    ER over-winning must pull below 1; OT under-winning must pull above 1. The +-0.05 clamp
    keeps it a small correction rather than something that overrides clinical urgency.
    """
    from allocation.features.scale import clamp

    cfg = config.budget["factors"]["fairness"]
    lo, hi = (float(x) for x in cfg["clamp"])
    weight = float(cfg["v2_weight"])

    over = clamp(1.0 + weight * (0.50 - 0.65), lo, hi)   # ER expected .50, actual .65
    under = clamp(1.0 + weight * (0.25 - 0.10), lo, hi)  # OT expected .25, actual .10
    assert over < 1.0 and over >= lo
    assert under > 1.0 and under <= hi


def test_scarcity_is_global_and_bounded(config):
    """One value for every agent. Below the onset it is exactly 1.0."""
    assert scarcity_factor(config, 0.80)[0] == pytest.approx(1.0)
    assert scarcity_factor(config, 0.85)[0] == pytest.approx(1.0)
    assert scarcity_factor(config, 1.00)[0] == pytest.approx(1.3)
    assert scarcity_factor(config, 0.925)[0] == pytest.approx(1.15, abs=0.01)


# ---------------------------------------------------------------------------------------
# Contention and spend — AGENT_BUDGET section 7
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "occ", "disch", "expected", "label"),
    [
        (1, 0.85, 3.0, 0.80, "one patient needs it — RL-Steps: low"),
        (2, 0.90, 2.0, 0.97, "two patients — normal"),
        (4, 0.92, 2.0, 1.19, "four patients — high (table says 1.2)"),
        (4, 0.98, 0.0, 1.28, "98 %, no discharge — very high"),
    ],
)
def test_contention_reproduces_rl_steps_labels(config, n, occ, disch, expected, label):
    """The only validation available: RL-Steps gives four labels and no formula."""
    assert contention(config, n, occ, disch) == pytest.approx(expected, abs=0.01), label


def test_contention_is_monotone_in_occupancy(config):
    """Bad input must not blow up a budget, and pressure must not decrease with occupancy."""
    values = [contention(config, 3, occ, 1.0) for occ in (0.80, 0.88, 0.95, 1.00)]
    assert values == sorted(values)
    assert all(0.8 <= v <= 1.3 for v in values)


def test_cost_uses_the_commitment_rate(config):
    """RL-Steps section 4's own worked example: bid 100, contention 1.1, win -> 27.5."""
    assert compute_cost(config, 100.0, 1.1, won=True).cost == pytest.approx(27.5, abs=0.01)
    assert compute_cost(config, 100.0, 1.1, won=False).cost == pytest.approx(2.75, abs=0.01)


def test_affordability_constrains_cost_not_bid(config):
    """AGENT_BUDGET 7.3 — RL-Steps' ``Bid <= Remaining`` over-restricts by 4x at rate 0.25."""
    remaining = 20.0
    allowed = max_affordable_bid(config, remaining, contention_factor=1.0, won=True)
    assert allowed == pytest.approx(80.0)
    assert compute_cost(config, allowed, 1.0, won=True).cost == pytest.approx(remaining)


# ---------------------------------------------------------------------------------------
# Section 11 — the worked example, end to end
# ---------------------------------------------------------------------------------------


@pytest.fixture
def shift(config):
    return resolve_shift(config, datetime(2026, 8, 7, 13, 0, tzinfo=timezone.utc))


#: Section 11 uses Demand 1.20 for ER (RL-Steps' 6/5 example) and 1.00 for OT and Ward, with
#: Fairness 1.00 and Scarcity 1.30 at 100 % occupancy.
SECTION_11_FACTORS = {
    # Criticality is 1.00 for every agent here: AGENT_BUDGET v0.3 dropped the factor, so
    # section 11's published totals were computed without it. Passing RL-Steps' 1.15 for ER
    # would break section 11's own arithmetic.
    AgentKind.ER: BudgetFactors(1.20, 1.00, 1.00, 1.30, "injected", "v0.3 drops it", "v1", "occ 100%"),
    AgentKind.OT: BudgetFactors(1.00, 1.00, 1.00, 1.30, "injected", "v0.3 drops it", "v1", "occ 100%"),
    AgentKind.WARD: BudgetFactors(1.00, 1.00, 1.00, 1.30, "injected", "v0.3 drops it", "v1", "occ 100%"),
}


@pytest.mark.parametrize(
    ("agent", "expected"), [(AgentKind.ER, 125.0), (AgentKind.OT, 17.0), (AgentKind.WARD, 23.0)]
)
def test_section_11_shift_budgets(config, shift, agent, expected):
    state = open_shift(config, derive_base(config, agent), SECTION_11_FACTORS[agent], shift)
    assert state.budget_total == pytest.approx(expected, abs=TOL)
    assert state.source is BudgetSource.SEED, "first shift is seeded at Base"


def test_section_11_first_auction(config, shift):
    """Three bidders, ICU at 100 %, one discharge expected in 4 h.

    ER wins at 67.5 (ceiling 107.1 x alpha 0.63) for a cost of 20.0 — 16 % of its shift
    budget on a single win. Under RL-Steps' 1000-point calibration the same win costs 2 %,
    which is the difference between a budget that binds and one that does not.
    """
    contention_factor = contention(config, n_bidders=3, occupancy=1.00, expected_discharges_4h=1.0)
    assert contention_factor == pytest.approx(1.183, abs=0.001)

    er = open_shift(config, derive_base(config, AgentKind.ER), SECTION_11_FACTORS[AgentKind.ER], shift)
    after, result = settle(config, er, bid=67.5, contention_factor=contention_factor, won=True)

    assert result.cost == pytest.approx(20.0, abs=0.1)
    assert after.budget_remaining == pytest.approx(105.0, abs=TOL)
    assert result.cost / er.budget_total == pytest.approx(0.16, abs=0.01)


@pytest.mark.parametrize(
    ("agent", "bid", "cost", "remaining"),
    [(AgentKind.WARD, 28.8, 0.85, 22.2), (AgentKind.OT, 21.5, 0.64, 16.4)],
)
def test_section_11_losers_pay_a_participation_charge(config, shift, agent, bid, cost, remaining):
    """Outcome factor 0.1. The small charge is what stops endless meaningless auctions."""
    contention_factor = contention(config, 3, 1.00, 1.0)
    state = open_shift(config, derive_base(config, agent), SECTION_11_FACTORS[agent], shift)
    after, result = settle(config, state, bid=bid, contention_factor=contention_factor, won=False)
    assert result.cost == pytest.approx(cost, abs=0.05)
    assert after.budget_remaining == pytest.approx(remaining, abs=TOL)


# ---------------------------------------------------------------------------------------
# Renewal and burn — AGENT_BUDGET sections 8 and 9
# ---------------------------------------------------------------------------------------


def test_recovery_cannot_exceed_the_shift_total(config, shift):
    """The ``min`` cap is what prevents hoarding — a quiet hour must not bank capacity."""
    base = derive_base(config, AgentKind.ER)
    state = open_shift(config, base, SECTION_11_FACTORS[AgentKind.ER], shift)
    spent, _ = settle(config, state, bid=20.0, contention_factor=1.1, won=True)
    assert recover(config, spent, hours=100.0).budget_remaining == pytest.approx(
        state.budget_total
    )


def test_recovery_replaces_half_the_run_rate(config, shift):
    """``B / 8 x rho`` with rho = 0.5, so a steady spender drifts down and an idle one recovers."""
    base = derive_base(config, AgentKind.ER)
    state = open_shift(config, base, SECTION_11_FACTORS[AgentKind.ER], shift)
    spent, _ = settle(config, state, bid=67.5, contention_factor=1.183, won=True)
    after = recover(config, spent, hours=1.0)
    expected = spent.budget_remaining + state.budget_total / 8 * 0.5
    assert after.budget_remaining == pytest.approx(expected)


def test_budget_cannot_go_negative(config, shift):
    state = open_shift(config, derive_base(config, AgentKind.OT), SECTION_11_FACTORS[AgentKind.OT], shift)
    after, _ = settle(config, state, bid=10_000.0, contention_factor=1.3, won=True)
    assert after.budget_remaining == 0.0
    assert after.spent > state.budget_total


def test_budget_only_binds_late_in_the_shift(config, shift):
    """When does the budget actually constrain a bid, rather than the ceiling?

    Under a 0.25 commitment rate and contention ~1.18, a department may bid roughly 3.4x its
    remaining budget. So ER — ceiling 107.1, shift budget 125.4 — can bid its full ceiling
    until about 32 points remain, i.e. after spending ~75 % of the shift allowance.

    **That is the mechanism working**, and it is worth pinning: RL-Steps section 21 asks that a
    department "must concede marginal cases" rather than be throttled from the first auction.
    But it also means the budget is invisible to the policy for the first few wins, which is
    exactly the regime where a mis-set commitment rate would go unnoticed.
    """
    contention_factor = contention(config, 3, 1.00, 1.0)
    ceiling = 107.1
    state = open_shift(config, derive_base(config, AgentKind.ER), SECTION_11_FACTORS[AgentKind.ER], shift)

    binding_at = compute_cost(config, ceiling, contention_factor, won=True).cost
    assert binding_at == pytest.approx(31.7, abs=0.5)
    assert binding_at / state.budget_total == pytest.approx(0.25, abs=0.02)

    assert max_affordable_bid(config, state.budget_total, contention_factor) > ceiling
    assert max_affordable_bid(config, binding_at - 1.0, contention_factor) < ceiling


def test_burn_bands(config):
    """Below 40 % the budget is inert and bidding maximum is free."""
    assert burn_band(config, 0.08) == "inert", "RL-Steps' own 1000/800/700 lands here"
    assert burn_band(config, 0.90) == "working"
    assert burn_band(config, 2.00) == "starved"


def test_burn_rate_tracks_spend_not_the_remainder(config, shift):
    """Recovery moves the remainder, so burn must be measured from cumulative spend."""
    state = open_shift(config, derive_base(config, AgentKind.ER), SECTION_11_FACTORS[AgentKind.ER], shift)
    after, result = settle(config, state, bid=67.5, contention_factor=1.183, won=True)
    recovered = recover(config, after, hours=4.0)
    assert recovered.burn_rate == pytest.approx(result.cost / state.budget_total)
    assert recovered.budget_remaining > after.budget_remaining


# ---------------------------------------------------------------------------------------
# Shifts — F-17
# ---------------------------------------------------------------------------------------


def _local(config, year, month, day, hour, minute=0):
    """A wall-clock time on the ward.

    Shift boundaries are local times, so a test that builds them in UTC is testing the offset,
    not the roster. Asia/Kolkata is UTC+5:30, which is exactly the kind of gap that would put
    a night shift on the wrong calendar day.
    """
    from allocation.budget.shifts import shift_timezone

    return datetime(year, month, day, hour, minute, tzinfo=shift_timezone(config))


def test_shifts_tile_the_day(config):
    """A gap would silently attribute spend to no shift at all."""
    for hour in range(24):
        moment = _local(config, 2026, 8, 7, hour, 30)
        shift = resolve_shift(config, moment)
        assert shift.contains(moment)


def test_night_shift_crosses_midnight(config):
    """23:00-07:00 is one shift spanning two dates, not two shifts."""
    late = resolve_shift(config, _local(config, 2026, 8, 7, 23, 30))
    early = resolve_shift(config, _local(config, 2026, 8, 8, 2, 0))
    assert late.label == early.label == "night"
    assert late.shift_id == early.shift_id, "one shift, not two"
    assert late.hours == pytest.approx(8.0)


def test_boundaries_are_local_not_utc(config):
    """The roster is wall-clock. 02:00 UTC is 07:30 in Asia/Kolkata — a morning shift."""
    assert resolve_shift(config, datetime(2026, 8, 8, 2, 0, tzinfo=timezone.utc)).label == "morning"
    assert resolve_shift(config, _local(config, 2026, 8, 8, 2, 0)).label == "night"


def test_unknown_roster_label_raises(config):
    """An unmatched label must not default — it would corrupt every budget row silently."""
    from allocation.budget.shifts import normalise_label

    assert normalise_label(config, "Day") == "morning"
    with pytest.raises(ValueError, match="F-17"):
        normalise_label(config, "twilight")
