"""The diagnostic bidding policy and the pathway/action split.

Two things under test. First, that the diagnostic ladder still derives the alphas the worked
example pins — it is a copy of the bed rules, kept local so the bed path stays frozen, and
``test_diagnostic_ladder.py`` is what stops the copy drifting. Second, that ``DiagnosticPathwayAction`` and ``Action`` never collapse into
each other, which is what keeps an abandonment distinguishable from a handover in the log.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from allocation.budget.shifts import Shift
from allocation.config import load_config
from allocation.contracts import Action, AgentKind, Bid, RoundState
from allocation.use_cases.diagnostic_machine.ladder import Verdict, climb
from allocation.use_cases.diagnostic_machine import (
    MODALITIES,
    DiagnosticDecision,
    DiagnosticHeuristicPolicy,
    DiagnosticMachineState,
    DiagnosticModality,
    DiagnosticPathwayAction,
    DiagnosticPlan,
    DiagnosticRequest,
    MachineStatus,
    build_options,
    diagnostic_shift,
    for_modality,
    next_capacity,
    open_diagnostic_budgets,
)
from allocation.use_cases.diagnostic_machine.contracts import AllocationInterval

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
CT = DiagnosticModality.CT
MRI = DiagnosticModality.MRI
US = DiagnosticModality.ULTRASOUND


@pytest.fixture(scope="module")
def config():
    return for_modality(load_config(), CT)


@pytest.fixture(scope="module")
def profile():
    return MODALITIES.get(CT)


@pytest.fixture
def budgets(config, profile):
    return open_diagnostic_budgets(config, profile, diagnostic_shift(NOW))


def make_request(**changes) -> DiagnosticRequest:
    values = dict(
        request_id="er-1",
        patient_token="patient-1",
        agent=AgentKind.ER,
        clinical_question="suspected pulmonary embolism",
        requested_procedure="CTPA",
        eligible_modalities=frozenset({CT}),
        requested_at=NOW,
        latest_useful_at=NOW + timedelta(minutes=45),
        estimated_duration=timedelta(minutes=15),
        diagnostic_yield=0.9,
        management_impact_probability=0.8,
        management_impact_importance=1.0,
    )
    values.update(changes)
    return DiagnosticRequest(**values)


def ct_machine(**changes) -> DiagnosticMachineState:
    values = dict(
        machine_id="CT-01",
        modality=CT,
        status=MachineStatus.AVAILABLE,
        window_starts_at=NOW,
        window_ends_at=NOW + timedelta(hours=8),
        capabilities=frozenset({"head", "chest", "abdomen", "angiography", "contrast"}),
        setup_minutes=5,
        cleanup_minutes=5,
    )
    values.update(changes)
    return DiagnosticMachineState(**values)


def view(*bids: tuple[AgentKind, float, Action]) -> RoundState:
    return RoundState(
        auction_id="a1",
        round_index=1,
        opened_at=NOW,
        bids=tuple(
            Bid(
                auction_id="a1",
                round_index=1,
                agent=agent,
                candidate_id=agent.value,
                action=action,
                amount=amount,
                utility=amount,
                ceiling=amount,
                contention=1.0,
            )
            for agent, amount, action in bids
        ),
        active_agents=frozenset(agent for agent, _, action in bids
                                if action is not Action.WITHDRAW),
    )


# -- the ladder ------------------------------------------------------------------------------


def test_diagnostic_and_bed_policies_derive_the_same_alpha(config, budgets):
    """Rule 4: what overtaking costs, derived rather than tabulated."""
    round_state = view((AgentKind.ER, 75.0, Action.INCREASE_BID),
                       (AgentKind.OT, 101.0, Action.INCREASE_BID))
    outcome = climb(
        config,
        agent=AgentKind.ER,
        ceiling=112.0,
        round_state=round_state,
        budget=budgets[AgentKind.ER],
        opening_alpha=0.4,
        lead_alpha=0.25,
        overtake_margin=1.0,
    )
    assert outcome.verdict is Verdict.COMPETE
    # (101 - 75 + 1) / (112 - 75) — RL-Steps section 14's 0.82.
    assert outcome.alpha == pytest.approx(27.0 / 37.0, abs=1e-9)


def test_ceiling_below_the_leader_exits(config, budgets, profile):
    policy = DiagnosticHeuristicPolicy(config)
    options = build_options(make_request(), profile, [ct_machine()], NOW)
    decision = policy.decide(
        AgentKind.ER,
        ceiling=40.0,
        round_state=view((AgentKind.ICU, 85.0, Action.INCREASE_BID)),
        budget=budgets[AgentKind.ER],
        options=options,
    )
    assert decision.exits
    assert decision.action is Action.WITHDRAW


# -- the pathway / action split ----------------------------------------------------------------


def test_every_pathway_maps_to_a_bid_mechanic():
    for pathway in DiagnosticPathwayAction:
        assert pathway.auction_action in {Action.INCREASE_BID, Action.WITHDRAW}


def test_hold_is_a_continue_not_a_win():
    """At the ceiling with nothing left to expose. Holding is a form of continuing."""
    decision = DiagnosticDecision(pathway=DiagnosticPathwayAction.CONTINUE, holds=True)
    assert decision.action is Action.HOLD
    assert not decision.exits


def test_an_arranged_exit_must_name_what_it_arranged():
    with pytest.raises(ValueError, match="must carry a DiagnosticPlan"):
        DiagnosticDecision(pathway=DiagnosticPathwayAction.USE_ALTERNATIVE)
    with pytest.raises(ValueError, match="must name the modality"):
        DiagnosticDecision(
            pathway=DiagnosticPathwayAction.USE_ALTERNATIVE, plan=DiagnosticPlan()
        )
    with pytest.raises(ValueError, match="must carry the expected capacity"):
        DiagnosticDecision(
            pathway=DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY, plan=DiagnosticPlan()
        )
    with pytest.raises(ValueError, match="must carry the condition"):
        DiagnosticDecision(
            pathway=DiagnosticPathwayAction.RE_ENTER_LATER, plan=DiagnosticPlan()
        )


def test_an_abandonment_may_not_carry_a_plan():
    """The invariant that stops an abandonment collecting what an arranged exit earns."""
    with pytest.raises(ValueError, match="arranged nothing"):
        DiagnosticDecision(
            pathway=DiagnosticPathwayAction.WITHDRAW_UNPLANNED,
            plan=DiagnosticPlan(alternative_modality=US),
        )


def test_a_competing_decision_may_not_carry_a_plan():
    with pytest.raises(ValueError, match="must not carry a plan"):
        DiagnosticDecision(
            pathway=DiagnosticPathwayAction.CONTINUE, plan=DiagnosticPlan(note="x")
        )


def test_arranges_diagnostic_pathway_excludes_abandonment():
    assert DiagnosticPathwayAction.USE_ALTERNATIVE.arranges_diagnostic_pathway
    assert DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY.arranges_diagnostic_pathway
    assert DiagnosticPathwayAction.RE_ENTER_LATER.arranges_diagnostic_pathway
    assert not DiagnosticPathwayAction.WITHDRAW_UNPLANNED.arranges_diagnostic_pathway


# -- which exit ---------------------------------------------------------------------------------


def test_exit_prefers_an_alternative_with_real_capacity(config, profile, budgets):
    """An alternative modality that is free answers the question; nothing else needs doing."""
    policy = DiagnosticHeuristicPolicy(config)
    request = make_request(
        eligible_modalities=frozenset({CT, US}),
        modality_yields={CT: 0.7, US: 0.85},
    )
    ultrasound = DiagnosticMachineState(
        machine_id="US-01",
        modality=US,
        status=MachineStatus.AVAILABLE,
        window_starts_at=NOW,
        window_ends_at=NOW + timedelta(hours=8),
        capabilities=frozenset({"abdomen", "vascular"}),
    )
    options = build_options(
        request, profile, [ct_machine()], NOW, alternative_machines=[ultrasound]
    )
    decision = policy.decide(
        AgentKind.ER,
        ceiling=10.0,
        round_state=view((AgentKind.ICU, 90.0, Action.INCREASE_BID)),
        budget=budgets[AgentKind.ER],
        options=options,
    )
    assert decision.pathway is DiagnosticPathwayAction.USE_ALTERNATIVE
    assert decision.plan is not None
    assert decision.plan.alternative_modality is US


def test_a_partial_alternative_is_not_a_handover(config, profile, budgets):
    """Answering 40% of the question is not answering it.

    The bed rule ``min_safe_hold_minutes`` in the same shape: an exit that buys nothing is
    worse than staying in. Here the request should wait for the right machine instead.
    """
    policy = DiagnosticHeuristicPolicy(config)
    request = make_request(
        clinical_question="intra-abdominal sepsis source",
        eligible_modalities=frozenset({CT, US}),
        modality_yields={CT: 0.90, US: 0.40},
        latest_useful_at=NOW + timedelta(hours=3),
    )
    ultrasound = DiagnosticMachineState(
        machine_id="US-01",
        modality=US,
        status=MachineStatus.AVAILABLE,
        window_starts_at=NOW,
        window_ends_at=NOW + timedelta(hours=8),
        capabilities=frozenset({"abdomen"}),
    )
    options = build_options(
        request, profile, [ct_machine()], NOW, alternative_machines=[ultrasound]
    )
    assert options.best_alternative() is None
    decision = policy.decide(
        AgentKind.ER,
        ceiling=10.0,
        round_state=view((AgentKind.ICU, 90.0, Action.INCREASE_BID)),
        budget=budgets[AgentKind.ER],
        options=options,
    )
    assert decision.pathway is DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY


def test_a_sufficient_alternative_still_is_one(config, profile, budgets):
    """The other side of the same threshold — it must not refuse a real substitute."""
    request = make_request(
        clinical_question="right upper quadrant pain",
        eligible_modalities=frozenset({CT, US}),
        modality_yields={CT: 0.70, US: 0.85},
    )
    ultrasound = DiagnosticMachineState(
        machine_id="US-01",
        modality=US,
        status=MachineStatus.AVAILABLE,
        window_starts_at=NOW,
        window_ends_at=NOW + timedelta(hours=8),
        capabilities=frozenset({"abdomen"}),
    )
    options = build_options(
        request, profile, [ct_machine()], NOW, alternative_machines=[ultrasound]
    )
    assert options.best_alternative() == (US, 0.85)


def test_an_alternative_with_no_capacity_is_not_an_alternative(config, profile, budgets):
    """On paper is not the same as in time. Recording it would log a pathway that cannot run."""
    policy = DiagnosticHeuristicPolicy(config)
    request = make_request(
        eligible_modalities=frozenset({CT, US}),
        modality_yields={CT: 0.7, US: 0.85},
    )
    options = build_options(request, profile, [ct_machine()], NOW, alternative_machines=[])
    decision = policy.decide(
        AgentKind.ER,
        ceiling=10.0,
        round_state=view((AgentKind.ICU, 90.0, Action.INCREASE_BID)),
        budget=budgets[AgentKind.ER],
        options=options,
    )
    assert decision.pathway is not DiagnosticPathwayAction.USE_ALTERNATIVE


def test_exit_waits_for_scheduled_capacity_when_nothing_else_answers(
    config, profile, budgets
):
    """§19: in a bed environment this action waited on a forecast. Here it waits on a timetable."""
    policy = DiagnosticHeuristicPolicy(config)
    options = build_options(make_request(), profile, [ct_machine()], NOW)
    decision = policy.decide(
        AgentKind.ER,
        ceiling=10.0,
        round_state=view((AgentKind.ICU, 90.0, Action.INCREASE_BID)),
        budget=budgets[AgentKind.ER],
        options=options,
    )
    assert decision.pathway is DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY
    assert decision.plan is not None
    assert decision.plan.capacity_probability == 1.0


def test_next_capacity_reads_the_schedule_not_a_forecast():
    """Probability 1.0 because it is arithmetic on committed intervals, not a prediction."""
    request = make_request(latest_useful_at=NOW + timedelta(hours=3))
    busy = ct_machine(
        allocations=(
            AllocationInterval("CT-01", "other", NOW, NOW + timedelta(minutes=40)),
        )
    )
    at, probability = next_capacity([busy], request, NOW)
    assert probability == 1.0
    assert at == NOW + timedelta(minutes=45)  # release + 5 min setup


def test_next_capacity_is_none_when_nothing_fits_the_deadline():
    """The answer that makes AWAIT_NEXT_CAPACITY honest rather than a default."""
    request = make_request(latest_useful_at=NOW + timedelta(minutes=20))
    busy = ct_machine(
        allocations=(
            AllocationInterval("CT-01", "other", NOW, NOW + timedelta(hours=3)),
        )
    )
    assert next_capacity([busy], request, NOW) == (None, None)


def test_unreachable_capacity_is_not_feasible(config, profile, budgets):
    """A request that can never be served must not exit as though it had a plan."""
    policy = DiagnosticHeuristicPolicy(config)
    request = make_request(latest_useful_at=NOW + timedelta(minutes=20))
    busy = ct_machine(
        allocations=(
            AllocationInterval("CT-01", "other", NOW, NOW + timedelta(hours=3)),
        )
    )
    options = build_options(request, profile, [busy], NOW, monitorable=False)
    decision = policy.decide(
        AgentKind.ER,
        ceiling=10.0,
        round_state=view((AgentKind.ICU, 90.0, Action.INCREASE_BID)),
        budget=budgets[AgentKind.ER],
        options=options,
    )
    assert decision.pathway is DiagnosticPathwayAction.WITHDRAW_UNPLANNED
    assert decision.plan is None
    assert DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY not in decision.feasible


def test_feasible_records_what_was_available(config, profile, budgets):
    """Declined and unavailable must be distinguishable in the log."""
    policy = DiagnosticHeuristicPolicy(config)
    options = build_options(make_request(), profile, [ct_machine()], NOW)
    decision = policy.decide(
        AgentKind.ER,
        ceiling=80.0,
        round_state=view(),
        budget=budgets[AgentKind.ER],
        options=options,
    )
    assert DiagnosticPathwayAction.WIN_NOW in decision.feasible
    assert DiagnosticPathwayAction.USE_ALTERNATIVE not in decision.feasible


def test_win_now_when_the_answer_cannot_wait(config, profile, budgets):
    policy = DiagnosticHeuristicPolicy(config)
    urgent = build_options(
        make_request(latest_useful_at=NOW + timedelta(minutes=20)),
        profile, [ct_machine()], NOW,
    )
    relaxed = build_options(
        make_request(latest_useful_at=NOW + timedelta(hours=5)),
        profile, [ct_machine()], NOW,
    )
    for options, expected in (
        (urgent, DiagnosticPathwayAction.WIN_NOW),
        (relaxed, DiagnosticPathwayAction.CONTINUE),
    ):
        decision = policy.decide(
            AgentKind.ER, 80.0, view(), budgets[AgentKind.ER], options
        )
        assert decision.pathway is expected
        assert decision.action is Action.INCREASE_BID


def test_policy_ranks_nothing(config, profile, budgets):
    """A rule-based policy publishes an honest empty, not invented scores."""
    policy = DiagnosticHeuristicPolicy(config)
    options = build_options(make_request(), profile, [ct_machine()], NOW)
    decision = policy.decide(AgentKind.ER, 80.0, view(), budgets[AgentKind.ER], options)
    assert decision.action_values == {}


def test_budgets_must_come_from_this_modality_pool(config, profile):
    """Funding a scanner auction from a bed pool is the error the split exists to prevent."""
    with pytest.raises(ValueError, match="call config.for_modality"):
        open_diagnostic_budgets(load_config(), profile, diagnostic_shift(NOW))


def test_shift_helper_builds_an_eight_hour_shift():
    shift = diagnostic_shift(NOW)
    assert isinstance(shift, Shift)
    assert shift.end - shift.start == timedelta(hours=8)
