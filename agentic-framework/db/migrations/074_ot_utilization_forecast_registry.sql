-- ============================================================
-- 074_ot_utilization_forecast_registry.sql
-- Registers sa_ot_utilization (ot_agent) and its task
-- ta_forecast_ot_utilization -- forecasts the % of OT capacity
-- used over a goal-derived horizon (used/free OT hours + capacity
-- risk) via Hospilot /ot/utilization (util/forecast_client.py).
-- The first OT forecast sub-agent. Executed by run_ot_body in
-- workflows/graph/agents/simple.py. Parent ot_agent is upserted
-- first (pre-005 seed may not have carried into hospilot_app in
-- every DB -- avoids the FK trap).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/074_ot_utilization_forecast_registry.sql
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
  ('sa_ot_utilization', 'ot_agent', 'OT Utilization Forecast',
   'Forward-looking forecast of how much OT capacity (%) will be used over a horizon inferred from the request (3h–7d), with used/free theatre hours and capacity risk (Low/Medium/High). Include when the goal asks how busy/full theatres will be, upcoming OT capacity/utilization, or overtime/overrun risk. Distinct from the live turnaround/scheduling work — it projects utilization forward rather than managing today''s list.',
   '["OT Utilization","Capacity Forecast","Overrun Risk"]', false, 60)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_ot_utilization', 'sa_ot_utilization',
   'Forecast the % of operating-theatre capacity that will be used over a horizon derived from the goal, with used/free OT hours and capacity risk, from open theatres and the scheduled/emergency case load',
   '["forecast_available","predicted_utilization_percent","capacity_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast the % of operating-theatre capacity that will be used over a horizon derived from the goal, with used/free OT hours and capacity risk, from open theatres and the scheduled/emergency case load',
       updated_at = now()
 WHERE id = 'ta_forecast_ot_utilization';
