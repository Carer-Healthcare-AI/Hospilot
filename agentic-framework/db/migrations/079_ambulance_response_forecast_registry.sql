-- ============================================================
-- 079_ambulance_response_forecast_registry.sql
-- Registers sa_ambulance_response (ambulance_agent) and its task
-- ta_forecast_ambulance_response -- forecasts the service-average
-- ambulance response time (minutes) over a goal-derived horizon with
-- 8-minute SLA breach risk, via Hospilot /ambulance/response-time
-- (util/forecast_client.py). Executed by run_ambulance_body in
-- workflows/graph/agents/ambulance.py. Parent ambulance_agent is
-- upserted first (the 009 seed targeted the pre-005 `hospilot` schema
-- and may not have carried into hospilot_app in every DB -- avoids the
-- FK trap).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/079_ambulance_response_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('ambulance_agent', 'Ambulance Agent',
   'Assigns the best available ambulance unit, surfaces ETA and crew details, and flags emergency escalation for critical cases',
   '🚑', '#ef4444', true, 200)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_ambulance_response', 'ambulance_agent', 'Response Time Forecast',
   'Forward-looking forecast of the service-average ambulance RESPONSE TIME (minutes) over a horizon inferred from the request (3h-7d), with 8-minute SLA breach risk. Include when the goal asks about expected/predicted ambulance response times, EMS SLA risk, or fleet responsiveness over a time horizon. Distinct from live dispatch (sa_ambulance_dispatch), which assigns a unit to one call now.',
   '["Response Time","SLA Breach Risk","Fleet Responsiveness"]', false, 30)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_ambulance_response', 'sa_ambulance_response',
   'Forecast the service-average ambulance response time (minutes) over a horizon derived from the goal, with 8-minute SLA breach risk, from available and active units in the live fleet',
   '["forecast_available","predicted_response_minutes","target_breach_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast the service-average ambulance response time (minutes) over a horizon derived from the goal, with 8-minute SLA breach risk, from available and active units in the live fleet',
       updated_at = now()
 WHERE id = 'ta_forecast_ambulance_response';
