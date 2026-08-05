-- ============================================================
-- 104_ambulance_fleet_utilization_forecast_registry.sql
-- Registers sa_ambulance_fleet_utilization (ambulance_agent) and its task
-- ta_forecast_ambulance_fleet_utilization -- forecasts the worst-hour
-- percent of the ambulance fleet committed over a goal-derived horizon
-- (plus saturated hours), via Hospilot /ambulance/fleet-utilization
-- (util/forecast_client.py). Executed by run_ambulance_body in
-- workflows/graph/agents/ambulance.py. Parent ambulance_agent upserted first.
--
-- NOTE (build-ahead-of-fix, 2026-08): the deployed /ambulance/fleet-utilization
-- route is malformed -- it ignores the documented JSON body and 422s demanding
-- query params (predicted_raw/fleet/usable). Until the service route is fixed to
-- accept the body, this forecast degrades to forecast_available: 0 (graceful); it
-- works unchanged once the endpoint is corrected. Integrated on explicit request.
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/104_ambulance_fleet_utilization_forecast_registry.sql
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
  ('sa_ambulance_fleet_utilization', 'ambulance_agent', 'Fleet Utilization Forecast',
   'Forward-looking forecast of ambulance FLEET UTILIZATION -- the worst-hour share of the fleet committed over a horizon inferred from the request (3h-7d), plus the number of saturated hours. Include when the goal asks about fleet capacity/saturation, how stretched the ambulance fleet will be, or peak committed-unit share over a time horizon. Distinct from sa_ambulance_response (predicts response MINUTES) and live dispatch. NOTE: the forecast-service route is currently malformed and returns no forecast until fixed (degrades gracefully).',
   '["Fleet Utilization","Fleet Saturation","EMS Capacity"]', false, 20)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_ambulance_fleet_utilization', 'sa_ambulance_fleet_utilization',
   'Forecast the worst-hour percent of the ambulance fleet committed over a horizon derived from the goal, plus saturated hours, with a recommended action, from total/on-mission/available units in the live fleet',
   '["forecast_available","predicted_peak_fleet_utilization","predicted_saturated_hours","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast the worst-hour percent of the ambulance fleet committed over a horizon derived from the goal, plus saturated hours, with a recommended action, from total/on-mission/available units in the live fleet',
       updated_at = now()
 WHERE id = 'ta_forecast_ambulance_fleet_utilization';
