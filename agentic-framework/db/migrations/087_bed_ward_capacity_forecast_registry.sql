-- ============================================================
-- 087_bed_ward_capacity_forecast_registry.sql
-- Registers sa_bed_ward_capacity (bed_prediction_agent) and its task
-- ta_forecast_bed_ward_capacity -- per-ward occupancy & capacity-
-- utilization forecast at a goal-derived horizon via Hospilot
-- /bed/ward-capacity (util/forecast_client.py). Per-ward sibling of
-- sa_bed_occupancy (mig 072) / sa_bed_turnover (mig 054). Executed by
-- run_bed_prediction_body in workflows/graph/agents/simple.py. Owned by
-- bed_prediction_agent in the DB registry (same as the other bed forecasts).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/087_bed_ward_capacity_forecast_registry.sql
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
  ('sa_bed_ward_capacity', 'bed_prediction_agent', 'Ward Capacity Forecast',
   'Forward-looking PER-WARD capacity forecast: predicts each ward''s occupancy, capacity utilization %, available beds and overflow risk at a horizon inferred from the request (6h-7d). Include when the goal asks about a specific ward''s capacity/utilization or which wards will be over capacity. Distinct from sa_bed_occupancy (whole-hospital census) and sa_bed_turnover (beds freeing next shift).',
   '["Ward Capacity","Utilization","Overflow Risk"]', false, 45)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_bed_ward_capacity', 'sa_bed_ward_capacity',
   'Per-ward forecast of occupancy and capacity utilization at a horizon derived from the goal, with predicted available beds, net patient change and overflow risk, from each ward''s beds, discharge outlook and ER pressure',
   '["forecast_available","wards_forecast","high_risk_count","wards"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Per-ward forecast of occupancy and capacity utilization at a horizon derived from the goal, with predicted available beds, net patient change and overflow risk, from each ward''s beds, discharge outlook and ER pressure',
       updated_at = now()
 WHERE id = 'ta_forecast_bed_ward_capacity';
