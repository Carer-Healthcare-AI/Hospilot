"""The six-action decision space. RL-Steps' closing table, plus the exit it omits.

What these tests are actually protecting, stated once so the individual cases read as
consequences rather than as arbitrary assertions:

``config/reward.yaml`` pays ``safely_held`` (+10) and ``second_bed_opened`` (+15) for outcomes
that only the strategic exits produce. Before the Q-space every withdrawal was one
undifferentiated ``Action.WITHDRAW``, so those points attached to whichever agent happened to
have bid, for a hand-off no policy ever chose. The invariants below are the ones that keep the
reward attributable — most of them are enforced by construction in
:class:`~allocation.contracts.Decision`, and each test names the failure it prevents.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from allocation.auction.state import ExitReason
from allocation.config import load_config
from allocation.contracts import (
    Action,
    AgentKind,
    AlternativeOption,
    CareNeed,
    Candidate,
    Decision,
    PathwayPlan,
    QAction,
    ReentryTrigger,
    ResourceType,
)
from allocation.ingest.scenarios import load_scenario
from allocation.pathway.forecast import next_release
from allocation.pathway.options import build_options, safe_wait_minutes
from allocation.pathway.participation import ParticipationLedger, Standing
from allocation.pathway.reentry import ReentryRegistry
from allocation.trigger.runtime import run_allocation
from allocation.trigger.session import event_schedule, run_session

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


def _trigger(now: datetime = NOW, **kwargs) -> ReentryTrigger:
    body = dict(
        candidate_id="c1",
        agent=AgentKind.WARD,
        resource_type=ResourceType.ICU_BED,
        armed_at=now,
        expires_at=now + timedelta(hours=4),
        news2_rise=2.0,
        baseline_news2=5.0,
    )
    body.update(kwargs)
    return ReentryTrigger(**body)


# ---------------------------------------------------------------------------------------
# The action space itself
# ---------------------------------------------------------------------------------------


def test_six_actions_exist_and_four_of_them_exit():
    assert len(QAction) == 6
    exits = {a for a in QAction if a.exits}
    assert exits == {
        QAction.WITHDRAW_ALTERNATIVE,
        QAction.AWAIT_NEXT_RESOURCE,
        QAction.RE_ENTER_LATER,
        QAction.WITHDRAW_UNPLANNED,
    }


def test_only_arranged_exits_may_claim_to_have_arranged_care():
    """``arranges_care`` is the predicate the reward layer needs.

    Without it there is no way to stop ``safely_held``'s +10 attaching to an abandonment,
    which is the defect the whole action space was added to fix.
    """
    assert QAction.WITHDRAW_ALTERNATIVE.arranges_care
    assert QAction.AWAIT_NEXT_RESOURCE.arranges_care
    assert QAction.RE_ENTER_LATER.arranges_care
    assert not QAction.WITHDRAW_UNPLANNED.arranges_care
    assert not QAction.WIN_NOW.arranges_care  # it did not exit at all


# ---------------------------------------------------------------------------------------
# Decision invariants — the '+' in "Withdraw + Alternative", enforced by the type
# ---------------------------------------------------------------------------------------


def test_alternative_exit_without_a_named_unit_is_refused():
    with pytest.raises(ValueError, match="must name the alternative unit"):
        Decision(
            q_action=QAction.WITHDRAW_ALTERNATIVE,
            action=Action.WITHDRAW,
            plan=PathwayPlan(safe_hold_minutes=120),
        )


def test_await_exit_without_a_forecast_is_refused():
    with pytest.raises(ValueError, match="expected release"):
        Decision(
            q_action=QAction.AWAIT_NEXT_RESOURCE,
            action=Action.WITHDRAW,
            plan=PathwayPlan(note="a bed will turn up"),
        )


def test_reenter_exit_without_a_trigger_is_refused():
    """A "temporary" exit that nothing watches is a permanent one."""
    with pytest.raises(ValueError, match="ReentryTrigger"):
        Decision(
            q_action=QAction.RE_ENTER_LATER,
            action=Action.WITHDRAW,
            plan=PathwayPlan(target_unit="hdu"),
        )


def test_strategic_exit_with_no_plan_at_all_is_refused():
    with pytest.raises(ValueError, match="WITHDRAW_UNPLANNED"):
        Decision(q_action=QAction.WITHDRAW_ALTERNATIVE, action=Action.WITHDRAW)


def test_unplanned_exit_must_not_carry_a_plan():
    """The inverse guard. An abandonment that carries a plan under-reports what happened."""
    with pytest.raises(ValueError, match="arranged nothing"):
        Decision(
            q_action=QAction.WITHDRAW_UNPLANNED,
            action=Action.WITHDRAW,
            plan=PathwayPlan(target_unit="hdu"),
        )


def test_bid_mechanic_must_follow_the_decision():
    with pytest.raises(ValueError, match="leaves the auction"):
        Decision(q_action=QAction.WITHDRAW_UNPLANNED, action=Action.INCREASE_BID)
    with pytest.raises(ValueError, match="stays in the auction but emits WITHDRAW"):
        Decision(q_action=QAction.WIN_NOW, action=Action.WITHDRAW)


def test_a_competing_decision_may_not_carry_a_commitment():
    with pytest.raises(ValueError, match="must not carry"):
        Decision(
            q_action=QAction.CONTINUE,
            action=Action.INCREASE_BID,
            alpha=0.3,
            plan=PathwayPlan(target_unit="hdu"),
        )


def test_a_trigger_with_no_condition_never_fires_and_is_refused():
    with pytest.raises(ValueError, match="never fires"):
        _trigger(news2_rise=None, on_alternative_lost=False)


def test_a_trigger_must_expire_after_it_is_armed():
    with pytest.raises(ValueError, match="expire after"):
        _trigger(expires_at=NOW - timedelta(minutes=1))


# ---------------------------------------------------------------------------------------
# Alternatives — unknown is not open
# ---------------------------------------------------------------------------------------


def test_an_unread_alternative_is_not_usable():
    """The failure mode of the other convention is a patient withdrawn into a full HDU."""
    option = AlternativeOption(unit="hdu", safe_hold_minutes=168.0, available=None)
    assert option.available is None
    assert not option.usable


def test_a_capability_gap_makes_an_open_unit_unusable():
    option = AlternativeOption(
        unit="hdu",
        safe_hold_minutes=168.0,
        available=True,
        capability_gap=frozenset({CareNeed.VENTILATION}),
    )
    assert not option.usable


def test_alternatives_are_unusable_until_someone_reads_the_occupancy(config, scenario):
    """Without a unit reader the exit stays infeasible rather than being taken on faith."""
    source, candidates = scenario
    run = run_allocation(
        config=config, source=source, candidates=candidates, now=NOW,
        query="ICU bed", read_alternatives=False,
    )
    options = build_options(
        config, candidates[0], run.snapshot,
        target_unit="icu", horizon_hours=4.0,
    )
    assert all(o.available is None for o in options.alternatives)
    assert options.best_alternative is None


# ---------------------------------------------------------------------------------------
# Next-release forecast — derived, and honest about it
# ---------------------------------------------------------------------------------------


def test_absent_discharge_forecast_yields_no_release_estimate(config, scenario):
    """Absent is absent: a missing forecast is not a hospital with no discharges."""
    from dataclasses import replace

    source, candidates = scenario
    run = run_allocation(
        config=config, source=source, candidates=candidates, now=NOW, query="ICU bed"
    )
    hospital = replace(run.snapshot.hospital, expected_discharges_4h=None)
    estimate = next_release(config, hospital, NOW, window_minutes=120)
    assert not estimate.known
    assert estimate.expected_at is None
    assert "absent" in estimate.basis


def test_release_probability_rises_with_the_waiting_window(config, scenario):
    source, candidates = scenario
    run = run_allocation(
        config=config, source=source, candidates=candidates, now=NOW, query="ICU bed"
    )
    short = next_release(config, run.snapshot.hospital, NOW, window_minutes=30)
    long = next_release(config, run.snapshot.hospital, NOW, window_minutes=240)
    assert short.probability < long.probability
    assert 0.0 <= short.probability <= 1.0
    assert 0.0 <= long.probability <= 1.0
    # 1 discharge per 4 h is a rate of 0.25/h, so a 4 h window is 1 - e^-1.
    assert long.probability == pytest.approx(0.6321, abs=1e-3)


def test_the_release_estimate_declares_that_it_is_derived(config, scenario):
    """It must never be mistaken in the log for a discharge-timing model, because none exists."""
    source, candidates = scenario
    run = run_allocation(
        config=config, source=source, candidates=candidates, now=NOW, query="ICU bed"
    )
    estimate = next_release(config, run.snapshot.hospital, NOW, window_minutes=120)
    assert "DERIVED, not forecast" in estimate.basis


def test_safe_wait_is_none_for_a_patient_whose_unit_is_unrecorded(config):
    """Waiting is a claim about safety, and must not be available to a patient nobody vouches for."""
    anonymous = Candidate(
        candidate_id="x", patient_token="t", agent=AgentKind.WARD, current_unit=None
    )
    assert safe_wait_minutes(config, anonymous, horizon_hours=4.0) is None


# ---------------------------------------------------------------------------------------
# Re-entry registry
# ---------------------------------------------------------------------------------------


def test_a_monitor_fires_when_news2_rises_past_its_threshold(config):
    registry = ReentryRegistry(config)
    registry.arm(_trigger())
    fired = registry.due(NOW + timedelta(minutes=30), news2=lambda _: 7.5)
    assert [t.candidate_id for t in fired] == ["c1"]
    assert "c1" not in registry.armed  # it has done its job


def test_a_monitor_does_not_fire_on_an_unreadable_news2(config):
    """"We cannot see the patient" is not "the patient deteriorated"."""
    registry = ReentryRegistry(config)
    registry.arm(_trigger())
    assert registry.due(NOW + timedelta(minutes=30), news2=lambda _: None) == ()
    assert "c1" in registry.armed


def test_an_expired_monitor_is_retired_and_reported(config):
    """A patient parked under a lapsed monitor is one the system quietly stopped tracking."""
    registry = ReentryRegistry(config)
    registry.arm(_trigger())
    checks = registry.check(NOW + timedelta(hours=5), news2=lambda _: 99.0)
    assert len(checks) == 1
    assert not checks[0].fired
    assert "expired" in checks[0].reason
    assert registry.lapsed and registry.lapsed[0].candidate_id == "c1"


def test_expiry_is_tested_before_the_condition(config):
    """A lapsed monitor must not fire on physiology it was never scoped to."""
    registry = ReentryRegistry(config)
    registry.arm(_trigger())
    assert registry.due(NOW + timedelta(hours=5), news2=lambda _: 99.0) == ()


def test_a_monitor_fires_when_the_holding_unit_fills(config):
    registry = ReentryRegistry(config)
    registry.arm(_trigger(on_alternative_lost=True, holding_unit="hdu"))
    fired = registry.due(NOW + timedelta(minutes=30), available=lambda _: False)
    assert len(fired) == 1


def test_re_arming_replaces_rather_than_accumulates(config):
    """Two live triggers for one patient would re-enter them into one auction twice."""
    registry = ReentryRegistry(config)
    registry.arm(_trigger())
    registry.arm(_trigger(baseline_news2=9.0))
    assert len(registry.armed) == 1
    assert registry.armed["c1"].baseline_news2 == 9.0


# ---------------------------------------------------------------------------------------
# The policy chooses among six
# ---------------------------------------------------------------------------------------


def test_every_bid_records_the_decision_behind_it(config, scenario):
    source, candidates = scenario
    run = run_allocation(
        config=config, source=source, candidates=candidates, now=NOW,
        query="ICU bed", read_alternatives=True,
    )
    bids = [b for r in run.outcome.result.rounds for b in r.bids]
    assert bids
    assert all(b.q_action is not None for b in bids)


def test_a_withdrawal_names_what_it_arranged(config, scenario):
    """The whole point: three different exits are no longer one indistinguishable row."""
    source, candidates = scenario
    run = run_allocation(
        config=config, source=source, candidates=candidates, now=NOW,
        query="ICU bed", read_alternatives=True,
    )
    exits = [
        b for r in run.outcome.result.rounds for b in r.bids
        if b.action is Action.WITHDRAW
    ]
    assert exits, "the ward_crash scenario should produce at least one withdrawal"
    for bid in exits:
        assert bid.q_action.exits
        if bid.q_action.arranges_care:
            assert bid.plan is not None


def test_the_audit_row_carries_the_decision_and_its_plan(config, scenario):
    """Without these columns the reward stays unattributable in the log."""
    source, candidates = scenario
    run = run_allocation(
        config=config, source=source, candidates=candidates, now=NOW,
        query="ICU bed", read_alternatives=True,
    )
    rows = run.bundle.bids
    assert all(row.q_action is not None for row in rows)
    assert all(isinstance(row.plan, dict) for row in rows)
    assert all(row.feasible_actions for row in rows)

    arranged = [r for r in rows if r.q_action in {"withdraw_alternative", "re_enter_later"}]
    for row in arranged:
        assert row.plan, f"{row.q_action} must serialise its plan"


def test_a_rule_based_policy_publishes_no_q_values(config, scenario):
    """It ranks nothing, and must not appear to — invented scores would become training labels."""
    source, candidates = scenario
    run = run_allocation(
        config=config, source=source, candidates=candidates, now=NOW, query="ICU bed"
    )
    assert all(row.q_values == {} for row in run.bundle.bids)


def test_feasible_separates_declined_from_unavailable(config, scenario):
    source, candidates = scenario
    unread = run_allocation(
        config=config, source=source, candidates=candidates, now=NOW,
        query="ICU bed", read_alternatives=False,
    )
    feasible = {a for row in unread.bundle.bids for a in row.feasible_actions}
    assert "withdraw_alternative" not in feasible
    assert "withdraw_unplanned" in feasible


def test_exit_reason_distinguishes_the_strategic_exits():
    assert ExitReason.for_action(QAction.WITHDRAW_ALTERNATIVE) == ExitReason.ALTERNATIVE
    assert ExitReason.for_action(QAction.AWAIT_NEXT_RESOURCE) == ExitReason.AWAIT_NEXT
    assert ExitReason.for_action(QAction.RE_ENTER_LATER) == ExitReason.RE_ENTER
    assert ExitReason.for_action(QAction.WITHDRAW_UNPLANNED) == ExitReason.UNPLANNED
    assert len({
        ExitReason.for_action(a) for a in QAction if a.exits
    }) == 4, "four exits must not collapse to one reason"


def test_the_narrow_seam_never_invents_a_strategic_exit(config, scenario):
    """A three-action policy's withdrawal arranged nothing and must not be promoted."""
    from allocation.contracts import BiddingPolicy  # noqa: F401
    from allocation.policy.heuristic import HeuristicPolicy

    class NarrowPolicy:
        """Exposes only ``decide`` — the pre-Q-space interface."""

        name = "narrow"

        def __init__(self, config):
            self._inner = HeuristicPolicy(config)

        def decide(self, *args, **kwargs):
            return self._inner.decide(*args, **kwargs)

    source, candidates = scenario
    run = run_allocation(
        config=config, source=source, candidates=candidates, now=NOW,
        query="ICU bed", policy=NarrowPolicy(config), read_alternatives=True,
    )
    exits = [
        b for r in run.outcome.result.rounds for b in r.bids if b.action is Action.WITHDRAW
    ]
    assert exits
    assert all(b.q_action is QAction.WITHDRAW_UNPLANNED for b in exits)
    assert all(b.plan is None for b in exits)


# ---------------------------------------------------------------------------------------
# The exits have different consequences — which is what makes them learnable
# ---------------------------------------------------------------------------------------


def test_participation_removes_a_placed_patient_from_the_queue(config, scenario):
    source, candidates = scenario
    ledger = ParticipationLedger.for_candidates(config, candidates, NOW)
    target = candidates[0]

    ledger._apply(
        target.candidate_id, target.agent, QAction.WITHDRAW_ALTERNATIVE,
        PathwayPlan(target_unit="hdu", safe_hold_minutes=240), NOW, attempts=1,
    )
    assert ledger.standing_of(target.candidate_id).standing is Standing.RESOLVED
    assert target not in ledger.bidders(candidates, NOW)


def test_a_monitored_patient_sits_out_until_the_trigger_fires(config, scenario):
    source, candidates = scenario
    ledger = ParticipationLedger.for_candidates(config, candidates, NOW)
    target = candidates[0]
    trigger = _trigger(candidate_id=target.candidate_id, agent=target.agent)

    ledger._apply(
        target.candidate_id, target.agent, QAction.RE_ENTER_LATER,
        PathwayPlan(target_unit="hdu", reentry=trigger), NOW, attempts=1,
    )
    assert ledger.standing_of(target.candidate_id).standing is Standing.MONITORED
    assert target not in ledger.bidders(candidates, NOW + timedelta(minutes=10))

    back = ledger.bidders(candidates, NOW + timedelta(minutes=20), news2=lambda _: 99.0)
    assert target in back
    assert ledger.standing_of(target.candidate_id).standing is Standing.ACTIVE


def test_an_abandoned_patient_stays_in_the_queue_and_is_counted(config, scenario):
    """Nothing else the system reports distinguishes this from a safe hand-off."""
    source, candidates = scenario
    ledger = ParticipationLedger.for_candidates(config, candidates, NOW)
    target = candidates[0]

    ledger._apply(
        target.candidate_id, target.agent, QAction.WITHDRAW_UNPLANNED, None, NOW, attempts=1
    )
    assert ledger.standing_of(target.candidate_id).standing is Standing.ACTIVE
    assert target in ledger.bidders(candidates, NOW)
    assert ledger.abandoned == 1


def test_a_deferred_patient_bids_again_next_auction(config, scenario):
    source, candidates = scenario
    ledger = ParticipationLedger.for_candidates(config, candidates, NOW)
    target = candidates[0]

    ledger._apply(
        target.candidate_id, target.agent, QAction.AWAIT_NEXT_RESOURCE,
        PathwayPlan(expected_release_at=NOW + timedelta(minutes=35), release_probability=0.88),
        NOW, attempts=1,
    )
    assert ledger.standing_of(target.candidate_id).standing is Standing.DEFERRED
    assert target in ledger.bidders(candidates, NOW)


def test_tracking_participation_changes_who_wins_across_a_session(config, scenario):
    """The regression that proves the exits are not cosmetic.

    Untracked, one cohort bids forever and the highest-utility department monopolises.
    Tracked, placed patients leave the queue and the wins spread — and some releases find no
    bidder at all, which is the correct outcome when the alternatives absorbed the demand.
    """
    source, candidates = scenario
    events = event_schedule(NOW, 6, timedelta(minutes=75))

    untracked = run_session(
        config, source, candidates, NOW, events, query="ICU bed", track_participation=False
    )
    tracked = run_session(
        config, source, candidates, NOW, events, query="ICU bed",
        track_participation=True, read_alternatives=True,
    )

    assert len(untracked.runs) == 6
    assert len(tracked.runs) < len(untracked.runs), "placed patients should stop bidding"
    assert len(tracked.wins) > len(untracked.wins), "wins should spread across departments"
    assert tracked.participation is not None
    assert tracked.participation.counts()[Standing.RESOLVED] > 0


def test_a_departing_department_keeps_its_budget_row(config, scenario):
    """Regression: budgets were replaced per auction, so an agent that sat out lost its row."""
    source, candidates = scenario
    events = event_schedule(NOW, 5, timedelta(minutes=75))
    result = run_session(
        config, source, candidates, NOW, events, query="ICU bed",
        track_participation=True, read_alternatives=True,
    )
    agents = {c.agent for c in candidates}
    for report in result.shifts:
        assert agents <= set(report.closed), "every department must keep a budget row"
