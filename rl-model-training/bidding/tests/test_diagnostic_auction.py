"""One diagnostic auction, on the shared auction core.

The point of these tests is that the reuse is real: the same guards clamp the bids, the same
ledger charges the budgets, the same reserve closes the auction. Where a bed idea is being
reinterpreted rather than reused — occupancy becoming committed capacity — that substitution
is pinned too, because it is the one place the two families could silently diverge.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from allocation.config import load_config
from allocation.contracts import Action, AgentKind, AuctionMode
from allocation.use_cases.diagnostic_machine import (
    MODALITIES,
    DiagnosticMachineState,
    DiagnosticModality,
    DiagnosticPathwayAction,
    DiagnosticRequest,
    MachineStatus,
    diagnostic_shift,
    for_modality,
    open_diagnostic_budgets,
    run_diagnostic_auction,
)
from allocation.use_cases.diagnostic_machine.auction import (
    OUTCOME_AWARDED,
    committed_fraction,
    free_slots,
)
from allocation.use_cases.diagnostic_machine.contracts import AllocationInterval

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
CT = DiagnosticModality.CT
MRI = DiagnosticModality.MRI


@pytest.fixture(scope="module")
def config():
    return for_modality(load_config(), CT)


@pytest.fixture(scope="module")
def profile():
    return MODALITIES.get(CT)


@pytest.fixture
def budgets(config, profile, machine):
    return open_diagnostic_budgets(config, profile, diagnostic_shift(NOW), machines=[machine])


@pytest.fixture
def machine():
    return DiagnosticMachineState(
        machine_id="CT-01",
        modality=CT,
        status=MachineStatus.AVAILABLE,
        window_starts_at=NOW,
        window_ends_at=NOW + timedelta(hours=8),
        capabilities=frozenset({"head", "chest", "abdomen", "angiography", "contrast"}),
        setup_minutes=5,
        cleanup_minutes=5,
    )


def make_request(request_id, agent, minutes, ct_yield, **changes) -> DiagnosticRequest:
    values = dict(
        request_id=request_id,
        patient_token=f"patient-{request_id}",
        agent=agent,
        clinical_question=f"question-{request_id}",
        requested_procedure="ct",
        eligible_modalities=frozenset({CT}),
        requested_at=NOW,
        latest_useful_at=NOW + timedelta(minutes=minutes),
        estimated_duration=timedelta(minutes=15),
        diagnostic_yield=ct_yield,
        management_impact_probability=0.8,
        management_impact_importance=0.9,
        required_capabilities=frozenset({"head"}),
        transport_minutes=15,
    )
    values.update(changes)
    return DiagnosticRequest(**values)


def three_requests():
    return [
        make_request("er-1", AgentKind.ER, 40, 0.95),
        make_request("icu-1", AgentKind.ICU, 90, 0.85),
        make_request("ot-1", AgentKind.OT, 180, 0.60),
    ]


def run(config, profile, machine, budgets, requests=None, **kwargs):
    return run_diagnostic_auction(
        config,
        profile,
        [machine],
        requests or three_requests(),
        budgets,
        opened_at=NOW,
        mode=AuctionMode.SIMULATION,
        **kwargs,
    )


# -- it runs and it awards ---------------------------------------------------------------------


def test_the_most_urgent_high_yield_question_wins(config, profile, machine, budgets):
    result = run(config, profile, machine, budgets).result
    assert result.outcome == OUTCOME_AWARDED
    assert result.winner is AgentKind.ER
    assert result.winning_request_id == "er-1"
    assert result.awarded_interval is not None


def test_the_award_is_an_interval_including_setup_and_cleanup(config, profile, machine, budgets):
    result = run(config, profile, machine, budgets).result
    interval = result.awarded_interval
    assert interval is not None
    held = (interval.ends_at - interval.starts_at).total_seconds() / 60
    assert held == 15 + machine.setup_minutes + machine.cleanup_minutes


def test_losers_are_recorded_with_their_bids_and_pathways(config, profile, machine, budgets):
    """Both a winning and a losing episode are needed; the log must record the losers."""
    result = run(config, profile, machine, budgets).result
    agents = {bid.agent for bid in result.bids}
    assert agents == {AgentKind.ER, AgentKind.ICU, AgentKind.OT}
    assert all(bid.pathway in set(DiagnosticPathwayAction) for bid in result.bids)


def test_no_bid_exceeds_its_own_ceiling(config, profile, machine, budgets):
    """The ceiling guard, applied by the shared ``apply_guards``."""
    result = run(config, profile, machine, budgets).result
    for bid in result.bids:
        assert bid.amount <= bid.ceiling + 1e-9


def test_pathway_and_action_agree_on_every_row(config, profile, machine, budgets):
    result = run(config, profile, machine, budgets).result
    for bid in result.bids:
        if bid.action is Action.WITHDRAW:
            assert bid.pathway.exits
        else:
            assert not bid.pathway.exits


def test_an_exiting_row_carries_its_plan(config, profile, machine, budgets):
    """An exit that arranged something must say what, or it cannot be scored for it."""
    result = run(config, profile, machine, budgets).result
    for bid in result.bids:
        if bid.pathway.arranges_diagnostic_pathway:
            assert bid.plan is not None
        if bid.pathway is DiagnosticPathwayAction.WITHDRAW_UNPLANNED:
            assert bid.plan is None


# -- budgets settle through the shared ledger ----------------------------------------------------


def test_the_winner_pays_and_the_losers_pay_a_participation_charge(
    config, profile, machine, budgets
):
    outcome = run(config, profile, machine, budgets, charge_budgets=True)
    winner = outcome.result.winner
    assert winner is not None
    assert outcome.spends[winner].cost > 0
    losers = [a for a in outcome.spends if a is not winner]
    assert losers
    for agent in losers:
        # Outcome factor 0.1: small, and its job is to stop endless meaningless auctions.
        assert 0 < outcome.spends[agent].cost < outcome.spends[winner].cost


def test_a_non_binding_auction_does_not_move_a_budget(config, profile, machine, budgets):
    outcome = run(config, profile, machine, budgets)
    for agent, state in outcome.budgets.items():
        assert state.budget_remaining == budgets[agent].budget_remaining


# -- eligibility is a hard filter ------------------------------------------------------------------


def test_a_machine_that_cannot_do_the_procedure_is_filtered_before_bidding(
    config, profile, machine, budgets
):
    """Never a penalty inside the auction — a penalty is something a policy can outspend."""
    unservable = make_request(
        "er-1", AgentKind.ER, 60, 0.9, required_capabilities=frozenset({"pet"})
    )
    result = run(
        config, profile, machine, budgets,
        requests=[unservable, make_request("icu-1", AgentKind.ICU, 90, 0.85)],
    ).result
    assert AgentKind.ER not in {bid.agent for bid in result.bids}
    assert result.winner is AgentKind.ICU


def test_one_request_per_agent_per_auction(config, profile, machine, budgets):
    """Two of ER's own patients is a queueing question this auction does not answer."""
    with pytest.raises(ValueError, match="one request per agent"):
        run(
            config, profile, machine, budgets,
            requests=[
                make_request("er-1", AgentKind.ER, 60, 0.9),
                make_request("er-2", AgentKind.ER, 90, 0.8),
            ],
        )


def test_a_request_that_cannot_be_served_in_time_never_bids(config, profile, machine, budgets):
    impossible = make_request("er-1", AgentKind.ER, 5, 0.95)
    result = run(
        config, profile, machine, budgets,
        requests=[impossible, make_request("icu-1", AgentKind.ICU, 90, 0.85)],
    ).result
    assert AgentKind.ER not in {bid.agent for bid in result.bids}
    assert result.winner is AgentKind.ICU


def test_no_eligible_bidder_is_an_error_not_a_silent_no_award(config, profile, machine, budgets):
    with pytest.raises(ValueError, match="no eligible bidders"):
        run(config, profile, machine, budgets,
            requests=[make_request("er-1", AgentKind.ER, 5, 0.95)])


def test_one_auction_allocates_one_modality(config, profile, budgets, machine):
    mri = DiagnosticMachineState(
        machine_id="MRI-01",
        modality=MRI,
        status=MachineStatus.AVAILABLE,
        window_starts_at=NOW,
        window_ends_at=NOW + timedelta(hours=8),
    )
    with pytest.raises(ValueError, match="one modality"):
        run_diagnostic_auction(
            config, profile, [machine, mri], three_requests(), budgets, opened_at=NOW
        )


# -- the occupancy substitution --------------------------------------------------------------------


def test_committed_fraction_is_the_occupancy_the_formulas_read(machine):
    from dataclasses import replace

    empty = committed_fraction([machine], NOW, timedelta(hours=4))
    half = committed_fraction(
        [replace(machine, allocations=(
            AllocationInterval("CT-01", "x", NOW, NOW + timedelta(hours=2)),
        ))],
        NOW,
        timedelta(hours=4),
    )
    assert empty == 0.0
    assert half == pytest.approx(0.5)


def test_free_slots_counts_what_the_modality_could_still_take(machine):
    request = make_request("er-1", AgentKind.ER, 240, 0.9)
    slots = free_slots([machine], request, NOW, timedelta(hours=4))
    # Four hours of window, 25 minutes held per request.
    assert slots == pytest.approx(240 / 25, rel=0.01)


def test_a_busier_scanner_raises_contention(config, profile, machine, budgets):
    from dataclasses import replace

    quiet = run(config, profile, machine, budgets).result
    busy_machine = replace(machine, allocations=(
        AllocationInterval("CT-01", "x", NOW + timedelta(hours=1), NOW + timedelta(hours=4)),
    ))
    busy = run_diagnostic_auction(
        config, profile, [busy_machine], three_requests(), budgets,
        opened_at=NOW, mode=AuctionMode.SIMULATION,
    ).result
    assert busy.contention > quiet.contention


# -- determinism -------------------------------------------------------------------------------------


def test_the_same_inputs_produce_the_same_auction(config, profile, machine, budgets):
    a = run(config, profile, machine, budgets).result
    b = run(config, profile, machine, budgets).result
    assert [(x.agent, x.amount, x.pathway) for x in a.bids] == [
        (y.agent, y.amount, y.pathway) for y in b.bids
    ]
    assert a.winner == b.winner and a.winning_bid == b.winning_bid


def test_the_auction_key_buckets_by_time_and_machine(config, profile, machine, budgets):
    result = run(config, profile, machine, budgets).result
    assert result.auction_key.startswith("ct:CT-01:")


def test_versions_are_stamped_from_the_modality_tables(config, profile, machine, budgets):
    result = run(config, profile, machine, budgets).result
    assert result.caps_version == config.caps_version
    assert result.config_version == config.config_version
    assert "caps.ct" in result.unsigned_rules
