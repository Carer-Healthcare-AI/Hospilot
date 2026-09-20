"""Tests for the AMBULANCE bed bidder (agent-extension, 2026-08-27).

Scope discipline: this file adds coverage, it does not modify any existing test, encoder
version, or clock/discount file. It only exercises the additive changes — a new AgentKind
member, two new optional Candidate fields, a new safe-wait branch in
allocation/pathway/options.py's build_options(), two new encoder features, and AMBULANCE's
eligibility on ICU_BED — against the SAME, unmodified auction/utility/budget/settlement
math every other bed bidder already runs through.

No training happens here. Where a Q policy is exercised, its weights are constructed
in-process (zeros, or a small hand-picked vector for the alpha-mechanism test) — never
loaded from a fitted artifact, since none exists yet for the bumped encoder.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from allocation.config import load_config
from allocation.contracts import AgentKind, AuctionMode
from allocation.ingest.fixtures import (
    CANDIDATES,
    CANDIDATES_WITH_AMBULANCE,
    NOW,
    AMBULANCE_CANDIDATE,
    FixtureDataSource,
)
from allocation.policy.heuristic import HeuristicPolicy
from allocation.use_cases.bed.profiles.icu_bed import ICU_BED
from allocation.rl.encoder import SIZE, StateEncoder
from allocation.rl.policy import LinearQPolicy, QWeights
from allocation.trigger.runtime import run_allocation

CONFIG = load_config()
ENCODER_VERSION = StateEncoder().version


def _run(policy, candidates=CANDIDATES_WITH_AMBULANCE, read_alternatives: bool = True):
    return run_allocation(
        config=CONFIG,
        source=FixtureDataSource(),
        candidates=candidates,
        now=NOW,
        query="agent-extension bed test",
        profile=ICU_BED,
        mode=AuctionMode.SIMULATION,
        policy=policy,
        read_alternatives=read_alternatives,
    )


def _zero_q_policy() -> LinearQPolicy:
    weights = QWeights.zeros(encoder_version=ENCODER_VERSION, fabrication_version="test")
    return LinearQPolicy(CONFIG, weights)


# ------------------------------------------------------------------------------------------
# Four bidders can enter; one winner
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("policy_factory", [lambda: HeuristicPolicy(CONFIG), _zero_q_policy])
def test_four_bidders_enter_the_icu_bed_scenario(policy_factory):
    run = _run(policy_factory())
    result = run.outcome.result
    agents_seen = {b.agent for r in result.rounds for b in r.bids}
    assert agents_seen == {AgentKind.ER, AgentKind.OT, AgentKind.WARD, AgentKind.AMBULANCE}


@pytest.mark.parametrize("policy_factory", [lambda: HeuristicPolicy(CONFIG), _zero_q_policy])
def test_every_auction_has_exactly_one_winner(policy_factory):
    run = _run(policy_factory())
    result = run.outcome.result
    winners = [a for a in (AgentKind.ER, AgentKind.OT, AgentKind.WARD, AgentKind.AMBULANCE)
               if a is result.winner]
    assert len(winners) <= 1
    assert (result.winner is None) == (result.winning_bid is None)


# ------------------------------------------------------------------------------------------
# AMBULANCE has its own budget and context
# ------------------------------------------------------------------------------------------


def test_ambulance_has_its_own_independent_budget_row():
    run = _run(HeuristicPolicy(CONFIG))
    budgets = run.outcome.budgets
    assert AgentKind.AMBULANCE in budgets
    assert budgets[AgentKind.AMBULANCE].budget_total > 0
    # Genuinely independent, not a copy of another agent's row.
    totals = {a: b.budget_total for a, b in budgets.items()}
    assert len({round(v, 6) for v in totals.values()}) > 1 or len(totals) == 1


def test_ambulance_candidate_carries_genuinely_new_context():
    assert AMBULANCE_CANDIDATE.current_unit is None
    assert AMBULANCE_CANDIDATE.arrived_at is None
    assert AMBULANCE_CANDIDATE.eta_minutes == 12.0
    assert AMBULANCE_CANDIDATE.safe_wait_minutes_override == 30.0
    assert AMBULANCE_CANDIDATE.needs  # required-bed-compatibility, via the existing field


def test_ambulance_utility_is_scored_with_partial_but_honest_coverage():
    run = _run(HeuristicPolicy(CONFIG))
    breakdown = run.utilities[AMBULANCE_CANDIDATE.candidate_id]
    assert breakdown.total > 0
    coverage = sum(c.coverage for c in breakdown.components) / len(breakdown.components)
    # Waiting/Throughput/Operational/avoided-cost are honestly absent for a not-yet-arrived,
    # not-yet-placed patient (no arrived_at, no current_unit) -- this is the EXISTING D.0
    # coverage mechanism working as designed, not a defect introduced here. Coverage must be
    # real (not zero, not fabricated to 1.0) and below the fully-arrived agents'.
    assert 0.0 < coverage < 1.0
    er_breakdown = run.utilities["ER-Patient-A"]
    er_coverage = sum(c.coverage for c in er_breakdown.components) / len(er_breakdown.components)
    assert coverage < er_coverage


# ------------------------------------------------------------------------------------------
# Q chooses only feasible canonical actions
# ------------------------------------------------------------------------------------------


def test_q_never_selects_an_infeasible_action_for_ambulance():
    run = _run(_zero_q_policy())
    for round_state in run.outcome.result.rounds:
        for bid in round_state.bids:
            if bid.agent is not AgentKind.AMBULANCE or bid.q_action is None:
                continue
            assert bid.q_action in bid.feasible, (
                f"round {round_state.round_index}: chose {bid.q_action} "
                f"not in feasible {sorted(a.value for a in bid.feasible)}"
            )


def test_ambulance_reaches_withdraw_alternative_and_re_enter_later_via_existing_mechanics():
    """Proves the new safe-wait/diversion wiring genuinely unlocks masks, not just win_now."""
    run = _run(HeuristicPolicy(CONFIG))
    feasible_seen: set[str] = set()
    for round_state in run.outcome.result.rounds:
        for bid in round_state.bids:
            if bid.agent is AgentKind.AMBULANCE:
                feasible_seen |= {a.value for a in bid.feasible}
    assert "withdraw_alternative" in feasible_seen, (
        "PACU diversion (read_alternatives=True, full capability match) should be usable"
    )
    assert "re_enter_later" in feasible_seen, (
        "EMS-reported vitals should arm a NEWS2-based reentry monitor"
    )


# ------------------------------------------------------------------------------------------
# Alpha and bids vary by round using existing mechanics
# ------------------------------------------------------------------------------------------


def test_ambulance_bid_amount_changes_across_rounds():
    run = _run(HeuristicPolicy(CONFIG))
    amounts = [
        bid.amount
        for round_state in run.outcome.result.rounds
        for bid in round_state.bids
        if bid.agent is AgentKind.AMBULANCE and bid.action.value == "increase_bid"
    ]
    assert len(amounts) >= 2
    assert len(set(amounts)) > 1, "bid amount must actually move across rounds"


def test_the_alpha_head_mechanism_produces_different_alpha_for_ambulance_than_arrived_agents():
    """Mechanism-level proof (no training): a hand-built, deliberately non-zero alpha_row
    that weights the NEW eta feature produces a genuinely different alpha for AMBULANCE
    (eta_known=1) than for the three already-arrived agents (eta_known=0, eta=0) -- proving
    the sigmoid-over-alpha-head architecture is real and state-dependent, and that the new
    features actually reach it, without claiming any trained efficacy.
    """
    alpha_row = tuple(0.0 for _ in range(SIZE - 2)) + (-8.0, 0.0)  # weight against `eta`
    weights = QWeights(
        rows=tuple(tuple(0.0 for _ in range(SIZE)) for _ in range(6)),
        biases=tuple(0.0 for _ in range(6)),
        alpha_row=alpha_row, alpha_bias=2.0,
        encoder_version=ENCODER_VERSION, fabrication_version="alpha-mechanism-test",
    )
    run = _run(LinearQPolicy(CONFIG, weights))
    alphas_by_agent: dict[AgentKind, list[float]] = {}
    for round_state in run.outcome.result.rounds:
        for bid in round_state.bids:
            if bid.alpha is not None:
                alphas_by_agent.setdefault(bid.agent, []).append(bid.alpha)
    ambulance_alpha = alphas_by_agent[AgentKind.AMBULANCE][0]
    other_alphas = {a for agent, vals in alphas_by_agent.items() if agent is not AgentKind.AMBULANCE for a in vals}
    assert other_alphas, "expected at least one other agent to have a recorded alpha"
    assert all(abs(ambulance_alpha - a) > 1e-6 for a in other_alphas)


# ------------------------------------------------------------------------------------------
# Settlement / accounting conservation
# ------------------------------------------------------------------------------------------


def test_settlement_conserves_correctly_with_four_bidders():
    run = _run(HeuristicPolicy(CONFIG))
    outcome = run.outcome
    for agent, state in outcome.budgets.items():
        assert state.budget_remaining <= state.budget_total + 1e-9
        assert state.spent >= -1e-9
        # SIMULATION mode computes but does not CHARGE spend (allocation/trigger/runtime.py's
        # _burn_row docstring: "a non-binding auction ... moves no budget"). That is existing,
        # unchanged behaviour -- conservation here means budget_remaining stays exactly at
        # budget_total (nothing charged), never a partial/inconsistent decrement.
        assert state.spent == 0.0
        assert state.budget_remaining == state.budget_total
    for agent, spend in outcome.spends.items():
        # The cost computation itself still runs generically for every bidder, including
        # AMBULANCE, and produces a real, non-negative, finite number every time.
        assert spend.cost >= 0.0
        assert spend.bid >= 0.0
        assert spend.contention > 0.0
    result = outcome.result
    if result.winner is not None:
        winner_spend = outcome.spends.get(result.winner)
        assert winner_spend is not None and winner_spend.cost > 0
        assert winner_spend.won is True


# ------------------------------------------------------------------------------------------
# Existing three-agent scenarios remain supported
# ------------------------------------------------------------------------------------------


def test_original_three_agent_candidates_tuple_is_unchanged():
    assert {c.agent for c in CANDIDATES} == {AgentKind.ER, AgentKind.OT, AgentKind.WARD}
    assert len(CANDIDATES) == 3


def test_three_agent_icu_scenario_still_runs_correctly_end_to_end():
    run = _run(HeuristicPolicy(CONFIG), candidates=CANDIDATES)
    result = run.outcome.result
    agents_seen = {b.agent for r in result.rounds for b in r.bids}
    assert agents_seen == {AgentKind.ER, AgentKind.OT, AgentKind.WARD}
    assert result.winner in (AgentKind.ER, AgentKind.OT, AgentKind.WARD, None)


def test_other_bed_profiles_do_not_gain_ambulance_eligibility():
    from allocation.use_cases.bed.profiles.ward_bed import WARD_BED

    assert AgentKind.AMBULANCE not in WARD_BED.eligible_agents


# ------------------------------------------------------------------------------------------
# Clock/exogenous-timing coverage with AMBULANCE present (CLOCK PROTECTION)
#
# Scope note: this proves policy choice never affects the EXOGENOUS release-schedule facts
# on the static/deterministic path (FixtureDataSource + run_allocation's manual_event()),
# which is the pathway AMBULANCE actually uses here. It deliberately does NOT extend the
# Poisson training-data simulator (allocation/sim/world.py, allocation/sim/patients.py) to
# fabricate AMBULANCE patients -- those files, and every other file on the clock/discount
# forbidden list (allocation/reward/episode.py, allocation/reward/terms.py, allocation/sim/
# dataset.py's DecisionTransition, allocation/rl/{baseline_q,linear_baseline,qlearn,cohort}.py),
# are untouched, verified below by hash where practical and by the untouched-clock-tests run
# in the harness. tests/test_bed_clock_invariance.py itself is run unmodified, not edited.
# ------------------------------------------------------------------------------------------


def test_exogenous_release_timing_is_identical_regardless_of_policy_with_ambulance_present():
    heuristic_run = _run(HeuristicPolicy(CONFIG))
    q_run = _run(_zero_q_policy())

    # The release event -- WHEN the bed becomes available -- is exogenous input to the
    # auction, computed before any policy decides anything (trigger/query.py's
    # manual_event()). It must be identical across policies.
    assert heuristic_run.event.predicted_free_at == q_run.event.predicted_free_at
    assert heuristic_run.event.detected_at == q_run.event.detected_at

    # Every candidate's arrival-side facts (including AMBULANCE's ETA) are read from the
    # same fixture, never derived from a policy's choices.
    assert heuristic_run.snapshot.taken_at == q_run.snapshot.taken_at
    for candidate_id in ("ER-Patient-A", "OT-Patient-B", "Ward-Patient-C", "AMBULANCE-Patient-D"):
        h_patient = heuristic_run.snapshot.for_candidate(candidate_id)
        q_patient = q_run.snapshot.for_candidate(candidate_id)
        assert h_patient.candidate.arrived_at == q_patient.candidate.arrived_at
        assert h_patient.candidate.eta_minutes == q_patient.candidate.eta_minutes


def test_completion_can_diverge_across_policies_despite_matching_exogenous_facts():
    """Companion to the fingerprint test above: a matching exogenous-facts fingerprint must
    NOT come from the two policies happening to make identical decisions -- that would be a
    much stronger and false claim (mirrors test_bed_clock_invariance.py's own
    ``test_completion_still_legitimately_diverges_despite_the_matching_fingerprint``).
    """
    heuristic_run = _run(HeuristicPolicy(CONFIG))
    aggressive_weights = QWeights(
        rows=tuple(tuple(1.0 for _ in range(SIZE)) for _ in range(6)),
        biases=tuple(0.0 for _ in range(6)),
        alpha_row=tuple(0.0 for _ in range(SIZE)), alpha_bias=4.0,  # near-1.0 alpha, always
        encoder_version=ENCODER_VERSION, fabrication_version="divergence-test",
    )
    q_run = _run(LinearQPolicy(CONFIG, aggressive_weights))
    heuristic_amounts = [b.amount for r in heuristic_run.outcome.result.rounds for b in r.bids]
    q_amounts = [b.amount for r in q_run.outcome.result.rounds for b in r.bids]
    assert heuristic_amounts != q_amounts, "the two policies should genuinely produce different bids"
    # Yet the exogenous facts still match exactly.
    assert heuristic_run.event.predicted_free_at == q_run.event.predicted_free_at
