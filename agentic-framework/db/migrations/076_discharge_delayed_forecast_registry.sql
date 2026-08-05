-- ============================================================
-- 076_discharge_delayed_forecast_registry.sql
-- Registers sa_discharge_delayed (discharge_agent) and its task
-- ta_forecast_delayed_discharge -- forecasts how many discharges
-- will be DELAYED (beds blocked) over a goal-derived horizon, with
-- patient-flow risk, via Hospilot /discharge/delayed-forecast
-- (util/forecast_client.py). Flow-risk sibling of 075 (discharge/volume).
-- Executed by run_discharge_body in workflows/graph/agents/clinical.py.
-- Parent discharge_agent is upserted first (pre-005 seed may not have
-- carried into hospilot_app in every DB -- avoids the FK trap).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/076_discharge_delayed_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('discharge_agent', 'Discharge Planning',
   'Identifies patients ready for discharge, resolves barriers, and generates discharge documentation',
   '📤', '#10b981', true, 50)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_discharge_delayed', 'discharge_agent', 'Delayed Discharge Forecast',
   'Forward-looking forecast of how many discharges will be DELAYED (beds stay blocked) over a horizon inferred from the request (6h/12h/24h/3d/7d), with patient-flow risk (Low/Medium/High). Include when the goal asks about discharge delays, bed-blocking, patient-flow bottlenecks, or beds staying occupied. Distinct from sa_discharge_volume (predicts total discharges) — this predicts the delayed SHORTFALL.',
   '["Delayed Discharge","Bed Blocking","Patient Flow Risk"]', false, 40)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_delayed_discharge', 'sa_discharge_delayed',
   'Forecast how many discharges will be DELAYED (beds blocked) over a horizon derived from the goal, with patient-flow risk, from census, the discharge outlook, medically-ready count and free ward beds',
   '["forecast_available","predicted_delayed_discharges","delayed_percent","flow_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast how many discharges will be DELAYED (beds blocked) over a horizon derived from the goal, with patient-flow risk, from census, the discharge outlook, medically-ready count and free ward beds',
       updated_at = now()
 WHERE id = 'ta_forecast_delayed_discharge';
