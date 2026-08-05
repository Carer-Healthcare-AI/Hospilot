-- ============================================================
-- 097_icu_los_forecast_registry.sql
-- Registers sa_icu_los (icu_agent) and its task ta_forecast_icu_los --
-- forecasts the average ICU length of stay (days) at a goal-derived
-- horizon with a trend vs the current average, via Hospilot
-- /icu/los-forecast (util/forecast_client.py). Executed by run_icu_body
-- in workflows/graph/agents/clinical.py. Parent icu_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/097_icu_los_forecast_registry.sql
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
  ('sa_icu_los', 'icu_agent', 'ICU LOS Forecast',
   'Forward-looking forecast of the average ICU LENGTH OF STAY (days) at a horizon inferred from the request (6h-7d), with the trend vs the current average (rising/steady/falling) and an extended-stay flag. Include when the goal asks about ICU LOS, how long patients will stay, expected ICU stay duration, or ICU turnover/throughput over a time horizon. Distinct from sa_icu_occupancy (bed census) and sa_icu_capacity_forecast (admissions inflow) -- this predicts stay DURATION.',
   '["ICU LOS","Stay Duration","ICU Turnover"]', false, 60)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_icu_los', 'sa_icu_los',
   'Forecast the average ICU length of stay (days) at a horizon derived from the goal, with the trend vs the current average and a recommended action, from current mean LOS, ICU census, ventilated patients and bed occupancy',
   '["forecast_available","predicted_average_los","los_trend","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast the average ICU length of stay (days) at a horizon derived from the goal, with the trend vs the current average and a recommended action, from current mean LOS, ICU census, ventilated patients and bed occupancy',
       updated_at = now()
 WHERE id = 'ta_forecast_icu_los';
