"""The safe-pilot mechanisms. Essential 4.

The property under test throughout: **no configuration of the shadow path lets a learned policy
allocate a bed.** Everything else here is secondary, because everything else is a judgement call
about thresholds and this one is a safety invariant.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from allocation.config import load_config
from allocation.contracts import Action, AgentKind, Decision, PathwayPlan, QAction
from allocation.ingest.scenarios import load_scenario
from allocation.policy.heuristic import HeuristicPolicy
from allocation.rl.encoder import StateEncoder
from allocation.rl.pilot import (
    DivergenceMonitor,
    GatedPolicy,
    SafetyGate,
    ShadowPolicy,
)
from allocation.rl.policy import PARAM_COUNT, LinearQPolicy, QWeights
from allocation.trigger.runtime import run_allocation

UTC = timezone.utc
NOW = datetime(2026, 8, 7, 13, 0, tzinfo=UTC)
SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "ward_crash.yaml"


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture
def scenario():
    source, candidates, _ = load_scenario(SCENARIO, NOW)
    return source, candidates


def _learned(config, value: float = 0.5) -> LinearQPolicy:
    return LinearQPolicy(
        config, QWeights.from_flat([value] * PARAM_COUNT, StateEncoder().version)
    )


# ---------------------------------------------------------------------------------------
# Shadow — the safety invariant
# ---------------------------------------------------------------------------------------


def test_shadow_mode_allocates_exactly_as_the_baseline_would(config, scenario):
    """The invariant. Shadowing must not change a single bid.

    If it did, the learned policy would be influencing allocations during the period that
    exists precisely so it cannot.
    """
    source, candidates = scenario

    plain = run_allocation(
        config=config, source=source, candidates=candidates, now=NOW,
        query="ICU bed", read_alternatives=True,
    )
    shadowed = run_allocation(
        config=config, source=source, candidates=candidates, now=NOW, query="ICU bed",
        policy=ShadowPolicy(HeuristicPolicy(config), _learned(config)),
        read_alternatives=True,
    )

    assert shadowed.winner == plain.winner
    assert shadowed.outcome.result.winning_bid == plain.outcome.result.winning_bid

    plain_bids = [(b.agent, b.round_index, b.amount, b.q_action)
                  for r in plain.outcome.result.rounds for b in r.bids]
    shadow_bids = [(b.agent, b.round_index, b.amount, b.q_action)
                   for r in shadowed.outcome.result.rounds for b in r.bids]
    assert shadow_bids == plain_bids


def test_shadow_mode_still_records_what_the_learned_policy_wanted(config, scenario):
    """A shadow run that logged nothing would be safe and useless."""
    source, candidates = scenario
    shadow = ShadowPolicy(HeuristicPolicy(config), _learned(config))

    run_allocation(
        config=config, source=source, candidates=candidates, now=NOW,
        query="ICU bed", policy=shadow, read_alternatives=True,
    )
    assert shadow.monitor._total > 0
    assert "decisions observed" in shadow.monitor.report()


def test_divergence_is_measured_against_the_same_state(config, scenario):
    """Paired by construction: both policies are asked about one round view.

    An unpaired log of a learned policy running alone answers nothing, because the states it
    visits are its own.
    """
    source, candidates = scenario
    aggressive = ShadowPolicy(HeuristicPolicy(config), _learned(config, 0.9))
    passive = ShadowPolicy(HeuristicPolicy(config), _learned(config, -0.9))

    for shadow in (aggressive, passive):
        run_allocation(
            config=config, source=source, candidates=candidates, now=NOW,
            query="ICU bed", policy=shadow, read_alternatives=True,
        )
    assert aggressive.monitor._total == passive.monitor._total


# ---------------------------------------------------------------------------------------
# The circuit breaker
# ---------------------------------------------------------------------------------------


def test_the_breaker_needs_a_minimum_sample_before_it_can_trip():
    """Three disagreements are not evidence of drift."""
    monitor = DivergenceMonitor(threshold=0.1)
    for _ in range(3):
        monitor.record(_win_now(), _abandon())
    assert monitor.recent_rate == 1.0
    assert not monitor.tripped


def test_the_breaker_trips_on_sustained_divergence():
    monitor = DivergenceMonitor(threshold=0.35)
    for _ in range(60):
        monitor.record(_win_now(), _abandon())
    assert monitor.tripped
    assert "TRIPPED" in monitor.report()


def test_agreement_keeps_the_breaker_closed():
    monitor = DivergenceMonitor(threshold=0.35)
    for _ in range(60):
        monitor.record(_win_now(), _win_now())
    assert monitor.rate == 0.0
    assert not monitor.tripped


def test_the_breaker_reads_the_trailing_window_not_all_history():
    """A cumulative rate would not notice a policy that started diverging today."""
    monitor = DivergenceMonitor(threshold=0.35, window=40)
    for _ in range(200):
        monitor.record(_win_now(), _win_now())
    for _ in range(40):
        monitor.record(_win_now(), _abandon())
    assert monitor.rate < 0.35
    assert monitor.recent_rate == 1.0
    assert monitor.tripped


# ---------------------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------------------


def test_a_critical_patient_may_not_be_abandoned(config, scenario):
    """The one action with no onward plan, applied to the patient who least tolerates it."""
    source, candidates = scenario
    gate = SafetyGate(config, critical_news2=7.0)

    verdict = gate.check(
        _abandon(), candidates[0], _utility(), 150.0, pathways=None, news2=9.0
    )
    assert not verdict.allowed
    assert "may not be abandoned" in verdict.rule
    assert verdict.substituted is not None
    assert not verdict.substituted.q_action.exits, "the substitute must keep the patient in play"


def test_a_stable_patient_may_be_abandoned(config, scenario):
    """The gate is a floor, not a ban — abandonment is sometimes the only true description."""
    source, candidates = scenario
    gate = SafetyGate(config, critical_news2=7.0)
    assert gate.check(
        _abandon(), candidates[0], _utility(), 150.0, pathways=None, news2=2.0
    ).allowed


def test_an_unknown_news2_does_not_silently_pass_the_gate_as_safe(config, scenario):
    """Absent is absent.

    The gate cannot fire without a reading, which is a real limitation and is stated rather
    than papered over: a pilot must not run against patients whose NEWS2 cannot be computed,
    because the constraint that protects them is unenforceable there.
    """
    source, candidates = scenario
    gate = SafetyGate(config, critical_news2=7.0)
    verdict = gate.check(_abandon(), candidates[0], _utility(), 150.0, None, news2=None)
    assert verdict.allowed, "documented gap: no reading means no gate"


def test_gates_sit_outside_the_policy_not_inside_it(config):
    """``policy/__init__.py``: a constraint inside a policy is one a learned policy can be
    trained to violate. The learned policy must remain unaware it was overruled."""
    learned = _learned(config)
    gated = GatedPolicy(learned, SafetyGate(config), fallback=HeuristicPolicy(config))
    assert gated._learned is learned
    assert not hasattr(learned, "_gate")


def test_a_blocked_decision_falls_back_and_is_recorded(config, scenario):
    source, candidates = scenario

    class AlwaysAbandons:
        name = "abandoner"

        def decide_q(self, *args, **kwargs):
            return _abandon()

        def decide(self, *args, **kwargs):
            return Action.WITHDRAW, None

    gate = SafetyGate(config, critical_news2=0.0)  # every patient counts as critical
    gated = GatedPolicy(AlwaysAbandons(), gate, fallback=HeuristicPolicy(config))
    run = run_allocation(
        config=config, source=source, candidates=candidates, now=NOW,
        query="ICU bed", policy=gated, read_alternatives=True,
    )
    assert gated.blocked, "the gate should have refused at least one abandonment"
    assert run.winner is not None, "the fallback should still have run a real auction"


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------


def _win_now() -> Decision:
    return Decision.compete(QAction.WIN_NOW, Action.INCREASE_BID, 0.6)


def _abandon() -> Decision:
    return Decision(q_action=QAction.WITHDRAW_UNPLANNED, action=Action.WITHDRAW)


def _utility():
    from allocation.contracts import UtilityBreakdown

    return UtilityBreakdown(
        candidate_id="c", agent=AgentKind.ER, components=(),
        caps_version="x", config_version="y", computed_at=NOW,
    )


# ---------------------------------------------------------------------------------------
# Constraints declared in auction.yaml, not hard-coded
# ---------------------------------------------------------------------------------------


def test_gate_reads_its_threshold_from_config(config):
    """The NEWS2 line lives in auction.yaml where a clinical review will find it.

    It was previously a literal 7.0 on the dataclass — a clinical threshold buried in code,
    which is exactly where nobody with standing to change it would ever look.
    """
    gate = SafetyGate(config)
    assert gate.news2_limit == 7.0
    assert gate.forbids_avoidable_abandonment is True


def test_avoidable_abandonment_is_refused(config, scenario):
    """The defect the first trained policy showed six times, now structurally refused.

    No threshold in this rule, so no clinical judgement is embedded: if an exit that arranges
    something was feasible, the one that arranges nothing is not allowed.
    """
    decision = Decision(
        q_action=QAction.WITHDRAW_UNPLANNED,
        action=Action.WITHDRAW,
        feasible=frozenset({QAction.WITHDRAW_UNPLANNED, QAction.RE_ENTER_LATER}),
    )
    verdict = SafetyGate(config).check(
        decision, scenario[1][0], _utility(), 100.0, None, news2=2.0
    )
    assert verdict.allowed is False
    assert "planned exit was available" in verdict.rule
    assert verdict.substituted is not None
    assert verdict.substituted.q_action is not QAction.WITHDRAW_UNPLANNED


def test_unavoidable_abandonment_is_allowed_when_not_critical(config, scenario):
    """A patient with nothing arrangeable and no critical score is not a rule breach.

    The action space has to be able to say "abandoned" — contracts.py keeps
    WITHDRAW_UNPLANNED as a sixth action precisely so that a rising count reports rationing
    past what the alternatives absorb. Refusing it unconditionally would hide that.
    """
    decision = Decision(
        q_action=QAction.WITHDRAW_UNPLANNED,
        action=Action.WITHDRAW,
        feasible=frozenset({QAction.WITHDRAW_UNPLANNED}),
    )
    verdict = SafetyGate(config).check(
        decision, scenario[1][0], _utility(), 100.0, None, news2=2.0
    )
    assert verdict.allowed is True


def test_critical_patient_refused_even_when_unavoidable(config, scenario):
    """NEWS2 above the line overrides "nothing else was feasible"."""
    decision = Decision(
        q_action=QAction.WITHDRAW_UNPLANNED,
        action=Action.WITHDRAW,
        feasible=frozenset({QAction.WITHDRAW_UNPLANNED}),
    )
    verdict = SafetyGate(config).check(
        decision, scenario[1][0], _utility(), 100.0, None, news2=8.0
    )
    assert verdict.allowed is False
    assert "NEWS2 8 >= 7" in verdict.rule
