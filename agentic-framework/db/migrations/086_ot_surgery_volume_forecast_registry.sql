-- ============================================================
-- 086_ot_surgery_volume_forecast_registry.sql
-- Registers sa_ot_surgery_volume (ot_agent) and its task
-- ta_forecast_ot_surgery_volume -- forecasts how many surgeries will
-- be performed over a goal-derived horizon (total/emergency/elective)
-- with capacity risk, via Hospilot /ot/surgery-volume
-- (util/forecast_client.py). Sibling of sa_ot_utilization (mig 074).
-- Executed by run_ot_body in workflows/graph/agents/simple.py.
-- Parent ot_agent upserted first (pre-005 seed may not have carried
-- into hospilot_app in every DB).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/086_ot_surgery_volume_forecast_registry.sql
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
  ('sa_ot_surgery_volume', 'ot_agent', 'Surgery Volume Forecast',
   'Forward-looking forecast of how many SURGERIES will be performed over a horizon inferred from the request (shift/24h/3d/7d) -- total, emergency and elective -- with workload status and capacity risk. Include when the goal asks about predicted surgery/case volume, theatre throughput, or elective-vs-emergency case load over a time horizon. Distinct from sa_ot_utilization (predicts % capacity USED) and the live scheduling work.',
   '["Surgery Volume","Case Load","Capacity Risk"]', false, 70)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_ot_surgery_volume', 'sa_ot_surgery_volume',
   'Forecast how many surgeries will be performed over a horizon derived from the goal (total, emergency and elective), with capacity risk, from operating rooms and the scheduled/emergency case load',
   '["forecast_available","predicted_surgeries","predicted_emergency_surgeries","capacity_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast how many surgeries will be performed over a horizon derived from the goal (total, emergency and elective), with capacity risk, from operating rooms and the scheduled/emergency case load',
       updated_at = now()
 WHERE id = 'ta_forecast_ot_surgery_volume';
