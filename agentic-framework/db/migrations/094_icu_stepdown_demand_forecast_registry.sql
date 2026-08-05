-- ============================================================
-- 094_icu_stepdown_demand_forecast_registry.sql
-- Registers sa_icu_stepdown_demand (icu_agent) and its task
-- ta_forecast_icu_stepdown_demand -- forecasts ICU patients becoming
-- ready for a ward (step-down) bed over a goal-derived horizon with a
-- ward-absorption status, via Hospilot /icu/stepdown-demand
-- (util/forecast_client.py). Executed by run_icu_body in
-- workflows/graph/agents/clinical.py. Parent icu_agent upserted first.
--
-- NOTE: /icu/stepdown-demand is a DEPRECATED forecast endpoint (the API
-- recommends /icu/transfer-forecast, "same quantity, rebuilt model").
-- Integrated on explicit request; migrate the activity to
-- /icu/transfer-forecast when its required capacity inputs are sourceable.
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/094_icu_stepdown_demand_forecast_registry.sql
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
  ('sa_icu_stepdown_demand', 'icu_agent', 'Step-down Demand Forecast',
   'Forward-looking forecast of ICU STEP-DOWN DEMAND -- how many ICU patients will become ready to move to a general/step-down WARD bed over a horizon inferred from the request (6h-7d), with a ward-absorption status. Include when the goal asks about upcoming ICU-to-ward step-down/transfer volume or whether wards can absorb ICU step-downs. Distinct from sa_icu_occupancy (ICU census) and sa_icu_stepdown (the live per-patient step-down transfer action). NOTE: backed by a deprecated forecast endpoint (/icu/stepdown-demand) pending migration to /icu/transfer-forecast.',
   '["ICU Step-down","Ward Transfer Demand","ICU-to-Ward Flow"]', false, 40)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_icu_stepdown_demand', 'sa_icu_stepdown_demand',
   'Forecast ICU patients becoming ready for a ward (step-down) bed over a horizon derived from the goal, with a ward-absorption status and recommended action, from ICU census, available ward beds, ward occupancy and ward staffing',
   '["forecast_available","predicted_step_down_bed_demand","ward_status","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast ICU patients becoming ready for a ward (step-down) bed over a horizon derived from the goal, with a ward-absorption status and recommended action, from ICU census, available ward beds, ward occupancy and ward staffing',
       updated_at = now()
 WHERE id = 'ta_forecast_icu_stepdown_demand';
