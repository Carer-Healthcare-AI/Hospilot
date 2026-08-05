"""Per-domain live E2E tests — icu.

Owner: TODO   ·   Status: complete (Tier A + Tier B)

Tier-A tasks (census, forecasts) take only session_id. Tier-B tasks take an `inp`
dataclass whose fields are the OUTPUTS of upstream tasks, so we build them by chaining
real live outputs via fixtures:  get_icu_census -> analyze_icu_status -> approval/confirm,
and rank_icu_requests -> the ranked_requests consumers.
Run: pytest tests/e2e/test_icu.py -v
"""
import pytest

from agents.icu import activities as icu
from agents.icu.activities import (
    IcuAnalysisInput, IcuApprovalInput, IcuConfirmInput, IcuTransferInput, IcuAdmissionInput,
)
from _helpers import SESSION_ID, announce, assert_sane_shape


def _has(r, *keys):
    missing = {k for k in keys} - set(r)
    assert not missing, f"missing expected keys {missing}; got {sorted(r)}"


def _check_forecast(r):
    _has(r, "forecast_available")
    assert r["forecast_available"] in (True, False, 0, 1)


def _nonneg(r, key):
    v = r[key]
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        assert v >= 0, f"{key} = {v}, expected >= 0"


# --- shared live inputs, built by chaining real upstream task outputs --------

@pytest.fixture
async def census():
    return await icu.get_icu_census(SESSION_ID)


@pytest.fixture
async def analysis(census):
    return await icu.analyze_icu_status(IcuAnalysisInput(
        SESSION_ID, census["icu_admissions"], census["non_icu_admissions"],
        census["available_beds"], census["bed_by_id"],
    ))


@pytest.fixture
async def ranked(census):
    return await icu.rank_icu_requests(IcuTransferInput(
        SESSION_ID, census["non_icu_admissions"], census["available_beds"],
    ))


# --- Tier A ------------------------------------------------------------------

async def test_get_icu_census(census, capsys):
    announce(capsys, "icu:get_icu_census", "admissions/beds are lists, bed_by_id maps every available bed")
    assert_sane_shape("get_icu_census", census)
    _has(census, "icu_admissions", "non_icu_admissions", "available_beds", "bed_by_id")
    assert isinstance(census["bed_by_id"], dict)
    assert len(census["available_beds"]) <= len(census["bed_by_id"])


async def test_forecast_icu_demand(capsys):
    announce(capsys, "icu:forecast_icu_demand", "forecast envelope; predicted 24h admissions non-negative")
    r = await icu.forecast_icu_demand(SESSION_ID)
    assert_sane_shape("forecast_icu_demand", r)
    _check_forecast(r)
    if r["forecast_available"]:
        _has(r, "predicted_admissions_24h", "capacity_alert")
        _nonneg(r, "predicted_admissions_24h")


async def test_forecast_icu_occupancy(capsys):
    announce(capsys, "icu:forecast_icu_occupancy", "forecast envelope; occupied/free beds non-negative")
    r = await icu.forecast_icu_occupancy(SESSION_ID)
    assert_sane_shape("forecast_icu_occupancy", r)
    _check_forecast(r)
    if r["forecast_available"]:
        _has(r, "predicted_occupied_beds", "predicted_free_beds", "predicted_occupancy_percent", "status")
        _nonneg(r, "predicted_occupied_beds")
        _nonneg(r, "predicted_free_beds")


# --- Tier B (inputs chained from real upstream outputs) ----------------------

async def test_analyze_icu_status(analysis, capsys):
    announce(capsys, "icu:analyze_icu_status", "produces step-down/escalation candidate lists + summary")
    assert_sane_shape("analyze_icu_status", analysis)
    _has(analysis, "step_down_candidates", "escalation_candidates", "summary", "critical_vital_ids")
    assert isinstance(analysis["step_down_candidates"], list)
    assert isinstance(analysis["escalation_candidates"], list)
    assert isinstance(analysis["summary"], str)


async def test_create_icu_approval(analysis, capsys):
    announce(capsys, "icu:create_icu_approval", "creates an approval task from analysis candidates")
    r = await icu.create_icu_approval(IcuApprovalInput(
        SESSION_ID, analysis["step_down_candidates"], analysis["escalation_candidates"], analysis["summary"],
    ))
    assert_sane_shape("create_icu_approval", r)
    _has(r, "approval_id")


async def test_confirm_icu_actions(analysis, capsys):
    announce(capsys, "icu:confirm_icu_actions", "flags critical vitals and stages transfers (counts >= 0)")
    r = await icu.confirm_icu_actions(IcuConfirmInput(
        SESSION_ID, analysis["critical_vital_ids"], assessments=[],
    ))
    assert_sane_shape("confirm_icu_actions", r)
    _has(r, "critical_vitals_flagged", "transfers_staged")


async def test_rank_icu_requests(ranked, capsys):
    announce(capsys, "icu:rank_icu_requests", "ranked_requests list; risk/ventilator counts within it")
    assert_sane_shape("rank_icu_requests", ranked)
    _has(ranked, "ranked_requests", "deterioration_risk_count", "ventilator_dependent_count")
    assert isinstance(ranked["ranked_requests"], list)
    assert ranked["deterioration_risk_count"] <= len(ranked["ranked_requests"])
    assert ranked["ventilator_dependent_count"] <= len(ranked["ranked_requests"])


async def test_prioritize_ventilator_bed(ranked, capsys):
    announce(capsys, "icu:prioritize_ventilator_bed", "ventilator_priority_count within ranked_requests")
    r = await icu.prioritize_ventilator_bed(IcuAdmissionInput(SESSION_ID, ranked["ranked_requests"]))
    assert_sane_shape("prioritize_ventilator_bed", r)
    _has(r, "ranked_requests", "ventilator_priority_count")
    assert r["ventilator_priority_count"] <= len(r["ranked_requests"])


async def test_reserve_icu_admission(ranked, capsys):
    announce(capsys, "icu:reserve_icu_admission", "reserves top-ranked admission (approval_id + patient_token)")
    r = await icu.reserve_icu_admission(IcuAdmissionInput(SESSION_ID, ranked["ranked_requests"]))
    assert_sane_shape("reserve_icu_admission", r)
    _has(r, "approval_id", "patient_token")


async def test_trigger_overflow_evaluation(ranked, capsys):
    announce(capsys, "icu:trigger_overflow_evaluation", "overflow_triggered flag + patients_pending count")
    r = await icu.trigger_overflow_evaluation(IcuAdmissionInput(SESSION_ID, ranked["ranked_requests"]))
    assert_sane_shape("trigger_overflow_evaluation", r)
    _has(r, "overflow_triggered", "patients_pending")


async def test_escalate_deterioration(ranked, capsys):
    announce(capsys, "icu:escalate_deterioration", "escalated flag/count present")
    r = await icu.escalate_deterioration(IcuAdmissionInput(SESSION_ID, ranked["ranked_requests"]))
    assert_sane_shape("escalate_deterioration", r)
    _has(r, "escalated")
