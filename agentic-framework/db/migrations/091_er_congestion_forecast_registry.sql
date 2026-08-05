-- ============================================================
-- 091_er_congestion_forecast_registry.sql
-- Registers sa_er_congestion (er_agent) and its task
-- ta_forecast_er_congestion -- forecasts overall ED congestion as a
-- composite score (0-100) over a goal-derived horizon with a
-- congestion level and recommended action, via Hospilot
-- /er/congestion (util/forecast_client.py). Executed by run_er_body
-- in workflows/graph/agents/simple.py. Parent er_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/091_er_congestion_forecast_registry.sql
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
  ('sa_er_congestion', 'er_agent', 'Congestion Forecast',
   'Forward-looking forecast of overall ED CONGESTION as a single composite score (0-100) with a congestion level (normal/busy/congested/critical) over a horizon inferred from the request (6h-7d). Include when the goal asks about overall ED congestion, crowding, gridlock, or a single crowding score/level. Distinct from the component forecasts sa_er_wait_time (wait minutes), sa_er_boarding_forecast (boarding), sa_er_lwbs (walkouts) and sa_er_surge_prediction (arrival volume).',
   '["ED Congestion","Crowding","Gridlock"]', false, 90)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_er_congestion', 'sa_er_congestion',
   'Forecast overall ED congestion as a composite score (0-100) over a horizon derived from the goal, with congestion level and recommended action, from ED census, the waiting queue, boarders, recent arrivals, critical patient load and bed/staffing availability',
   '["forecast_available","predicted_congestion_score","congestion_level","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast overall ED congestion as a composite score (0-100) over a horizon derived from the goal, with congestion level and recommended action, from ED census, the waiting queue, boarders, recent arrivals, critical patient load and bed/staffing availability',
       updated_at = now()
 WHERE id = 'ta_forecast_er_congestion';
