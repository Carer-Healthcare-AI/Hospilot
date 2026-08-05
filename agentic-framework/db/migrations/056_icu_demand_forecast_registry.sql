-- ============================================================
-- 056_icu_demand_forecast_registry.sql
-- Registers sa_icu_capacity_forecast (icu_agent) and its task
-- ta_forecast_icu_demand -- next-24h ICU admissions forecast via
-- Hospilot /icu/demand (util/forecast_client.py).
-- Executed by run_icu_body in workflows/graph/agents/clinical.py.
-- Parent icu_agent is upserted first (pre-005 seed may not have
-- carried into hospilot_app in every DB -- avoids the FK trap).
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
  ('sa_icu_capacity_forecast', 'icu_agent', 'ICU Demand Forecast',
   'Forward-looking forecast of how many ICU admissions to expect in the next 24 hours (drives overnight bed allocation and anaesthetist cover). Include when the goal is about anticipating ICU demand, tomorrow''s expected admissions, or proactive capacity planning. Independent of the current census/ranking work -- it predicts inflow, not who is in ICU now.',
   '["ICU Demand","24h Admissions","Capacity Planning"]', false, 40)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_icu_demand', 'sa_icu_capacity_forecast',
   'Forecast expected ICU admissions over the next 24 hours, with a capacity alert',
   '["forecast_available","predicted_admissions_24h","capacity_alert"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast expected ICU admissions over the next 24 hours, with a capacity alert',
       updated_at = now()
 WHERE id = 'ta_forecast_icu_demand';
