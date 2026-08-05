-- ============================================================
-- 055_pharmacy_demand_forecast_registry.sql
-- Re-points the existing ta_forecast_pharmacy_demand task (under
-- sa_pharmacy_capacity, pharmacy_agent) from the old Claude/LLM
-- heuristic forecast to the per-drug demand forecast served by
-- Hospilot /pharmacy/demand (util/forecast_client.py).
-- Only the label + outputs change; the task id / subagent stay put,
-- so no new registry rows and no FK concerns.
-- Python fallback parity lives in SUB_AGENTS['pharmacy_agent'] in
-- workflows/planner.py and agents/pharmacy/capacity.py::forecast_pharmacy_demand.
-- Idempotent -- safe to re-run.
-- ============================================================

UPDATE hospilot_app.task_registry
SET label   = 'Forecast of next-day dispensing demand per drug',
    outputs = '["forecast_available","forecast_orders","surge_expected"]'
WHERE id = 'ta_forecast_pharmacy_demand';
