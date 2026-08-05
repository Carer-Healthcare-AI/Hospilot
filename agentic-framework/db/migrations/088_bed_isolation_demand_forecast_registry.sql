-- ============================================================
-- 088_bed_isolation_demand_forecast_registry.sql
-- Registers sa_bed_isolation_demand (bed_prediction_agent) and its task
-- ta_forecast_bed_isolation_demand -- forecasts isolation-bed demand
-- and shortage risk at a goal-derived horizon via Hospilot
-- /bed/isolation-demand (util/forecast_client.py). Executed by
-- run_bed_prediction_body in workflows/graph/agents/simple.py.
-- Parent bed_prediction_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/088_bed_isolation_demand_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('bed_prediction_agent', 'Bed Prediction',
   'Analyses current bed usage and predicts capacity pressures over the next 4–24 hours (forecast only, no placement)',
   '📊', '#0284c7', true, 90)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_bed_isolation_demand', 'bed_prediction_agent', 'Isolation Bed Demand Forecast',
   'Forward-looking forecast of ISOLATION-BED demand and shortage risk at a horizon inferred from the request (6h-7d), from isolation-bed occupancy and the infection-control caseload. Include when the goal asks about isolation/negative-pressure bed needs, infectious-disease surge capacity, or isolation shortage risk. Distinct from the general bed occupancy/ward-capacity forecasts.',
   '["Isolation Beds","Infectious Demand","Shortage Risk"]', false, 46)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_bed_isolation_demand', 'sa_bed_isolation_demand',
   'Forecast isolation-bed requirements and shortage risk at a horizon derived from the goal, from current isolation beds occupied/total, active infectious-disease cases and suspected cases',
   '["forecast_available","predicted_isolation_beds_required","isolation_shortage_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast isolation-bed requirements and shortage risk at a horizon derived from the goal, from current isolation beds occupied/total, active infectious-disease cases and suspected cases',
       updated_at = now()
 WHERE id = 'ta_forecast_bed_isolation_demand';
