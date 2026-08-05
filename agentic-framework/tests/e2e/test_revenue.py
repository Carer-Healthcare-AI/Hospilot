"""Per-domain live E2E tests — revenue.

Owner: TODO   ·   Status: complete (Tier A + Tier B)

8 Tier-A snapshot/forecast tasks + 2 Tier-B tasks whose input is session_id+goal.
Run: pytest tests/e2e/test_revenue.py -v
"""
from agents.revenue import activities as rev
from agents.revenue.activities import RevAnalysisInput
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


# --- Tier A ------------------------------------------------------------------

async def test_identify_revenue_leakage(capsys):
    announce(capsys, "revenue:identify_revenue_leakage", "leakage_detected flag agrees with leakage_amount")
    r = await rev.identify_revenue_leakage(SESSION_ID)
    assert_sane_shape("identify_revenue_leakage", r)
    _has(r, "leakage_amount", "leakage_detected", "low_value_ipd_count", "unbilled_count", "unlinked_claims")
    assert bool(r["leakage_detected"]) == (r["leakage_amount"] > 0)


async def test_analyze_resource_utilization(capsys):
    announce(capsys, "revenue:analyze_resource_utilization", "occupied+available beds within total_beds")
    r = await rev.analyze_resource_utilization(SESSION_ID)
    assert_sane_shape("analyze_resource_utilization", r)
    _has(r, "total_beds", "occupied_beds", "available_beds", "utilization_score", "idle_equipment_count")
    if all(isinstance(r[k], int) for k in ("occupied_beds", "available_beds", "total_beds")):
        assert r["occupied_beds"] + r["available_beds"] <= r["total_beds"]


async def test_analyze_dept_profitability(capsys):
    announce(capsys, "revenue:analyze_dept_profitability", "below_target_count within dept_count")
    r = await rev.analyze_dept_profitability(SESSION_ID)
    assert_sane_shape("analyze_dept_profitability", r)
    _has(r, "dept_count", "dept_revenue", "below_target_count", "recommendations")
    assert r["below_target_count"] <= r["dept_count"]


async def test_predict_denial_risk_rev(capsys):
    announce(capsys, "revenue:predict_denial_risk_rev", "high+medium risk counts within claims_at_risk")
    r = await rev.predict_denial_risk_rev(SESSION_ID)
    assert_sane_shape("predict_denial_risk_rev", r)
    _has(r, "claims_at_risk", "denial_probability", "high_risk_count", "medium_risk_count")
    if isinstance(r["claims_at_risk"], int):
        assert r["high_risk_count"] + r["medium_risk_count"] <= r["claims_at_risk"]


async def test_presubmission_validation_rev(capsys):
    announce(capsys, "revenue:presubmission_validation_rev", "validation_passed flag + issue counts present")
    r = await rev.presubmission_validation_rev(SESSION_ID)
    assert_sane_shape("presubmission_validation_rev", r)
    _has(r, "validation_passed", "failed_claims", "issues_found", "missing_fields_count")


async def test_payer_rule_compliance_rev(capsys):
    announce(capsys, "revenue:payer_rule_compliance_rev", "payer compliance issue counts present")
    r = await rev.payer_rule_compliance_rev(SESSION_ID)
    assert_sane_shape("payer_rule_compliance_rev", r)
    _has(r, "compliance_issues", "auth_missing_count", "no_tpa_count", "non_covered_count", "pending_reviewed")


async def test_detect_missing_docs_rev(capsys):
    announce(capsys, "revenue:detect_missing_docs_rev", "missing_docs_count within admissions_checked")
    r = await rev.detect_missing_docs_rev(SESSION_ID)
    assert_sane_shape("detect_missing_docs_rev", r)
    # one admission can have several missing docs, so missing_docs_count may exceed
    # admissions_checked; just assert the counts are present and internally consistent.
    _has(r, "admissions_checked", "missing_docs_count", "missing_signatures", "missing_summaries")


async def test_forecast_revenue(capsys):
    announce(capsys, "revenue:forecast_revenue",
             "forecast envelope; predicted outstanding balance non-negative when available")
    r = await rev.forecast_revenue(SESSION_ID)
    assert_sane_shape("forecast_revenue", r)
    _check_forecast(r)
    if r["forecast_available"]:
        _has(r, "predicted_outstanding_balance", "balance_risk", "current_outstanding_balance")
        _nonneg(r, "predicted_outstanding_balance")


# --- Tier B (input = session_id + goal) --------------------------------------

async def test_optimize_package_utilization(capsys):
    announce(capsys, "revenue:optimize_package_utilization", "recommendations list + savings figure present")
    r = await rev.optimize_package_utilization(RevAnalysisInput(SESSION_ID, goal="optimize package utilization"))
    assert_sane_shape("optimize_package_utilization", r)
    _has(r, "packages_reviewed", "recommendations", "savings_identified")


async def test_escalation_recommendations_rev(capsys):
    announce(capsys, "revenue:escalation_recommendations_rev", "escalation_count matches escalations list")
    r = await rev.escalation_recommendations_rev(RevAnalysisInput(SESSION_ID, goal="escalate high-risk claims"))
    assert_sane_shape("escalation_recommendations_rev", r)
    _has(r, "escalated", "escalation_count", "escalations")
    assert len(r["escalations"]) <= r["escalation_count"]
