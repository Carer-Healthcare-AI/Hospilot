-- ============================================================
-- 083_lab_test_volume_forecast_registry.sql
-- Registers sa_lab_test_volume (lab_agent) and its task
-- ta_forecast_test_volume -- forecasts laboratory test volume over a
-- goal-derived horizon with specimen volume, analyzer/staff utilisation
-- and overload risk, via Hospilot /lab/test-volume (util/forecast_client.py).
-- Executed by run_lab_body in workflows/graph/agents/simple.py. Distinct
-- from sa_capacity_prediction (per-analyzer next-hour util, mig 052).
-- Parent lab_agent upserted first (mirrors 014).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/083_lab_test_volume_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('lab_agent', 'Lab Operations',
   'Manages lab operations: sample tracking, TAT optimization, critical result escalation, analyzer utilization, QC compliance, test recommendations, and capacity forecasting.',
   '🧪', '#06b6d4', true, 130)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_lab_test_volume', 'lab_agent', 'Test Volume Forecast',
   'Forward-looking forecast of laboratory TEST VOLUME over a horizon inferred from the request (6h-7d), with expected specimen volume, analyzer/staff utilisation and capacity-overload risk. Include when the goal asks about predicted lab test/order volume, specimen load, or lab capacity/overload over a time horizon. Distinct from sa_capacity_prediction (per-analyzer next-hour utilisation) -- this projects overall test volume forward.',
   '["Test Volume","Specimen Load","Overload Risk"]', false, 100)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_test_volume', 'sa_lab_test_volume',
   'Forecast how many lab tests will be ordered over a horizon derived from the goal, with expected specimen volume, analyzer/staff utilisation and overload risk, from recent order volume, online analyzers and lab staffing',
   '["forecast_available","predicted_test_volume","predicted_analyzer_utilization","capacity_overload_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast how many lab tests will be ordered over a horizon derived from the goal, with expected specimen volume, analyzer/staff utilisation and overload risk, from recent order volume, online analyzers and lab staffing',
       updated_at = now()
 WHERE id = 'ta_forecast_test_volume';
