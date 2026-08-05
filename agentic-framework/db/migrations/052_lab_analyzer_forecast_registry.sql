-- ============================================================
-- 052_lab_analyzer_forecast_registry.sql
-- Registers ta_forecast_analyzer_util under sa_capacity_prediction (lab_agent).
-- ML next-hour analyzer utilization forecast via Hospilot /lab/analyzer-util.
-- Reference integration for the forecast API (util/forecast_client.py).
-- Idempotent via ON CONFLICT (id) DO NOTHING.
-- ============================================================

INSERT INTO "hospilot_app".task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  ('ta_forecast_analyzer_util', 'sa_capacity_prediction', 'ML forecast of next-hour utilization per analyzer (Hospilot /lab/analyzer-util)', '["analyzers_forecast","critical_count","forecast_available"]', true, false, 25)
ON CONFLICT (id) DO NOTHING;
