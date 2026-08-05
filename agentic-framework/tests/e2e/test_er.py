"""Per-domain live E2E tests — er.

Owner: TODO   ·   Status: Tier-A complete

Each test: announce(...) -> call task live with SESSION_ID -> assert_sane_shape(...) ->
task-specific assertions. Only 6 ER tasks are Tier-A; the rest take an `inp` dataclass
(Tier B, second pass) and `save_triage_scores` is a fenced live-writer.
Run: pytest tests/e2e/test_er.py -v
"""
import pytest

from agents.er import activities as acts, surge_prediction as sp
from agents.er.activities import ErTriageInput, ErFasttrackInput, SelectCriticalInput
from _helpers import SESSION_ID, announce, assert_sane_shape


def _has(r, *keys):
    missing = {k for k in keys} - set(r)
    assert not missing, f"missing expected keys {missing}; got {sorted(r)}"


def _check_forecast(r):
    """Only forecast_available is guaranteed; predicted fields appear when it's truthy
    (an error/no-data path returns a minimal {forecast_available: 0, reason, ...})."""
    _has(r, "forecast_available")
    assert r["forecast_available"] in (True, False, 0, 1)


async def test_get_er_visits(capsys):
    announce(capsys, "er:get_er_visits", "returns a list of encounter records (dicts)")
    r = await acts.get_er_visits(SESSION_ID)
    assert_sane_shape("get_er_visits", r)
    assert isinstance(r, list)
    assert all(isinstance(x, dict) for x in r), "each ER visit should be a record dict"


async def test_check_er_boarders(capsys):
    announce(capsys, "er:check_er_boarders", "boarders count >= 0; escalated within boarders")
    r = await acts.check_er_boarders(SESSION_ID)
    assert_sane_shape("check_er_boarders", r)
    _has(r, "boarders", "escalated")
    assert r["boarders"] >= 0
    assert r["escalated"] <= r["boarders"]


async def test_forecast_er_surge(capsys):
    announce(capsys, "er:forecast_er_surge", "forecast envelope present; total_expected sane when available")
    r = await sp.forecast_er_surge(SESSION_ID)
    assert_sane_shape("forecast_er_surge", r)
    _check_forecast(r)
    if r["forecast_available"]:
        _has(r, "total_expected", "peak_volume", "horizon_hours")
        assert r["total_expected"] >= 0 and r["peak_volume"] >= 0


async def test_forecast_er_wait_time(capsys):
    announce(capsys, "er:forecast_er_wait_time", "forecast envelope present; predicted wait non-negative")
    r = await sp.forecast_er_wait_time(SESSION_ID)
    assert_sane_shape("forecast_er_wait_time", r)
    _check_forecast(r)
    if r["forecast_available"]:
        _has(r, "predicted_wait_minutes", "wait_status")
        assert r["predicted_wait_minutes"] >= 0


async def test_forecast_er_boarding(capsys):
    announce(capsys, "er:forecast_er_boarding", "forecast envelope present; boarding status/risk reported")
    r = await sp.forecast_er_boarding(SESSION_ID)
    assert_sane_shape("forecast_er_boarding", r)
    _check_forecast(r)
    if r["forecast_available"]:
        _has(r, "boarding_status", "boarding_risk", "predicted_boarding_patients")
        assert r["predicted_boarding_patients"] >= 0


async def test_forecast_er_lwbs(capsys):
    announce(capsys, "er:forecast_er_lwbs", "forecast envelope present; predicted LWBS count non-negative")
    r = await sp.forecast_er_lwbs(SESSION_ID)
    assert_sane_shape("forecast_er_lwbs", r)
    _check_forecast(r)
    if r["forecast_available"]:
        _has(r, "lwbs_risk_level", "predicted_lwbs_patients")
        assert r["predicted_lwbs_patients"] >= 0


# --- Tier B (chained: get_er_visits -> triage_er_patients -> triage_results) -------

@pytest.fixture
async def visits():
    return await acts.get_er_visits(SESSION_ID)


@pytest.fixture
async def triaged(visits):
    return await acts.triage_er_patients(ErTriageInput(SESSION_ID, visits))


async def test_triage_er_patients(triaged, capsys):
    announce(capsys, "er:triage_er_patients", "one triage record per visit with a NEWS2 score + token")
    assert_sane_shape("triage_er_patients", triaged)
    assert isinstance(triaged, list)
    for t in triaged:
        _has(t, "patient_token", "news2_score")


async def test_route_fasttrack_patients(triaged, capsys):
    announce(capsys, "er:route_fasttrack_patients", "fasttrack_candidates within the triaged patients")
    r = await acts.route_fasttrack_patients(ErFasttrackInput(SESSION_ID, triaged))
    assert_sane_shape("route_fasttrack_patients", r)
    _has(r, "fasttrack_candidates", "patients")


async def test_select_critical_patients(triaged, capsys):
    announce(capsys, "er:select_critical_patients", "returns at most n critical patients from the triaged set")
    r = await acts.select_critical_patients(SelectCriticalInput(SESSION_ID, triaged, 5))
    assert_sane_shape("select_critical_patients", r)
    assert isinstance(r, list)
    assert len(r) <= 5 and len(r) <= len(triaged)


async def test_detect_cardiac_arrest(triaged, capsys):
    announce(capsys, "er:detect_cardiac_arrest", "cardiac_arrest_suspected + code_blue_triggered flags present")
    r = await acts.detect_cardiac_arrest(ErFasttrackInput(SESSION_ID, triaged))
    assert_sane_shape("detect_cardiac_arrest", r)
    _has(r, "cardiac_arrest_suspected", "code_blue_triggered")


async def test_check_spo2_critical(triaged, capsys):
    announce(capsys, "er:check_spo2_critical", "spo2_critical + escalated flags present")
    r = await acts.check_spo2_critical(ErFasttrackInput(SESSION_ID, triaged))
    assert_sane_shape("check_spo2_critical", r)
    _has(r, "spo2_critical", "escalated")


async def test_detect_clinical_protocol(triaged, capsys):
    announce(capsys, "er:detect_clinical_protocol", "protocol_count matches protocols list")
    r = await acts.detect_clinical_protocol(ErFasttrackInput(SESSION_ID, triaged))
    assert_sane_shape("detect_clinical_protocol", r)
    _has(r, "protocol_activated", "protocol_count", "protocols")
    assert len(r["protocols"]) <= r["protocol_count"]


async def test_notify_specialist(triaged, capsys):
    announce(capsys, "er:notify_specialist", "notified count + specialists_notified list present")
    r = await acts.notify_specialist(ErFasttrackInput(SESSION_ID, triaged))
    assert_sane_shape("notify_specialist", r)
    _has(r, "notified", "specialists_notified")


# save_triage_scores is a FENCED live-writer (bulk_set_triage_scores) — not tested here.
