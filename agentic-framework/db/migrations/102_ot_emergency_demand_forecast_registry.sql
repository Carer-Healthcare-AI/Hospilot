-- ============================================================
-- 102_ot_emergency_demand_forecast_registry.sql
-- Registers sa_ot_emergency_demand (ot_agent) and its task
-- ta_forecast_ot_emergency_demand -- forecasts emergency surgeries
-- requiring immediate OT access over a goal-derived horizon (driven by ER
-- pressure), via Hospilot /ot/emergency-demand (util/forecast_client.py).
-- Executed by run_ot_body in workflows/graph/agents/simple.py. Parent
-- ot_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/102_ot_emergency_demand_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('ot_agent', 'OT Scheduling',
   'Reviews today''s surgical schedule against available post-op beds and flags any conflicts',
   '⚕️', '#7c3aed', true, 70)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_ot_emergency_demand', 'ot_agent', 'Emergency OT Demand Forecast',
   'Forward-looking forecast of EMERGENCY OT DEMAND -- how many emergency surgeries will need immediate operating-theatre access over a horizon inferred from the request (3h-7d), driven by ER pressure. Include when the goal asks about upcoming emergency/trauma surgical demand, unplanned OT access, or emergency theatre reservation over a time horizon. Distinct from sa_ot_surgery_volume (total case count) and sa_ot_utilization (% capacity) -- this predicts unplanned EMERGENCY case load.',
   '["Emergency OT","Trauma Surgery Demand","Unplanned OT Access"]', false, 50)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_ot_emergency_demand', 'sa_ot_emergency_demand',
   'Forecast emergency surgeries requiring immediate OT access over a horizon derived from the goal, with a recommended action, from current emergency cases, ER critical patients, ambulance arrivals, emergency admissions and open theatres',
   '["forecast_available","predicted_emergency_surgeries","operating_rooms_open","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast emergency surgeries requiring immediate OT access over a horizon derived from the goal, with a recommended action, from current emergency cases, ER critical patients, ambulance arrivals, emergency admissions and open theatres',
       updated_at = now()
 WHERE id = 'ta_forecast_ot_emergency_demand';
