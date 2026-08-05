-- ============================================================
-- 101_ot_surgery_duration_forecast_registry.sql
-- Registers sa_ot_surgery_duration (ot_agent) and its task
-- ta_forecast_ot_surgery_duration -- forecasts the mean surgery duration
-- (minutes) over a goal-derived horizon with the change vs the current
-- average, via Hospilot /ot/surgery-duration (util/forecast_client.py).
-- Executed by run_ot_body in workflows/graph/agents/simple.py. Parent
-- ot_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/101_ot_surgery_duration_forecast_registry.sql
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
  ('sa_ot_surgery_duration', 'ot_agent', 'Surgery Duration Forecast',
   'Forward-looking forecast of the mean SURGERY DURATION (minutes per case) over a horizon inferred from the request (6h-7d), with the change vs the current average. Include when the goal asks about predicted/expected surgery duration, how long cases will run, or case-length-driven overruns over a time horizon. Distinct from sa_ot_turnaround_forecast (between-case turnaround), sa_ot_surgery_volume (case count) and sa_ot_utilization (% capacity used) -- this predicts per-case length.',
   '["Surgery Duration","Case Length","OT Overrun"]', false, 40)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_ot_surgery_duration', 'sa_ot_surgery_duration',
   'Forecast the mean surgery duration (minutes) over a horizon derived from the goal, with the change vs the current average and a recommended action, from current average duration, scheduled/emergency/elective cases and open theatres',
   '["forecast_available","predicted_average_duration","change_vs_now_minutes","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast the mean surgery duration (minutes) over a horizon derived from the goal, with the change vs the current average and a recommended action, from current average duration, scheduled/emergency/elective cases and open theatres',
       updated_at = now()
 WHERE id = 'ta_forecast_ot_surgery_duration';
