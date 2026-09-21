"""The schedule-based simulator, and the deterministic scenarios it runs.

The scheduling invariants are the ones worth pinning hardest — durations, setup/cleanup,
deadlines, capabilities, non-overlap — because every one of them is a place where a plausible
number could come out of a run that could not actually happen in a department.

The other half is fates. A simulator that only records winners scores every strategic exit as
an abandonment, which is how the bed simulator came to understate every baseline it produced.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.use_cases.diagnostic_machine import (
    MODALITIES,
    DiagnosticModality,
    Fate,
    diagnostic_shift,
    for_modality,
    open_diagnostic_budgets,
    simulate,
)
from allocation.use_cases.diagnostic_machine.scenarios import (
    EPOCH,
    SCENARIOS,
    all_scenarios,
    machine,
    request,
    run_all,
)

CT = DiagnosticModality.CT
MRI = DiagnosticModality.MRI
US = DiagnosticModality.ULTRASOUND


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def results(config):
    return run_all(config)


# -- scheduling invariants, on every scenario ------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_bookings_never_overlap(results, name):
    for state in results[name].machines:
        ordered = sorted(state.allocations, key=lambda i: i.starts_at)
        assert not any(a.overlaps(b) for a, b in zip(ordered, ordered[1:]))


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_booking_holds_setup_and_cleanup(results, name):
    """A scanner booked at the nominal duration is over-booked."""
    result = results[name]
    by_machine = {m.machine_id: m for m in result.machines}
    for procedure in result.procedures:
        state = by_machine[procedure.machine_id]
        booking = next(
            i for i in state.allocations if i.request_id == procedure.request_id
        )
        assert booking.starts_at == procedure.starts_at - timedelta(
            minutes=state.setup_minutes
        )
        assert booking.ends_at == procedure.ends_at + timedelta(
            minutes=state.cleanup_minutes
        )


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_no_procedure_lands_after_its_deadline(results, name):
    """Deadlines are enforced by refusing the booking, not by scoring it badly afterwards."""
    for procedure in results[name].procedures:
        assert procedure.on_time
        assert procedure.deadline_slack_minutes >= 0
    assert results[name].metrics.answered_late == 0
    assert results[name].metrics.constraint_violations == 0


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_bookings_stay_inside_the_machine_window(results, name):
    for state in results[name].machines:
        for booking in state.allocations:
            assert booking.starts_at >= state.window_starts_at
            assert booking.ends_at <= state.window_ends_at


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_request_gets_exactly_one_fate(results, name):
    result = results[name]
    ids = [outcome.request_id for outcome in result.outcomes]
    assert len(ids) == len(set(ids))
    assert result.metrics.requests == len(ids)
    assert sum(
        (
            result.metrics.answered,
            result.metrics.answered_late,
            result.metrics.diverted,
            result.metrics.expired,
            result.metrics.abandoned,
            result.metrics.pending,
        )
    ) == result.metrics.requests


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_a_machine_only_runs_what_it_can_do(results, name):
    """Capabilities are a hard filter, checked against what was actually booked."""
    result = results[name]
    by_machine = {m.machine_id: m for m in result.machines}
    arrivals = {a.request.request_id: a.request for a in _arrivals(name)}
    for procedure in result.procedures:
        state = by_machine[procedure.machine_id]
        assert arrivals[procedure.request_id].required_capabilities <= state.capabilities


def _arrivals(name):
    return SCENARIOS[name]().arrivals


# -- determinism -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_a_scenario_reruns_identically(config, name):
    a = SCENARIOS[name]().run(config)
    b = SCENARIOS[name]().run(config)
    assert [(o.request_id, o.fate) for o in a.outcomes] == [
        (o.request_id, o.fate) for o in b.outcomes
    ]
    assert a.metrics == b.metrics


# -- what each scenario is built to demonstrate -------------------------------------------------


def test_three_way_contention_serves_everyone_without_abandoning(results):
    """ER's question is the most urgent; ICU and OT wait rather than being dropped."""
    result = results["three_way_contention"]
    assert result.metrics.abandoned == 0
    assert result.metrics.timeliness == 1.0
    first = result.auctions[0]
    assert first.winner is AgentKind.ER


def test_alternative_diverts_only_the_question_ultrasound_can_answer(results):
    """The §13 claim: no universal ladder. Same modality, opposite outcomes."""
    result = results["alternative_answers_the_question"]
    diverted = result.by_fate(Fate.DIVERTED)
    assert [outcome.request_id for outcome in diverted] == ["er-gallbladder"]
    assert diverted[0].diverted_to is US
    assert {o.request_id for o in result.outcomes if o.fate == Fate.ANSWERED} == {
        "er-head",
        "icu-abdo",
    }


def test_deadline_pressure_records_expiry_as_expiry(results):
    """Capacity binds. What must not happen is an unserved request counted as served."""
    result = results["deadline_pressure"]
    assert result.metrics.expired > 0
    assert result.metrics.answered + result.metrics.expired == result.metrics.requests
    assert result.metrics.timeliness < 1.0
    assert result.metrics.management_impact_rate < 1.0


def test_mri_runs_the_same_code_with_different_data(results):
    """Forty-five-minute studies, one at a time, no branch anywhere."""
    result = results["mri_scarcity"]
    profile = MODALITIES.get(MRI)
    assert result.metrics.answered == 3
    for procedure in result.procedures:
        held = procedure.ends_at - procedure.starts_at
        assert held == profile.typical_duration


def test_capability_filter_routes_the_ctpa_to_the_capable_scanner(results):
    result = results["capability_is_a_hard_filter"]
    ctpa = next(p for p in result.procedures if p.request_id == "er-pe")
    assert ctpa.machine_id == "CT-02"


def test_portable_modality_ignores_distance(results, config):
    """Transport burden is zero on a portable unit however far the patient is."""
    from allocation.use_cases.diagnostic_machine.scoring import transport_burden

    scenario = SCENARIOS["portable_costs_no_transport"]()
    far, near = (a.request for a in scenario.arrivals)
    profile = scenario.profile
    assert transport_burden(far, profile) == transport_burden(near, profile) == 0.0
    assert results["portable_costs_no_transport"].metrics.answered == 2


# -- metrics -------------------------------------------------------------------------------------


def test_impact_rate_is_measured_against_what_was_requested(results):
    """Delivered over performed reads 1.00 on a run that answered one request in fifty."""
    pressured = results["deadline_pressure"].metrics
    assert 0.0 < pressured.management_impact_rate < 1.0


def test_utilisation_counts_held_time_not_procedure_time(results):
    result = results["mri_scarcity"]
    assert 0.0 < result.metrics.utilisation <= 1.0


def test_alternative_usage_is_a_share_of_the_unserved(results):
    assert results["alternative_answers_the_question"].metrics.alternative_usage == 1.0
    assert results["three_way_contention"].metrics.alternative_usage == 0.0


def test_budgets_burn_and_are_never_negative(results):
    for result in results.values():
        for state in result.budgets.values():
            assert state.budget_remaining >= 0
            assert state.spent >= 0


# -- boundaries ------------------------------------------------------------------------------------


def test_a_request_arriving_after_the_clock_stops_is_pending_not_lost(config):
    """A boundary, not a failure of the mechanism — and it must still get a fate."""
    scoped = for_modality(config, CT)
    profile = MODALITIES.get(CT)
    scanner = machine("CT-01", CT, ("head",))
    late = request(
        "er-late", AgentKind.ER, "late question", CT,
        at_minutes=200, useful_for_minutes=120, yields={CT: 0.9}, capabilities=("head",),
    )
    budgets = open_diagnostic_budgets(
        scoped, profile, diagnostic_shift(EPOCH), machines=[scanner]
    )
    result = simulate(
        scoped, profile, [scanner], [late], budgets,
        starts_at=EPOCH, ends_at=EPOCH + timedelta(hours=1),
    )
    assert result.metrics.requests == 1
    assert result.by_fate(Fate.PENDING)[0].request_id == "er-late"


def test_cadence_must_be_positive(config):
    scoped = for_modality(config, CT)
    profile = MODALITIES.get(CT)
    scanner = machine("CT-01", CT, ("head",))
    budgets = open_diagnostic_budgets(scoped, profile, diagnostic_shift(EPOCH))
    with pytest.raises(ValueError, match="cadence must be positive"):
        simulate(
            scoped, profile, [scanner], [], budgets,
            starts_at=EPOCH, ends_at=EPOCH + timedelta(hours=1),
            cadence=timedelta(0),
        )


def test_every_scenario_declares_what_it_expects():
    """A fixture nobody can read the intent of is a fixture nobody will maintain."""
    for scenario in all_scenarios():
        assert scenario.expects
        assert scenario.description
