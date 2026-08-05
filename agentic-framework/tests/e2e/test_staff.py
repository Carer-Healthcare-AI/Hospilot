"""Per-domain live E2E tests — staff.

Owner: TODO   ·   Status: complete (Tier A + Tier B)

Tier-A: ward/hourly/doc snapshots + two forecasts. Tier-B `inp` built by chaining:
get_ward_workload -> analyze_staff_workload -> approval/confirm.
Run: pytest tests/e2e/test_staff.py -v
"""
import pytest

from agents.staff import activities as s
from agents.staff.activities import (
    StaffAnalysisInput, StaffApprovalInput, StaffConfirmInput, AreaStaffingInput,
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


@pytest.fixture
async def ward_workload():
    return await s.get_ward_workload(SESSION_ID)


@pytest.fixture
async def analysis(ward_workload):
    return await s.analyze_staff_workload(StaffAnalysisInput(SESSION_ID, ward_workload))


# --- Tier A ------------------------------------------------------------------

async def test_get_ward_workload(ward_workload, capsys):
    announce(capsys, "staff:get_ward_workload", "list of per-ward workload records")
    assert_sane_shape("get_ward_workload", ward_workload)
    assert isinstance(ward_workload, list)
    assert all(isinstance(w, dict) and "ward" in w for w in ward_workload)


async def test_get_hourly_workload(capsys):
    announce(capsys, "staff:get_hourly_workload", "by_hour + peak/understaffed hours; total_tasks non-negative")
    r = await s.get_hourly_workload(SESSION_ID)
    assert_sane_shape("get_hourly_workload", r)
    _has(r, "by_hour", "peak_hours", "total_tasks", "understaffed_hours")
    _nonneg(r, "total_tasks")


async def test_get_documentation_gaps(capsys):
    announce(capsys, "staff:get_documentation_gaps", "has_gaps flag agrees with flagged_wards")
    r = await s.get_documentation_gaps(SESSION_ID)
    assert_sane_shape("get_documentation_gaps", r)
    _has(r, "by_ward", "documentation_tasks_overdue", "documentation_tasks_pending", "flagged_wards", "has_gaps")
    assert bool(r["has_gaps"]) == (len(r["flagged_wards"]) > 0)


async def test_forecast_nurse_demand(capsys):
    announce(capsys, "staff:forecast_nurse_demand", "forecast envelope; required nurses non-negative when available")
    r = await s.forecast_nurse_demand(SESSION_ID)
    assert_sane_shape("forecast_nurse_demand", r)
    _check_forecast(r)
    if r["forecast_available"]:
        _has(r, "predicted_required_nurses", "staffing_status", "staffing_gap")
        _nonneg(r, "predicted_required_nurses")


async def test_forecast_doctor_demand(capsys):
    announce(capsys, "staff:forecast_doctor_demand", "forecast envelope; required doctors non-negative when available")
    r = await s.forecast_doctor_demand(SESSION_ID)
    assert_sane_shape("forecast_doctor_demand", r)
    _check_forecast(r)
    if r["forecast_available"]:
        _has(r, "predicted_required_doctors", "staffing_status", "staffing_gap")
        _nonneg(r, "predicted_required_doctors")


# --- Tier B (chained inputs) -------------------------------------------------

async def test_analyze_staff_workload(analysis, capsys):
    announce(capsys, "staff:analyze_staff_workload", "produces recommendations + high_pressure_wards lists + summary")
    assert_sane_shape("analyze_staff_workload", analysis)
    _has(analysis, "recommendations", "high_pressure_wards", "summary")
    assert isinstance(analysis["recommendations"], list)
    assert isinstance(analysis["high_pressure_wards"], list)


async def test_create_staff_approval(analysis, capsys):
    announce(capsys, "staff:create_staff_approval", "creates an approval from staffing recommendations")
    r = await s.create_staff_approval(StaffApprovalInput(
        SESSION_ID, analysis["recommendations"], analysis["high_pressure_wards"], analysis["summary"],
    ))
    assert_sane_shape("create_staff_approval", r)
    _has(r, "approval_id")


async def test_confirm_staff_recommendation(analysis, capsys):
    announce(capsys, "staff:confirm_staff_recommendation", "confirms staffing analysis (status + recommendations)")
    r = await s.confirm_staff_recommendation(StaffConfirmInput(SESSION_ID, analysis))
    assert_sane_shape("confirm_staff_recommendation", r)
    _has(r, "recommendations", "status")


async def test_get_area_staffing(capsys):
    announce(capsys, "staff:get_area_staffing", "areas_assessed + understaffed_areas present (empty-area path)")
    r = await s.get_area_staffing(AreaStaffingInput(SESSION_ID, areas=[]))
    assert_sane_shape("get_area_staffing", r)
    _has(r, "areas", "areas_assessed", "shift", "understaffed_areas")
