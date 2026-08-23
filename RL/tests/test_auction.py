"""The auction and the heuristic policy against RL-Steps sections 9-19.

Section 18's three-turn table is the reference::

    Agent  Initial   R1   Utility R2   R2   Utility R3   R3    Result
    ER       135     85      148      101      171      118    WIN
    OT       117     75      112      105       94    withdraw lose
    Ward      86     55       76   withdraw      -        -    lose

Every one of those six bids is reproduced from the heuristic's four rules — none is tabulated.

**Why this test uses RL-Steps' budget scale, not the derived one.** Section 18's bids belong
to RL-Steps' utility scale (135/117/86); the derived budgets in the budget pool are
denominated in Appendix C's computed scale (107.1/34.2/45.7). Mixing them would have OT trying
to bid 105 against a shift budget of 16.7 — the affordability guard would clamp it and the
worked example would not reproduce, for a reason that has nothing to do with the auction.
Section 19's own figures (ER 740, OT 610, Ward 480) are used instead. The coupling itself is
asserted in ``test_budget_and_utility_must_share_a_scale``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

import pytest

from allocation.auction import (
    AuctionResult,
    ExitReason,
    apply_guards,
    decision_order,
    reserve_price,
    round_budget,
    run_auction,
    safety_violations,
)
from allocation.auction.state import Position
from allocation.budget.spend import contention as compute_contention
from allocation.contracts import (
    Action,
    AgentKind,
    AuctionMode,
    BudgetSource,
    BudgetState,
    ComponentName,
    ComponentResult,
    ReleaseEvent,
    ResourceType,
    TriggerSource,
    UtilityBreakdown,
)
from allocation.ingest.fixtures import ER_CANDIDATE, NOW, OT_CANDIDATE, WARD_CANDIDATE
from allocation.policy import HeuristicPolicy

CANDIDATES = (ER_CANDIDATE, OT_CANDIDATE, WARD_CANDIDATE)

#: Section 18, per round. Ceiling = utility until the B.9 projection exists (D.9).
SECTION_18_UTILITIES: tuple[Mapping[str, float], ...] = (
    {"ER-Patient-A": 135.0, "OT-Patient-B": 117.0, "Ward-Patient-C": 86.0},
    {"ER-Patient-A": 148.0, "OT-Patient-B": 112.0, "Ward-Patient-C": 76.0},
    {"ER-Patient-A": 171.0, "OT-Patient-B": 94.0, "Ward-Patient-C": 76.0},
)

#: Section 19's opening budgets, on RL-Steps' own scale.
SECTION_19_BUDGETS = {AgentKind.ER: 740.0, AgentKind.OT: 610.0, AgentKind.WARD: 480.0}


class Section18Utilities:
    """A :class:`UtilitySource` serving section 18's published utilities.

    Section 18 publishes utilities, not the vitals behind them, so injecting them is the only
    way to test the auction against its own reference. That the utility source is a seam is
    what makes this possible at all.
    """

    def __init__(self, config):
        self._config = config

    def utilities(self, round_index: int) -> Mapping[str, UtilityBreakdown]:
        table = SECTION_18_UTILITIES[min(round_index, len(SECTION_18_UTILITIES) - 1)]
        return {
            candidate.candidate_id: UtilityBreakdown(
                candidate_id=candidate.candidate_id,
                agent=candidate.agent,
                components=(
                    ComponentResult(
                        component=ComponentName.CLINICAL_BENEFIT,
                        cap=60.0,
                        points=table[candidate.candidate_id],
                        coverage=1.0,
                    ),
                ),
                caps_version=self._config.caps_version,
                config_version=self._config.config_version,
                computed_at=NOW,
            )
            for candidate in CANDIDATES
        }

    def ceilings(self, round_index: int) -> Mapping[str, float]:
        return {cid: b.total for cid, b in self.utilities(round_index).items()}


def _budget(config, agent: AgentKind, total: float) -> BudgetState:
    return BudgetState(
        agent=agent,
        shift_id="2026-08-07:evening",
        shift_start=NOW,
        shift_end=NOW,
        base=total,
        demand=1.0,
        criticality=1.0,
        fairness=1.0,
        scarcity=1.0,
        budget_total=total,
        budget_remaining=total,
        source=BudgetSource.SEED,
        caps_version=config.caps_version,
        config_version=config.config_version,
    )


@pytest.fixture
def event() -> ReleaseEvent:
    return ReleaseEvent(
        event_id="evt-1",
        resource_type=ResourceType.ICU_BED,
        resource_id="icu-bed-07",
        predicted_free_at=datetime(2026, 8, 7, 13, 30, tzinfo=timezone.utc),
        detected_at=NOW,
        source=TriggerSource.DISCHARGE_PREDICTION,
        mode=AuctionMode.LIVE,
    )


@pytest.fixture
def outcome(config, profile, snapshot, event):
    budgets = {a: _budget(config, a, total) for a, total in SECTION_19_BUDGETS.items()}
    return run_auction(
        config,
        profile,
        event,
        CANDIDATES,
        Section18Utilities(config),
        HeuristicPolicy(config),
        budgets,
        snapshot,
    )


def _bid(result: AuctionResult, agent: AgentKind, round_index: int):
    return next(
        b for b in result.rounds[round_index].bids if b.agent is agent
    )


# ---------------------------------------------------------------------------------------
# Section 18 — the bid sequence
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "round_index", "action", "amount", "reference"),
    [
        (AgentKind.ER, 0, Action.INCREASE_BID, 85.0, "section 9 — 63% of 135"),
        (AgentKind.OT, 0, Action.INCREASE_BID, 75.0, "section 9 — 64% of 117"),
        (AgentKind.WARD, 0, Action.INCREASE_BID, 55.0, "section 9 — 64% of 86"),
        (AgentKind.ER, 1, Action.INCREASE_BID, 101.0, "section 13 — 85 + 0.25 x 63"),
        (AgentKind.OT, 1, Action.INCREASE_BID, 105.0, "section 14 — 75 + 0.82 x 37"),
        (AgentKind.WARD, 1, Action.WITHDRAW, 55.0, "section 12 — ceiling 76 below the leader"),
        (AgentKind.OT, 2, Action.WITHDRAW, 105.0, "section 16 — standing 105 above ceiling 94"),
        (AgentKind.ER, 2, Action.INCREASE_BID, 118.0, "section 17 — closes at 118"),
    ],
)
def test_section_18_bid_sequence(outcome, agent, round_index, action, amount, reference):
    bid = _bid(outcome.result, agent, round_index)
    assert bid.action is action, reference
    assert bid.amount == pytest.approx(amount), reference


def test_alpha_is_derived_not_tabulated(outcome):
    """Section 14's 0.82 is what overtaking cost, not a constant.

    OT had to clear a leader at 101 from 75 inside a ceiling of 112: (101 - 75 + 4) / 37.
    A policy that tabulated 0.82 would reproduce this example and nothing else.
    """
    assert _bid(outcome.result, AgentKind.OT, 1).alpha == pytest.approx(0.81, abs=0.02)
    assert _bid(outcome.result, AgentKind.ER, 1).alpha == pytest.approx(0.25)
    assert _bid(outcome.result, AgentKind.ER, 2).alpha == pytest.approx(0.25)


def test_er_wins_at_118(outcome):
    result = outcome.result
    assert result.winner is AgentKind.ER
    assert result.winning_bid == pytest.approx(118.0)
    assert result.winning_candidate_id == "ER-Patient-A"
    assert result.outcome == "awarded"


def test_bid_is_not_utility_is_not_ceiling(outcome):
    """The invariant the whole layer exists to preserve.

    "ER finally wins with a bid of 118 even though its utility is 171. It does not need to
    spend all its willingness to pay."
    """
    final = _bid(outcome.result, AgentKind.ER, 2)
    assert final.amount == 118.0
    assert final.utility == 171.0
    assert final.ceiling == 171.0
    assert final.amount < final.ceiling


def test_withdrawal_reasons_are_recorded(outcome):
    positions = outcome.result.positions
    assert positions[AgentKind.WARD].exit_reason == ExitReason.CANNOT_WIN
    assert positions[AgentKind.WARD].exit_round == 1
    assert positions[AgentKind.OT].exit_reason == ExitReason.BID_ABOVE_CEILING
    assert positions[AgentKind.OT].exit_round == 2


# ---------------------------------------------------------------------------------------
# Mechanics
# ---------------------------------------------------------------------------------------


def test_first_round_is_sealed(outcome):
    """Section 10: "Current highest competition unknown".

    If round 1 were open, OT would see ER's 85 and bid to overtake it instead of opening at
    64 % of its own ceiling.
    """
    round_one = outcome.result.rounds[0]
    amounts = {b.agent: b.amount for b in round_one.bids}
    assert amounts == {AgentKind.ER: 85.0, AgentKind.OT: 75.0, AgentKind.WARD: 55.0}


def test_later_rounds_are_open_within_the_round(outcome):
    """Section 14: OT reads ER's NEW 101, not its round-1 85."""
    assert _bid(outcome.result, AgentKind.OT, 1).amount == 105.0


def test_decision_order_is_leader_first(config):
    """Any other ordering produces different numbers — see the module note."""
    positions = {
        AgentKind.WARD: Position(AgentKind.WARD, "w", current_bid=55.0),
        AgentKind.ER: Position(AgentKind.ER, "e", current_bid=85.0),
        AgentKind.OT: Position(AgentKind.OT, "o", current_bid=75.0),
    }
    assert decision_order(positions) == (AgentKind.ER, AgentKind.OT, AgentKind.WARD)


def test_withdrawn_agents_leave_the_order(config):
    positions = {
        AgentKind.ER: Position(AgentKind.ER, "e", current_bid=101.0),
        AgentKind.OT: Position(AgentKind.OT, "o", current_bid=105.0, active=False),
    }
    assert decision_order(positions) == (AgentKind.ER,)


def test_rounds_do_not_stop_early_with_one_bidder(outcome):
    """Section 17 has ER alone in round 3 and still raising, 101 -> 118.

    Ending the auction the moment competition disappears would hand a scarce bed over at the
    opening bid.
    """
    assert outcome.result.rounds_run == 3
    assert outcome.result.rounds[2].active_agents == frozenset({AgentKind.ER})


def test_every_agent_is_recorded_every_round_until_it_exits(outcome):
    """Losers' bids and utilities must be logged, not only the winner's (sections 23-24)."""
    round_one = {b.agent for b in outcome.result.rounds[0].bids}
    assert round_one == {AgentKind.ER, AgentKind.OT, AgentKind.WARD}
    assert {b.agent for b in outcome.result.rounds[1].bids} == round_one
    assert all(b.utility > 0 for r in outcome.result.rounds for b in r.bids)


# ---------------------------------------------------------------------------------------
# Reserve, guards, settlement
# ---------------------------------------------------------------------------------------


def test_reserve_is_scarcity_scaled_and_does_not_bind(config, outcome):
    """Section 17 asserts 110 with no formula. Ours gives ~111 and never binds here."""
    assert outcome.result.reserve_price == pytest.approx(111.0, abs=2.0)
    assert outcome.result.winning_bid > outcome.result.reserve_price


def test_reserve_falls_with_occupancy(config):
    tight = reserve_price(config, highest_ceiling=171.0, occupancy=1.00)
    loose = reserve_price(config, highest_ceiling=171.0, occupancy=0.80)
    assert tight > loose


def test_guards_clamp_to_ceiling(config):
    guarded = apply_guards(config, proposed=200.0, ceiling=171.0, remaining_budget=740.0,
                           contention=1.183)
    assert guarded.amount == 171.0
    assert guarded.clamped_by == "ceiling"


def test_guards_clamp_to_affordability(config):
    """Cost <= Remaining, not Bid <= Remaining — the latter over-restricts by 4x."""
    guarded = apply_guards(config, proposed=171.0, ceiling=171.0, remaining_budget=10.0,
                           contention=1.0)
    assert guarded.amount == pytest.approx(40.0)
    assert "affordability" in guarded.clamped_by


def test_section_19_settlement(config, outcome):
    """ER pays on its winning bid; the losers pay a participation charge on their last bid.

    Section 19: ER 740 -> 704.6 at contention 1.2. Ours computes contention 1.183 from the
    live state, so the cost is 34.9 rather than 35.4.
    """
    spends = outcome.spends
    assert spends[AgentKind.ER].cost == pytest.approx(35.0, abs=0.6)
    assert spends[AgentKind.OT].cost == pytest.approx(3.2, abs=0.2)
    assert spends[AgentKind.WARD].cost == pytest.approx(1.7, abs=0.2)

    assert outcome.budgets[AgentKind.ER].budget_remaining == pytest.approx(705.0, abs=1.0)
    assert outcome.budgets[AgentKind.OT].budget_remaining == pytest.approx(606.8, abs=0.5)
    assert outcome.budgets[AgentKind.WARD].budget_remaining == pytest.approx(478.3, abs=0.5)


def test_losers_pay_on_their_last_standing_bid(outcome):
    """Section 16 says OT's 105 "disappears"; section 19 charges it anyway.

    The exposure is what is priced. Otherwise an agent bids into every contest for free.
    """
    assert outcome.spends[AgentKind.OT].bid == 105.0
    assert outcome.spends[AgentKind.OT].outcome_factor == 0.1
    assert outcome.spends[AgentKind.WARD].bid == 55.0


def test_contention_is_fixed_at_open(outcome, snapshot, config):
    """Priced on the opening field of three, not the one bidder left at close."""
    expected = compute_contention(
        config, 3, snapshot.hospital.occupancy, snapshot.hospital.expected_discharges_4h
    )
    assert outcome.result.contention == pytest.approx(expected)
    assert outcome.result.contention == pytest.approx(1.183, abs=0.001)


# ---------------------------------------------------------------------------------------
# Modes, idempotency, and what is not enforced
# ---------------------------------------------------------------------------------------


def test_simulation_mode_does_not_move_budgets(config, profile, snapshot, event):
    """A hand-fired test auction is scored and logged identically, but binds nothing."""
    from dataclasses import replace

    budgets = {a: _budget(config, a, total) for a, total in SECTION_19_BUDGETS.items()}
    sim = run_auction(
        config, profile, replace(event, mode=AuctionMode.SIMULATION), CANDIDATES,
        Section18Utilities(config), HeuristicPolicy(config), budgets, snapshot,
    )
    assert sim.result.winner is AgentKind.ER
    assert sim.result.winning_bid == 118.0
    assert sim.spends[AgentKind.ER].cost > 0, "still scored"
    assert sim.budgets[AgentKind.ER].budget_remaining == 740.0, "but not charged"


def test_auction_key_is_idempotent(event):
    """A re-firing discharge prediction must not open two auctions on one bed (section 7)."""
    from dataclasses import replace

    later = replace(event, event_id="evt-2", detected_at=NOW)
    assert event.auction_key(15) == later.auction_key(15)

    other_bed = replace(event, resource_id="icu-bed-08")
    assert event.auction_key(15) != other_bed.auction_key(15)


def test_safety_layer_is_provisional_not_signed_off(config):
    """F-13. Rules are enforced, but nobody clinical has approved their content.

    The layer moved from ``undeclared`` (nothing enforced, so a learned policy could not act)
    to ``provisional`` (enforced by engineering judgement). The distinction that must not blur
    is the one asserted last: ``provisional`` is *not* a sign-off, so anything asking "has a
    clinician approved this?" still gets no.
    """
    from allocation.auction.guards import (
        KNOWN_SAFETY_RULES,
        safety_is_declared,
        safety_is_enforced,
        safety_posture,
        safety_rules,
    )

    assert safety_violations(config, ER_CANDIDATE) == ()
    assert safety_posture(config) == "provisional"
    assert config.unsigned["auction.safety_constraints"] == "provisional"

    rules = safety_rules(config)
    assert rules, "provisional means declared; an empty list would be undeclared"
    # Every declared rule must have an evaluator. This is the property that stops a rule from
    # being written down, read as protection, and never enforced.
    for constraint in rules:
        assert constraint["rule"] in KNOWN_SAFETY_RULES
        assert constraint["provenance"] == "provisional_engineering"

    assert safety_is_enforced(config) is True
    assert safety_is_declared(config) is False, "provisional must never read as signed off"


def test_undeclared_rule_is_refused_not_ignored(config):
    """A rule with no evaluator must fail loudly rather than pass as enforced."""
    from dataclasses import replace as _replace

    from allocation.auction.guards import safety_rules

    broken = _replace(
        config,
        auction={
            **config.auction,
            "safety_constraints": [{"id": "x", "rule": "no_such_evaluator"}],
        },
    )
    with pytest.raises(NotImplementedError, match="no evaluator"):
        safety_rules(broken)


@pytest.mark.parametrize("ceiling", [107.4, 107.5, 107.6, 111.9, 156.7, 0.4])
def test_quantisation_never_lifts_a_bid_back_over_its_limit(config, ceiling):
    """The guard has to be the last word, not the second-to-last.

    Bids are whole points. Clamping to a ceiling of 107.6 and *then* rounding gives 108 — the
    guard passes and the invariant still breaks. Ceilings are fractional almost always
    (107.4, 111.9, 156.7), so this was the common case, not an edge one.
    """
    from allocation.auction.guards import apply_guards

    guarded = apply_guards(
        config, proposed=ceiling + 50, ceiling=ceiling,
        remaining_budget=10_000.0, contention=1.18,
    )
    assert guarded.amount <= ceiling
    assert guarded.amount == float(int(guarded.amount)), "bids must stay whole points"


def test_quantisation_never_lifts_a_bid_over_what_the_budget_covers(config):
    """The same failure on the affordability limit, where it silently overspent.

    A bid rounded above ``max_affordable_bid`` produces a cost greater than the remaining
    budget, and ``ledger.settle``'s ``max(0.0, ...)`` floor absorbed it — so the overspend
    was invisible and looked exactly like legitimate exhaustion.
    """
    from allocation.auction.guards import apply_guards
    from allocation.budget.spend import compute_cost, max_affordable_bid

    remaining, contention = 23.0, 1.18
    affordable = max_affordable_bid(config, remaining, contention, won=True)

    guarded = apply_guards(
        config, proposed=9_999.0, ceiling=10_000.0,
        remaining_budget=remaining, contention=contention,
    )
    assert guarded.amount <= affordable
    assert compute_cost(config, guarded.amount, contention, won=True).cost <= remaining


def test_section_17_rounding_still_resolves_to_118(config):
    """The fix must not disturb the worked example: 118.5 against a ceiling of 171."""
    from allocation.auction.guards import apply_guards

    guarded = apply_guards(
        config, proposed=118.5, ceiling=171.0, remaining_budget=10_000.0, contention=1.18
    )
    assert guarded.amount == 118.0


def test_unsigned_rules_are_stamped_on_the_auction(outcome):
    """No run is silently built on assumed clinical values."""
    assert "unit_benefit" in outcome.result.unsigned_rules
    assert outcome.result.caps_version and outcome.result.config_version


# ---------------------------------------------------------------------------------------
# How many rounds run — the cap is derived per auction, not fixed by the profile
# ---------------------------------------------------------------------------------------


class FlatUtilities:
    """A utility source that never moves, so a held bid stays held.

    Section 18's utilities rise and fall by design, which is exactly what makes an auction
    against them never go quiet. Testing the quiescence rule needs the opposite: a world that
    stands still, so "no bid moved" means the policy chose not to move rather than that the
    clinical picture changed underneath it.
    """

    def __init__(self, config, values: Mapping[str, float]):
        self._config = config
        self._values = values

    def utilities(self, round_index: int) -> Mapping[str, UtilityBreakdown]:
        return {
            candidate.candidate_id: UtilityBreakdown(
                candidate_id=candidate.candidate_id,
                agent=candidate.agent,
                components=(
                    ComponentResult(
                        component=ComponentName.CLINICAL_BENEFIT,
                        cap=60.0,
                        points=self._values[candidate.candidate_id],
                        coverage=1.0,
                    ),
                ),
                caps_version=self._config.caps_version,
                config_version=self._config.config_version,
                computed_at=NOW,
            )
            for candidate in CANDIDATES
        }

    def ceilings(self, round_index: int) -> Mapping[str, float]:
        return {cid: b.total for cid, b in self.utilities(round_index).items()}


class OpenThenHold:
    """Bids a fraction of ceiling once, then never moves again.

    Not a plausible bidder — a stub whose only job is to make a round provably quiescent.
    """

    name = "open-then-hold"

    def __init__(self, opening_alpha: float | None):
        self._alpha = opening_alpha

    def decide(self, candidate, utility, ceiling, round_state, budget, snapshot):
        standing = next(
            (b.amount for b in round_state.bids if b.agent is candidate.agent), 0.0
        )
        if standing > 0 or self._alpha is None:
            return Action.HOLD, None
        return Action.INCREASE_BID, self._alpha


def _event_at(minutes_out: float) -> ReleaseEvent:
    from datetime import timedelta

    return ReleaseEvent(
        event_id="evt-lead",
        resource_type=ResourceType.ICU_BED,
        resource_id="icu-bed-07",
        predicted_free_at=NOW + timedelta(minutes=minutes_out),
        detected_at=NOW,
        source=TriggerSource.DISCHARGE_PREDICTION,
        mode=AuctionMode.LIVE,
    )


@pytest.mark.parametrize(
    ("minutes_out", "expected", "bound_by"),
    [
        (30.0, 3, "the profile's own cadence — RL-Steps' scenario, unchanged"),
        (10.0, 3, "still the profile: 10 min buys 5 rounds, the profile allows 3"),
        (4.0, 2, "the bed lands: 4 min at 120s is two rounds"),
        (2.0, 1, "one round fits"),
        (1.5, 1, "floor — a bed 90s out still needs an owner"),
        (0.0, 1, "floor — never zero rounds, whatever the clock says"),
    ],
)
def test_round_budget_is_derived_from_the_release_event(
    profile, minutes_out, expected, bound_by
):
    assert round_budget(profile, _event_at(minutes_out), CANDIDATES) == expected, bound_by


def test_the_tightest_utility_ttl_caps_a_long_auction(profile):
    """ER's utility is good for 10 minutes, so no ICU auction can exceed five rounds.

    Raising the profile's cadence must not buy rounds the clinical data cannot support: past
    the shortest eligible TTL the auction is pricing vitals nobody would act on.
    """
    from allocation.trigger.session import with_rounds

    generous = with_rounds(profile, 20)
    assert round_budget(generous, _event_at(120.0), CANDIDATES) == 5
    assert min(profile.ttl_for(c.agent) for c in CANDIDATES) == 10


def test_a_bed_landing_soon_gets_a_shorter_auction(config, profile, snapshot):
    """End-to-end, not just the arithmetic: the loop honours the budget."""
    budgets = {a: _budget(config, a, total) for a, total in SECTION_19_BUDGETS.items()}
    outcome = run_auction(
        config, profile, _event_at(4.0), CANDIDATES, Section18Utilities(config),
        HeuristicPolicy(config), budgets, snapshot,
    )
    assert outcome.result.max_rounds == 2, "the cap that applied, not the profile's 3"
    assert outcome.result.rounds_run == 2


def test_max_rounds_records_the_cap_that_applied_not_the_profile(outcome):
    """RL-Steps' scenario is unaffected — 30 minutes out leaves room for all three."""
    assert outcome.result.max_rounds == 3
    assert outcome.result.rounds_run == 3


def test_a_quiet_round_above_the_reserve_closes_the_auction(config, profile, snapshot):
    """Going-going-gone. Nobody moved and the leader already clears the reserve."""
    budgets = {a: _budget(config, a, total) for a, total in SECTION_19_BUDGETS.items()}
    flat = FlatUtilities(config, {"ER-Patient-A": 171.0, "OT-Patient-B": 117.0,
                                  "Ward-Patient-C": 86.0})
    outcome = run_auction(
        config, profile, _event_at(30.0), CANDIDATES, flat,
        OpenThenHold(1.0), budgets, snapshot,
    )
    result = outcome.result
    assert result.max_rounds == 3, "three were available"
    assert result.rounds_run == 2, "round 1 bid, round 2 moved nothing — closed"
    assert result.winning_bid == pytest.approx(171.0)
    assert result.winning_bid > result.reserve_price


def test_a_quiet_round_below_the_reserve_does_not_close(config, profile, snapshot):
    """The guard that stops quiescence stranding a bed.

    Utilities are rescored every round, so a stalemate under the reserve may still clear it
    when a ceiling rises. Closing here would record ``not_awarded`` and leave a scarce bed
    unallocated while rounds were still available.
    """
    budgets = {a: _budget(config, a, total) for a, total in SECTION_19_BUDGETS.items()}
    flat = FlatUtilities(config, {"ER-Patient-A": 171.0, "OT-Patient-B": 117.0,
                                  "Ward-Patient-C": 86.0})
    outcome = run_auction(
        config, profile, _event_at(30.0), CANDIDATES, flat,
        OpenThenHold(None), budgets, snapshot,  # nobody ever bids
    )
    assert outcome.result.rounds_run == 3, "ran the full budget rather than closing short"
    assert outcome.result.reserve_price > 0


def test_the_worked_example_is_never_cut_short_by_quiescence(outcome):
    """Section 17 has ER alone and still climbing 101 -> 118 — a lone leader is not quiet."""
    assert outcome.result.rounds_run == 3
    assert _bid(outcome.result, AgentKind.ER, 2).amount == 118.0


def test_budget_and_utility_must_share_a_scale(config, profile, snapshot, event):
    """Budgets are denominated in utility points, so the two move together.

    Running section 18's bids against budgets derived from Appendix C's utilities clamps OT
    hard: a shift budget of 16.7 cannot support a bid of 105. That is not a bug in either
    number — it is why ``caps_version`` is stamped on every budget row, and why re-fitting the
    caps (B.13) re-derives every budget in the system.

    Pinned to ``base.mode: derived``. Under RL-Steps' common Base of 700 the budgets are an
    order of magnitude larger than the utilities and the guard never bites, which is the same
    scale mismatch pointing the other way.
    """
    from dataclasses import replace

    from allocation.budget import derive_base
    from allocation.budget.factors import BudgetFactors
    from allocation.budget.ledger import open_shift
    from allocation.budget.shifts import resolve_shift

    config = replace(
        config,
        budget={**config.budget, "base": {**config.budget["base"], "mode": "derived"}},
    )
    shift = resolve_shift(config, NOW)
    factors = BudgetFactors(1.0, 1.0, 1.0, 1.3, "", "", "", "")
    derived = {
        a: open_shift(config, derive_base(config, a), factors, shift)
        for a in (AgentKind.ER, AgentKind.OT, AgentKind.WARD)
    }
    result = run_auction(
        config, profile, event, CANDIDATES, Section18Utilities(config),
        HeuristicPolicy(config), derived, snapshot,
    ).result

    ot_bid = _bid(result, AgentKind.OT, 1)
    assert ot_bid.amount < 105.0, "the affordability guard bites on the derived scale"
    assert derived[AgentKind.OT].budget_total < 20.0
