-- ============================================================
-- 073_icu_occupancy_forecast_registry.sql
-- Registers sa_icu_occupancy (icu_agent) and its task
-- ta_forecast_icu_occupancy -- ICU CENSUS forecast (occupied/free
-- beds + overflow risk) at a goal-derived horizon via Hospilot
-- /icu/occupancy (util/forecast_client.py). The census twin of
-- 056 (sa_icu_capacity_forecast / /icu/demand, which predicts inflow).
-- Executed by run_icu_body in workflows/graph/agents/clinical.py.
-- Parent icu_agent is upserted first (pre-005 seed may not have
-- carried into hospilot_app in every DB -- avoids the FK trap).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/073_icu_occupancy_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('icu_agent', 'ICU Operations',
   'Monitors ICU capacity, tracks ventilated patients, and identifies patients ready for step-down',
   '🫀', '#dc2626', true, 20)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_icu_occupancy', 'icu_agent', 'ICU Occupancy Forecast',
   'Forward-looking ICU CENSUS forecast: predicts how many ICU beds will be occupied, free beds and overflow risk (Low/Medium/High) at a horizon inferred from the request (3h–7d). Include when the goal asks how full the ICU will be, ICU overflow/saturation risk, or ICU census planning over a time horizon. Distinct from sa_icu_capacity_forecast (predicts inflow/admissions, not census) and from the live census/ranking work.',
   '["ICU Occupancy","Census Forecast","Overflow Risk"]', false, 50)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_icu_occupancy', 'sa_icu_occupancy',
   'Forecast ICU census (occupied and free beds) and overflow risk at a horizon derived from the goal, from current ICU occupancy and critical patients awaiting ICU',
   '["forecast_available","predicted_occupancy_percent","overflow_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast ICU census (occupied and free beds) and overflow risk at a horizon derived from the goal, from current ICU occupancy and critical patients awaiting ICU',
       updated_at = now()
 WHERE id = 'ta_forecast_icu_occupancy';
