-- ============================================================
-- 085_er_wait_time_forecast_registry.sql
-- Registers sa_er_wait_time (er_agent) and its task
-- ta_forecast_er_wait_time -- forecasts the average ER wait time
-- (minutes) a chosen time ahead with wait status and 8-minute
-- target-breach risk, via Hospilot /er/wait-time (util/forecast_client.py).
-- Executed by run_er_body in workflows/graph/agents/simple.py. Sibling of
-- sa_er_surge_prediction (arrival volume, mig 053). Parent er_agent upserted
-- first (pre-005 seed may not have carried into hospilot_app in every DB).
-- NOTE: doctors_on_duty/nurses_on_duty are HOSPITAL-WIDE proxies (ER is not a
-- distinct roster area) -- see agents/er/surge_prediction.forecast_er_wait_time.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/085_er_wait_time_forecast_registry.sql
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
  ('sa_er_wait_time', 'er_agent', 'Wait Time Forecast',
   'Forward-looking forecast of the average ER WAIT TIME (minutes) a chosen time ahead (3h-7d), with wait status and 8-minute target-breach risk. Include when the goal asks about predicted/expected ER wait times, door-to-doctor time, or SLA/target-breach risk over a time horizon. Distinct from sa_er_surge_prediction (predicts arrival VOLUME) and the live Triage Monitor.',
   '["ER Wait Time","Target Breach Risk","Door-to-Doctor"]', false, 60)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_er_wait_time', 'sa_er_wait_time',
   'Forecast the average ER wait time (minutes) a chosen time ahead derived from the goal, with wait status and 8-minute target-breach risk, from the waiting queue, patients in the ED, recent arrivals and doctor/nurse staffing',
   '["forecast_available","predicted_wait_minutes","wait_status","target_breach_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast the average ER wait time (minutes) a chosen time ahead derived from the goal, with wait status and 8-minute target-breach risk, from the waiting queue, patients in the ED, recent arrivals and doctor/nurse staffing',
       updated_at = now()
 WHERE id = 'ta_forecast_er_wait_time';
