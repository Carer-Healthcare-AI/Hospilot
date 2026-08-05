-- ============================================================
-- 054_bed_turnover_forecast_registry.sql
-- Registers sa_bed_turnover (bed_prediction_agent) and its task
-- ta_forecast_bed_turnover -- per-ward forecast of beds freeing
-- next shift via Hospilot /bed/turnover (util/forecast_client.py).
-- Follows 052/053 (analyzer-util, er-surge). Executed by
-- run_bed_prediction_body in workflows/graph/agents/simple.py. NOTE: the Python
-- fallback catalog groups the bed-prediction sub-agents under the SUB_AGENTS
-- ['bed_agent'] key (alongside sa_bed_pred_census/forecast), but the DB registry
-- and registry.py dispatch both own them under bed_prediction_agent -- the DB is
-- the runtime source of truth, so this migration registers under that agent.
-- Idempotent -- safe to re-run.
-- Sub-agent ordering under bed_prediction_agent:
--   census(10) -> forecast(20) -> turnover(30).
--
-- NOTE: the parent agent row (bed_prediction_agent) is upserted first. It is a
-- live agent in code (planner.AVAILABLE_AGENTS + registry.py -> run_bed_prediction_body)
-- but the original seed (003) targeted the pre-005 `hospilot` schema and did not
-- carry into `hospilot_app` in every DB, so the subagent FK can fail without this.
-- ============================================================

-- 0. Ensure the parent agent exists in hospilot_app (idempotent; no-op if present).
INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('bed_prediction_agent', 'Bed Prediction',
   'Analyses current bed usage and predicts capacity pressures over the next 4–24 hours (forecast only, no placement)',
   '📊', '#0284c7', true, 90)
ON CONFLICT (id) DO NOTHING;

-- 1. New per-ward bed-turnover forecast sub-agent under bed_prediction_agent.
INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_bed_turnover', 'bed_prediction_agent', 'Bed Turnover Forecast',
   'Forward-looking per-ward forecast of how many beds will physically free up in the NEXT SHIFT — combines current occupancy, discharge outlook, housekeeping/cleaning backlog and ER inflow to give shift coordinators incoming-capacity visibility. Include when the goal is about upcoming bed availability, shift-handover capacity, or turnover planning. Distinct from sa_bed_pred_forecast (overall overflow/ICU risk narrative) and from live bed placement.',
   '["Bed Turnover","Shift Capacity","Cleaning Backlog"]', false, 30)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

-- 2. The forecast task under the new sub-agent.
INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_bed_turnover', 'sa_bed_turnover',
   'Forecast of beds becoming available in the next shift, per ward, from occupancy, discharge outlook, cleaning backlog and ER inflow',
   '["forecast_available","wards_forecast","low_capacity_count","wards"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

-- Keep the label in sync when the row already exists (INSERT above is DO NOTHING).
UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast of beds becoming available in the next shift, per ward, from occupancy, discharge outlook, cleaning backlog and ER inflow',
       updated_at = now()
 WHERE id = 'ta_forecast_bed_turnover';
