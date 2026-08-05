-- ============================================================
-- 089_er_boarding_forecast_registry.sql
-- Registers sa_er_boarding_forecast (er_agent) and its task
-- ta_forecast_er_boarding -- forecasts admitted patients boarding in
-- the ED awaiting inpatient beds over a goal-derived horizon, with
-- boarding time and risk, via Hospilot /er/boarding (util/forecast_client.py).
-- Executed by run_er_body in workflows/graph/agents/simple.py. Distinct from
-- the live Boarding Monitor (sa_er_boarding). Parent er_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/089_er_boarding_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('er_agent', 'ER Coordination',
   'Monitors emergency patients, assigns urgency scores, and routes patients to the right care setting',
   '🚑', '#ef4444', true, 30)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_er_boarding_forecast', 'er_agent', 'Boarding Forecast',
   'Forward-looking forecast of ED BOARDING — admitted patients stuck in the ED awaiting inpatient beds — over a horizon inferred from the request (6h-7d), with predicted boarding time and risk. Include when the goal asks about future boarding/bed-block, ED-to-inpatient flow, or boarding risk over a time horizon. Distinct from the live Boarding Monitor (sa_er_boarding, current boarders) and sa_er_wait_time (door-to-doctor wait).',
   '["ED Boarding","Bed Block","Boarding Risk"]', false, 70)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_er_boarding', 'sa_er_boarding_forecast',
   'Forecast admitted patients boarding in the ED awaiting inpatient beds over a horizon derived from the goal, with predicted boarding time and risk, from current boarders, available inpatient beds and hospital census',
   '["forecast_available","predicted_boarding_patients","boarding_status","boarding_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast admitted patients boarding in the ED awaiting inpatient beds over a horizon derived from the goal, with predicted boarding time and risk, from current boarders, available inpatient beds and hospital census',
       updated_at = now()
 WHERE id = 'ta_forecast_er_boarding';
