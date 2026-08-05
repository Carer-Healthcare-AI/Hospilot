-- ============================================================
-- 072_bed_occupancy_forecast_registry.sql
-- Registers sa_bed_occupancy (bed_prediction_agent) and its task
-- ta_forecast_bed_occupancy -- whole-hospital predicted census +
-- overflow risk at a goal-derived horizon via Hospilot /bed/occupancy
-- (util/forecast_client.py). Follows 054 (bed/turnover). Executed by
-- run_bed_prediction_body in workflows/graph/agents/simple.py. As with 054,
-- the Python fallback catalog groups the bed-prediction sub-agents under the
-- SUB_AGENTS['bed_agent'] key, but the DB registry and registry.py dispatch own
-- them under bed_prediction_agent -- the DB is the runtime source of truth.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/072_bed_occupancy_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- Sub-agent ordering under bed_prediction_agent:
--   census(10) -> forecast(20) -> turnover(30) -> occupancy(40).
-- ============================================================

-- 0. Ensure the parent agent exists in hospilot_app (idempotent; no-op if present).
INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('bed_prediction_agent', 'Bed Prediction',
   'Analyses current bed usage and predicts capacity pressures over the next 4–24 hours (forecast only, no placement)',
   '📊', '#0284c7', true, 90)
ON CONFLICT (id) DO NOTHING;

-- 1. New whole-hospital bed-occupancy forecast sub-agent under bed_prediction_agent.
INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_bed_occupancy', 'bed_prediction_agent', 'Bed Occupancy Forecast',
   'Whole-hospital forward census forecast: predicts total occupied beds, free beds and overflow risk (Low/Medium/High) at a horizon inferred from the request (3h–7d). Include when the goal asks how full the hospital will be, overflow/surge risk, or census planning over a time horizon. Distinct from sa_bed_turnover (per-ward beds freeing next shift) and sa_bed_pred_forecast (narrative risk).',
   '["Bed Occupancy","Census Forecast","Overflow Risk"]', false, 40)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

-- 2. The forecast task under the new sub-agent.
INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_bed_occupancy', 'sa_bed_occupancy',
   'Whole-hospital predicted bed census and overflow risk at a horizon derived from the goal, from current occupancy, expected ER admissions and discharge outlook',
   '["forecast_available","predicted_occupancy_percent","overflow_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

-- Keep the label in sync when the row already exists (INSERT above is DO NOTHING).
UPDATE "hospilot_app".task_registry
   SET label      = 'Whole-hospital predicted bed census and overflow risk at a horizon derived from the goal, from current occupancy, expected ER admissions and discharge outlook',
       updated_at = now()
 WHERE id = 'ta_forecast_bed_occupancy';
