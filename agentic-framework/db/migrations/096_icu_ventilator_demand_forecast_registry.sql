-- ============================================================
-- 096_icu_ventilator_demand_forecast_registry.sql
-- Registers sa_icu_ventilator_demand (icu_agent) and its task
-- ta_forecast_icu_ventilator_demand -- forecasts ventilators clinically
-- required over a goal-derived horizon (incl. demand the current free
-- fleet cannot meet), via Hospilot /icu/ventilator-demand
-- (util/forecast_client.py). Executed by run_icu_body in
-- workflows/graph/agents/clinical.py. Parent icu_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/096_icu_ventilator_demand_forecast_registry.sql
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
  ('sa_icu_ventilator_demand', 'icu_agent', 'Ventilator Demand Forecast',
   'Forward-looking forecast of VENTILATOR DEMAND -- how many ventilators will be clinically required over a horizon inferred from the request (6h-7d), including any need the current free fleet cannot meet. Include when the goal asks about upcoming ventilator/respiratory-support demand, ventilator shortfall/capacity, or surge in ventilated patients. Distinct from sa_icu_occupancy (ICU bed census) and sa_icu_capacity_forecast (ICU admissions inflow) -- this predicts ventilator equipment need specifically.',
   '["Ventilator Demand","Respiratory Support","Ventilator Shortfall"]', false, 50)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_icu_ventilator_demand', 'sa_icu_ventilator_demand',
   'Forecast ventilators clinically required over a horizon derived from the goal, including demand the current free fleet cannot meet, with a recommended action, from patients currently on ventilators, ventilator-capable beds free and ICU census',
   '["forecast_available","predicted_ventilator_demand","unmet_ventilator_need","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast ventilators clinically required over a horizon derived from the goal, including demand the current free fleet cannot meet, with a recommended action, from patients currently on ventilators, ventilator-capable beds free and ICU census',
       updated_at = now()
 WHERE id = 'ta_forecast_icu_ventilator_demand';
