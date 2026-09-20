"""The eight diagnostic utility components.

Each test pins one claim the framework makes about what a component *means*, not the number it
currently produces — the numbers are unfitted and will move. What must not move is the
direction: urgency about the answer rather than the patient, alternatives scored against the
question rather than a ladder, transport free on a portable machine.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from allocation.contracts import AgentKind
from allocation.use_cases.diagnostic_machine import (
    MODALITIES,
    DiagnosticComponent,
    DiagnosticContext,
    DiagnosticMachineState,
    DiagnosticModality,
    DiagnosticRequest,
    MachineStatus,
    score_request,
)
from allocation.use_cases.diagnostic_machine.contracts import AllocationInterval
from allocation.use_cases.diagnostic_machine.scoring import (
    alternative_quality,
    delay_pressure,
    machine_scarcity,
    transport_burden,
    urgency,
)

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)
CT = DiagnosticModality.CT
MRI = DiagnosticModality.MRI
US = DiagnosticModality.ULTRASOUND


def make_request(**changes) -> DiagnosticRequest:
    values = dict(
        request_id="req-1",
        patient_token="patient-1",
        agent=AgentKind.ER,
        clinical_question="suspected pulmonary embolism",
        requested_procedure="CT pulmonary angiography",
        eligible_modalities=frozenset({CT}),
        requested_at=NOW,
        latest_useful_at=NOW + timedelta(hours=2),
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
        window_ends_at=NOW + timedelta(hours=4),
        capabilities=frozenset({"head", "chest", "abdomen", "angiography", "contrast"}),
        setup_minutes=5,
        cleanup_minutes=5,
    )
    values.update(changes)
    return DiagnosticMachineState(**values)


# -- urgency is about the answer -------------------------------------------------------------


def test_urgency_rises_as_the_safe_delay_shortens():
    short = make_request(latest_useful_at=NOW + timedelta(minutes=20))
    long = make_request(latest_useful_at=NOW + timedelta(hours=6))
    assert urgency(short) == 1.0
    assert urgency(long) < urgency(make_request())
    assert urgency(make_request()) < urgency(short)


def test_urgency_ignores_how_sick_the_patient_is():
    """No patient acuity input exists, and that is the design, not an omission."""
    a = make_request(management_impact_importance=1.0)
    b = make_request(management_impact_importance=0.1)
    assert urgency(a) == urgency(b)


# -- delay pressure is the thing that moves ---------------------------------------------------


def test_delay_pressure_rises_with_waiting():
    request = make_request()
    assert delay_pressure(request, NOW) == 0.0
    assert delay_pressure(request, NOW + timedelta(hours=1)) == pytest.approx(0.5)
    assert delay_pressure(request, NOW + timedelta(hours=3)) == 1.0


def test_delay_pressure_is_what_stops_the_queue_tail_starving():
    """A long-waiting routine request must eventually out-score a fresh urgent one."""
    routine = make_request(request_id="routine", latest_useful_at=NOW + timedelta(hours=4))
    profile = MODALITIES.get(CT)
    machines = [ct_machine(window_ends_at=NOW + timedelta(hours=8))]

    fresh = score_request(routine, profile, machines, NOW)
    waited = score_request(routine, profile, machines, NOW + timedelta(hours=3))
    assert waited.total > fresh.total


# -- alternatives are scored against the question, not a ladder -------------------------------


def test_alternative_scored_against_the_clinical_question():
    """The §13 case. Same two modalities, opposite answers, because the question differs."""
    pe = make_request(
        eligible_modalities=frozenset({CT, MRI}),
        modality_yields={CT: 0.95, MRI: 0.20},
    )
    cord = make_request(
        clinical_question="suspected cord compression",
        eligible_modalities=frozenset({CT, MRI}),
        modality_yields={CT: 0.30, MRI: 0.95},
    )
    assert alternative_quality(pe, CT) < 0.3
    assert alternative_quality(cord, CT) == 1.0


def test_no_scored_alternative_scores_zero():
    assert alternative_quality(make_request(), CT) == 0.0


def test_a_better_alternative_is_capped_at_one():
    """A referral error is not something to express as a bid."""
    request = make_request(
        eligible_modalities=frozenset({CT, MRI}),
        modality_yields={CT: 0.20, MRI: 0.95},
    )
    assert alternative_quality(request, CT) == 1.0


def test_best_alternative_modality_picks_the_highest_yield():
    request = make_request(
        eligible_modalities=frozenset({CT, MRI, US}),
        modality_yields={CT: 0.5, MRI: 0.7, US: 0.9},
    )
    assert request.best_alternative_modality(exclude=CT) == (US, 0.9)


# -- transport --------------------------------------------------------------------------------


def test_portable_modality_has_no_transport_burden():
    request = make_request(transport_minutes=60)
    assert transport_burden(request, MODALITIES.get(DiagnosticModality.X_RAY)) == 0.0
    assert transport_burden(request, MODALITIES.get(CT)) == 1.0


# -- machine scarcity --------------------------------------------------------------------------


def test_scarcity_counts_only_machines_that_could_take_this_request():
    """A scanner that cannot do the procedure is not slack."""
    request = make_request(required_capabilities=frozenset({"angiography"}))
    incapable = ct_machine(machine_id="CT-09", capabilities=frozenset({"head"}))
    assert machine_scarcity([incapable], request, NOW, timedelta(hours=4)) == 1.0


def test_scarcity_rises_with_bookings():
    request = make_request()
    empty = ct_machine()
    booked = ct_machine(
        allocations=(
            AllocationInterval("CT-01", "other", NOW, NOW + timedelta(hours=2)),
        )
    )
    assert machine_scarcity([empty], request, NOW, timedelta(hours=4)) == 0.0
    assert machine_scarcity([booked], request, NOW, timedelta(hours=4)) == pytest.approx(0.5)


def test_no_eligible_machine_is_maximally_scarce():
    assert machine_scarcity([], make_request(), NOW, timedelta(hours=4)) == 1.0


# -- assembly ---------------------------------------------------------------------------------


def test_score_request_fills_every_component():
    utility = score_request(make_request(), MODALITIES.get(CT), [ct_machine()], NOW)
    assert set(utility.scores) == set(DiagnosticComponent)
    assert all(0.0 <= score <= 1.0 for score in utility.scores.values())


def test_negative_caps_pull_the_total_down():
    """Alternative Quality, Transport Burden and Resource Stress carry their own sign."""
    clean = score_request(make_request(), MODALITIES.get(CT), [ct_machine()], NOW)
    burdened = score_request(
        make_request(transport_minutes=60), MODALITIES.get(CT), [ct_machine()], NOW
    )
    assert burdened.total < clean.total


def test_absent_operational_impact_reads_neutral_not_zero():
    """The bed engine's own rule: a component with no data must not push the utility down."""
    absent = score_request(make_request(), MODALITIES.get(CT), [ct_machine()], NOW)
    zero = score_request(
        make_request(),
        MODALITIES.get(CT),
        [ct_machine()],
        NOW,
        context=DiagnosticContext(operational_impact=0.0),
    )
    assert absent.total > zero.total


def test_resource_stress_falls_back_to_machines_out_of_service():
    request = make_request()
    profile = MODALITIES.get(CT)
    down = ct_machine(machine_id="CT-02", status=MachineStatus.OUT_OF_SERVICE)
    healthy = score_request(request, profile, [ct_machine()], NOW)
    degraded = score_request(request, profile, [ct_machine(), down], NOW)
    assert degraded.scores[DiagnosticComponent.RESOURCE_STRESS] == pytest.approx(0.5)
    assert degraded.total < healthy.total


def test_context_rejects_out_of_range_inputs():
    with pytest.raises(ValueError, match="must be in"):
        DiagnosticContext(operational_impact=1.4)
