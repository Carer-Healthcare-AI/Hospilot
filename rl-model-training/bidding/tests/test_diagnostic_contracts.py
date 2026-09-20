from datetime import datetime, timedelta, timezone

import pytest

from allocation.contracts import Action, AgentKind
from allocation.use_cases.diagnostic_machine import (
    AllocationInterval,
    DiagnosticComponent,
    DiagnosticMachineState,
    DiagnosticModality,
    DiagnosticPathwayAction,
    DiagnosticRequest,
    DiagnosticUtility,
    INITIAL_ASSUMED_CAPS,
    MachineStatus,
)


NOW = datetime(2026, 8, 25, 9, 0, tzinfo=timezone.utc)


def request(**changes):
    values = dict(
        request_id="req-1",
        patient_token="patient-1",
        agent=AgentKind.ER,
        clinical_question="intracranial haemorrhage",
        requested_procedure="non-contrast head imaging",
        eligible_modalities=frozenset({DiagnosticModality.CT, DiagnosticModality.MRI}),
        requested_at=NOW,
        latest_useful_at=NOW + timedelta(hours=2),
        estimated_duration=timedelta(minutes=20),
        diagnostic_yield=0.9,
        management_impact_probability=0.8,
        management_impact_importance=1.0,
        required_capabilities=frozenset({"head"}),
    )
    values.update(changes)
    return DiagnosticRequest(**values)


def test_contract_is_modality_neutral():
    item = request()
    assert item.eligible_modalities == {DiagnosticModality.CT, DiagnosticModality.MRI}
    assert item.diagnostic_value == pytest.approx(0.72)


def test_machine_checks_modality_capability_deadline_and_schedule():
    occupied = AllocationInterval("CT-01", "other", NOW + timedelta(minutes=20), NOW + timedelta(minutes=40))
    machine = DiagnosticMachineState(
        machine_id="CT-01",
        modality=DiagnosticModality.CT,
        status=MachineStatus.AVAILABLE,
        window_starts_at=NOW,
        window_ends_at=NOW + timedelta(hours=4),
        capabilities=frozenset({"head", "contrast"}),
        allocations=(occupied,),
        cleanup_minutes=5,
    )
    assert machine.can_allocate(request(), NOW + timedelta(minutes=45))
    assert not machine.can_allocate(request(), NOW + timedelta(minutes=10))


def test_pathway_decision_is_separate_from_auction_action():
    assert DiagnosticPathwayAction.WIN_NOW.auction_action is Action.INCREASE_BID
    assert DiagnosticPathwayAction.CONTINUE.auction_action is Action.INCREASE_BID
    assert DiagnosticPathwayAction.USE_ALTERNATIVE.auction_action is Action.WITHDRAW
    assert DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY.auction_action is Action.WITHDRAW


def test_diagnostic_utility_uses_capped_bed_math():
    scores = {component: 0.5 for component in DiagnosticComponent}
    utility = DiagnosticUtility(scores)
    assert utility.total == pytest.approx(sum(INITIAL_ASSUMED_CAPS.values()) * 0.5)


def test_invalid_component_score_fails():
    scores = {component: 0.5 for component in DiagnosticComponent}
    scores[DiagnosticComponent.URGENCY] = 1.1
    with pytest.raises(ValueError, match="scores must be in"):
        DiagnosticUtility(scores)
