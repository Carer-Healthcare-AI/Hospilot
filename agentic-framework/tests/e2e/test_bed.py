"""Per-domain live E2E tests — bed.

Owner: TODO   ·   Status: complete (Tier A + Tier B)

Bed is the largest domain (56 tasks). This file currently covers the 20 Tier-A tasks
(session_id only): dirty-bed/overflow snapshots, transfer/discharge recommendations,
and the four bed forecasts. The 33 Tier-B tasks (bed allocation/readiness/notify chain)
are the next pass. Run: pytest tests/e2e/test_bed.py -v
"""
import pytest

from agents.bed import activities as acts, agent_activities as aa, prediction_activities as pred
from agents.bed.activities import (
    RankBedsInput, BatchAssignmentInput, BedApprovalInput, ReleaseLockInput,
)
from agents.bed.agent_activities import (
    QueryBedsInput, FilterBedsInput, SyncBedStatusInput, HoldBedInput,
    EmergencyCleaningInput, NotifyInput, BedReadinessInput, PredictSaturationInput,
)
from agents.bed.prediction_activities import CapacitySnapshotInput, BedForecastInput
from temporalio.exceptions import WorkflowAlreadyStartedError
from _helpers import SESSION_ID, announce, assert_sane_shape


def _ran_ok(r):
    """Some bed tasks are side-effecting and return None or a bare id string."""
    assert r is None or isinstance(r, (dict, list, str))


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


# --- activities --------------------------------------------------------------

async def test_find_available_beds(capsys):
    announce(capsys, "bed:find_available_beds", "returns a list of available bed records")
    r = await acts.find_available_beds(SESSION_ID)
    assert_sane_shape("find_available_beds", r)
    assert isinstance(r, list)


# --- agent_activities: snapshots ---------------------------------------------

async def test_check_dirty_icu_beds(capsys):
    announce(capsys, "bed:check_dirty_icu_beds", "dirty_count matches dirty_beds list")
    r = await aa.check_dirty_icu_beds(SESSION_ID)
    assert_sane_shape("check_dirty_icu_beds", r)
    _has(r, "dirty_beds", "dirty_count")
    assert r["dirty_count"] == len(r["dirty_beds"])


async def test_check_dirty_soon_to_release(capsys):
    announce(capsys, "bed:check_dirty_soon_to_release", "returns a beds list")
    r = await aa.check_dirty_soon_to_release(SESSION_ID)
    assert_sane_shape("check_dirty_soon_to_release", r)
    _has(r, "beds")
    assert isinstance(r["beds"], list)


async def test_check_overflow_candidates(capsys):
    announce(capsys, "bed:check_overflow_candidates", "candidates + alternate_wards are lists")
    r = await aa.check_overflow_candidates(SESSION_ID)
    assert_sane_shape("check_overflow_candidates", r)
    _has(r, "alternate_wards", "candidates")
    assert isinstance(r["candidates"], list)


async def test_check_temporary_overflow_beds(capsys):
    announce(capsys, "bed:check_temporary_overflow_beds", "candidates list + fast_track/surge counts present")
    r = await aa.check_temporary_overflow_beds(SESSION_ID)
    assert_sane_shape("check_temporary_overflow_beds", r)
    _has(r, "candidates", "fast_track_count", "surge_count")


# --- agent_activities: recommendations / triggers ----------------------------

async def test_recommend_transfer_allocation(capsys):
    announce(capsys, "bed:recommend_transfer_allocation", "returns a recommendation")
    r = await aa.recommend_transfer_allocation(SESSION_ID)
    assert_sane_shape("recommend_transfer_allocation", r)
    _has(r, "recommendation")


async def test_recommend_icu_to_ward_transfer(capsys):
    announce(capsys, "bed:recommend_icu_to_ward_transfer", "returns a recommendation")
    r = await aa.recommend_icu_to_ward_transfer(SESSION_ID)
    assert_sane_shape("recommend_icu_to_ward_transfer", r)
    _has(r, "recommendation")


async def test_allocate_overflow_bed(capsys):
    announce(capsys, "bed:allocate_overflow_bed", "returns an allocated bed_id (or None when none free)")
    r = await aa.allocate_overflow_bed(SESSION_ID)
    assert_sane_shape("allocate_overflow_bed", r)
    _has(r, "bed_id")


async def test_trigger_surge_forecast(capsys):
    announce(capsys, "bed:trigger_surge_forecast", "returns a forecast payload")
    r = await aa.trigger_surge_forecast(SESSION_ID)
    assert_sane_shape("trigger_surge_forecast", r)
    _has(r, "forecast")


async def test_predict_discharge_probability(capsys):
    announce(capsys, "bed:predict_discharge_probability", "returns per-admission discharge predictions")
    r = await aa.predict_discharge_probability(SESSION_ID)
    assert_sane_shape("predict_discharge_probability", r)
    _has(r, "predictions")
    assert isinstance(r["predictions"], list)


async def test_trigger_clearance_workflow(capsys):
    announce(capsys, "bed:trigger_clearance_workflow", "returns a triggered flag/count")
    r = await aa.trigger_clearance_workflow(SESSION_ID)
    assert_sane_shape("trigger_clearance_workflow", r)
    _has(r, "triggered")


async def test_predict_discharge_horizon(capsys):
    announce(capsys, "bed:predict_discharge_horizon", "discharge-now + freeing-in-4h/24h counts (or error)")
    r = await aa.predict_discharge_horizon(SESSION_ID)
    assert_sane_shape("predict_discharge_horizon", r)
    assert "error" in r or {"discharge_ready_now", "freeing_4h", "freeing_24h"} <= set(r)


async def test_run_surge_model(capsys):
    announce(capsys, "bed:run_surge_model", "returns a demand_forecast")
    r = await aa.run_surge_model(SESSION_ID)
    assert_sane_shape("run_surge_model", r)
    _has(r, "demand_forecast")


async def test_notify_staffing_agent(capsys):
    announce(capsys, "bed:notify_staffing_agent", "returns a notified flag/count")
    r = await aa.notify_staffing_agent(SESSION_ID)
    assert_sane_shape("notify_staffing_agent", r)
    _has(r, "notified")


async def test_recommend_overflow_zone(capsys):
    announce(capsys, "bed:recommend_overflow_zone", "returns a recommendation")
    r = await aa.recommend_overflow_zone(SESSION_ID)
    assert_sane_shape("recommend_overflow_zone", r)
    _has(r, "recommendation")


async def test_trigger_discharge_coordination(capsys):
    announce(capsys, "bed:trigger_discharge_coordination", "returns a triggered flag/count (writes audit)")
    r = await aa.trigger_discharge_coordination(SESSION_ID)
    assert_sane_shape("trigger_discharge_coordination", r)
    _has(r, "triggered")


# --- prediction_activities: forecasts ----------------------------------------

async def test_forecast_bed_turnover(capsys):
    announce(capsys, "bed:forecast_bed_turnover", "forecast envelope; per-ward turnover forecast present")
    r = await pred.forecast_bed_turnover(SESSION_ID)
    assert_sane_shape("forecast_bed_turnover", r)
    _check_forecast(r)
    if r["forecast_available"]:
        _has(r, "wards", "low_capacity_count")


async def test_forecast_bed_occupancy(capsys):
    announce(capsys, "bed:forecast_bed_occupancy", "forecast envelope; occupied/free beds non-negative when available")
    r = await pred.forecast_bed_occupancy(SESSION_ID)
    assert_sane_shape("forecast_bed_occupancy", r)
    _check_forecast(r)
    if r["forecast_available"]:
        _has(r, "predicted_occupied_beds", "predicted_free_beds", "overflow_risk")
        _nonneg(r, "predicted_occupied_beds")


async def test_forecast_bed_ward_capacity(capsys):
    announce(capsys, "bed:forecast_bed_ward_capacity", "forecast envelope; per-ward capacity + high_risk_count")
    r = await pred.forecast_bed_ward_capacity(SESSION_ID)
    assert_sane_shape("forecast_bed_ward_capacity", r)
    _check_forecast(r)
    if r["forecast_available"]:
        _has(r, "wards", "high_risk_count")


async def test_forecast_bed_isolation_demand(capsys):
    announce(capsys, "bed:forecast_bed_isolation_demand",
             "forecast envelope; isolation beds required non-negative when available")
    r = await pred.forecast_bed_isolation_demand(SESSION_ID)
    assert_sane_shape("forecast_bed_isolation_demand", r)
    _check_forecast(r)
    if r["forecast_available"]:
        _has(r, "predicted_isolation_beds_required", "isolation_capacity_status")
        _nonneg(r, "predicted_isolation_beds_required")


# =============================================================================
# Tier B — inputs built from real live outputs (beds/snapshot/assignments/dirty)
# =============================================================================

@pytest.fixture
async def beds():
    return await acts.find_available_beds(SESSION_ID)


@pytest.fixture
def bed_id(beds):
    return beds[0]["id"] if beds else "00000000-0000-0000-0000-000000000000"


@pytest.fixture
async def snapshot():
    return await pred.get_capacity_snapshot(CapacitySnapshotInput(SESSION_ID))


@pytest.fixture
async def assignments(beds):
    return await acts.find_beds_for_patients(BatchAssignmentInput(SESSION_ID, [], beds))


@pytest.fixture
async def dirty():
    r = await aa.check_dirty_icu_beds(SESSION_ID)
    return r["dirty_beds"]


# --- allocation / approval / batch -------------------------------------------

async def test_rank_beds_activity(beds, capsys):
    announce(capsys, "bed:rank_beds_activity", "ranks candidate beds and returns a result payload")
    r = await acts.rank_beds_activity(RankBedsInput(SESSION_ID, beds, {}))
    assert_sane_shape("rank_beds_activity", r)


async def test_find_beds_for_patients(assignments, capsys):
    announce(capsys, "bed:find_beds_for_patients", "returns a list of bed assignments")
    assert_sane_shape("find_beds_for_patients", assignments)
    assert isinstance(assignments, list)


async def test_create_batch_bed_approval(assignments, capsys):
    announce(capsys, "bed:create_batch_bed_approval", "creates a batch approval from assignments")
    r = await acts.create_batch_bed_approval(SESSION_ID, assignments)
    _ran_ok(r)


async def test_confirm_batch_reservations(assignments, capsys):
    announce(capsys, "bed:confirm_batch_reservations", "beds_reserved + status present")
    r = await acts.confirm_batch_reservations(SESSION_ID, assignments)
    assert_sane_shape("confirm_batch_reservations", r)
    _has(r, "beds_reserved", "status")


async def test_release_batch_locks(assignments, capsys):
    announce(capsys, "bed:release_batch_locks", "releases batch locks and returns a result")
    r = await acts.release_batch_locks(SESSION_ID, assignments)
    _ran_ok(r)


async def test_create_bed_approval(bed_id, capsys):
    announce(capsys, "bed:create_bed_approval", "creates an approval for a bed (approval_id)")
    try:
        r = await acts.create_bed_approval(BedApprovalInput(SESSION_ID, bed_id))
    except WorkflowAlreadyStartedError:
        return  # a bed_reservation approval workflow is already active this session — valid state
    assert_sane_shape("create_bed_approval", r)
    _has(r, "approval_id")


async def test_confirm_bed_reservation(bed_id, capsys):
    announce(capsys, "bed:confirm_bed_reservation", "confirms a bed reservation (bed_id + status)")
    r = await acts.confirm_bed_reservation(BedApprovalInput(SESSION_ID, bed_id))
    assert_sane_shape("confirm_bed_reservation", r)
    _has(r, "bed_id", "status")


async def test_release_bed_lock_activity(bed_id, capsys):
    announce(capsys, "bed:release_bed_lock_activity", "releases a bed lock and returns a result")
    r = await acts.release_bed_lock_activity(ReleaseLockInput(SESSION_ID, bed_id))
    _ran_ok(r)


# --- query / filters ---------------------------------------------------------

async def test_query_beds(capsys):
    announce(capsys, "bed:query_beds", "candidate_count matches candidates list; per-type counts present")
    r = await aa.query_beds(QueryBedsInput(SESSION_ID))
    assert_sane_shape("query_beds", r)
    _has(r, "candidate_count", "candidates", "icu_count", "general_count")
    assert r["candidate_count"] == len(r["candidates"])


@pytest.mark.parametrize("fn_name", [
    "filter_ventilator_beds", "filter_isolation_beds", "apply_gender_filter",
    "apply_isolation_room_filter", "trigger_alternate_ward_search",
])
async def test_bed_filters(beds, fn_name, capsys):
    announce(capsys, f"bed:{fn_name}", "returns a filtered candidates list (subset of input)")
    fn = getattr(aa, fn_name)
    r = await fn(FilterBedsInput(SESSION_ID, beds))
    assert_sane_shape(fn_name, r)
    _has(r, "candidates")
    assert isinstance(r["candidates"], list)
    assert len(r["candidates"]) <= len(beds)


# --- readiness ---------------------------------------------------------------

async def test_validate_sanitization(beds, capsys):
    announce(capsys, "bed:validate_sanitization", "passed_ids + pending_ids present")
    r = await aa.validate_sanitization(BedReadinessInput(SESSION_ID, beds))
    assert_sane_shape("validate_sanitization", r)
    _has(r, "passed", "passed_ids", "pending_ids")


async def test_mark_bed_ready(beds, capsys):
    announce(capsys, "bed:mark_bed_ready", "returns marked bed_ids")
    r = await aa.mark_bed_ready(BedReadinessInput(SESSION_ID, beds))
    assert_sane_shape("mark_bed_ready", r)
    _has(r, "bed_ids")


async def test_check_room_readiness(beds, capsys):
    announce(capsys, "bed:check_room_readiness", "ready_ids + issues present")
    r = await aa.check_room_readiness(BedReadinessInput(SESSION_ID, beds))
    assert_sane_shape("check_room_readiness", r)
    _has(r, "ready", "ready_ids", "issues")


async def test_validate_oxygen_readiness(beds, capsys):
    announce(capsys, "bed:validate_oxygen_readiness", "functional_ids + unknown_ids present")
    r = await aa.validate_oxygen_readiness(BedReadinessInput(SESSION_ID, beds))
    assert_sane_shape("validate_oxygen_readiness", r)
    _has(r, "functional", "functional_ids", "unknown_ids")


async def test_check_monitor_readiness(beds, capsys):
    announce(capsys, "bed:check_monitor_readiness", "functional_ids + unknown_ids present")
    r = await aa.check_monitor_readiness(BedReadinessInput(SESSION_ID, beds))
    assert_sane_shape("check_monitor_readiness", r)
    _has(r, "functional", "functional_ids", "unknown_ids")


async def test_sync_ready_status(beds, capsys):
    announce(capsys, "bed:sync_ready_status", "synced flag + bed_ids present")
    r = await aa.sync_ready_status(BedReadinessInput(SESSION_ID, beds))
    assert_sane_shape("sync_ready_status", r)
    _has(r, "bed_ids", "synced")


# --- sync / hold / emergency cleaning ----------------------------------------

async def test_sync_bed_status(beds, capsys):
    announce(capsys, "bed:sync_bed_status", "synced flag + bed_ids present")
    r = await aa.sync_bed_status(SyncBedStatusInput(SESSION_ID, [b["id"] for b in beds], "Available"))
    assert_sane_shape("sync_bed_status", r)
    _has(r, "bed_ids", "synced")


async def test_hold_bed_temporarily(bed_id, capsys):
    announce(capsys, "bed:hold_bed_temporarily", "held flag + bed_id present")
    r = await aa.hold_bed_temporarily(HoldBedInput(SESSION_ID, bed_id))
    assert_sane_shape("hold_bed_temporarily", r)
    _has(r, "bed_id", "held")


async def test_create_emergency_cleaning_task(dirty, capsys):
    announce(capsys, "bed:create_emergency_cleaning_task", "creates cleaning task(s) for dirty beds")
    r = await aa.create_emergency_cleaning_task(EmergencyCleaningInput(SESSION_ID, dirty))
    assert_sane_shape("create_emergency_cleaning_task", r)
    assert "created" in r or "task_ids" in r


async def test_dispatch_housekeeping_fast_track(dirty, capsys):
    announce(capsys, "bed:dispatch_housekeeping_fast_track", "dispatched count present")
    r = await aa.dispatch_housekeeping_fast_track(EmergencyCleaningInput(SESSION_ID, dirty))
    assert_sane_shape("dispatch_housekeeping_fast_track", r)
    _has(r, "dispatched")


# --- predict / snapshot / forecast -------------------------------------------

async def test_predict_icu_saturation(capsys):
    announce(capsys, "bed:predict_icu_saturation", "risk + saturation_pct present")
    r = await aa.predict_icu_saturation(PredictSaturationInput(SESSION_ID))
    assert_sane_shape("predict_icu_saturation", r)
    _has(r, "risk", "saturation_pct")


async def test_get_capacity_snapshot(snapshot, capsys):
    announce(capsys, "bed:get_capacity_snapshot", "discharge horizon + backlog counts present")
    assert_sane_shape("get_capacity_snapshot", snapshot)
    _has(snapshot, "discharge_ready_now", "discharge_4h", "discharge_24h", "critical_backlog")


async def test_run_capacity_forecast(snapshot, capsys):
    announce(capsys, "bed:run_capacity_forecast", "runs the capacity forecast on a snapshot")
    r = await pred.run_capacity_forecast(BedForecastInput(SESSION_ID, snapshot))
    assert_sane_shape("run_capacity_forecast", r)


# --- notify / escalate (NotifyInput) -----------------------------------------

@pytest.mark.parametrize("fn_name,key", [
    ("escalate_to_floor_supervisor", "escalated"),
    ("notify_biomedical_team", "notified"),
    ("create_equipment_task", "task_id"),
    ("generate_capacity_alert", "alert_sent"),
    ("recommend_overflow_strategy", "strategy"),
    ("notify_discharge_team", "notified"),
    ("alert_operations_team", "notified"),
    ("escalate_to_command_center", "escalated"),
    ("escalate_allocation_conflict", "escalated"),
])
async def test_bed_notify_tasks(fn_name, key, capsys):
    announce(capsys, f"bed:{fn_name}", f"returns '{key}' in its result")
    fn = getattr(aa, fn_name)
    r = await fn(NotifyInput(SESSION_ID, message="e2e test", payload={}))
    assert_sane_shape(fn_name, r)
    _has(r, key)
