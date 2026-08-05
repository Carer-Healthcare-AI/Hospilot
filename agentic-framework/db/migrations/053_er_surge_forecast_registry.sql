-- ============================================================
-- 053_er_surge_forecast_registry.sql
-- Registers sa_er_surge_prediction (er_agent) and its task
-- ta_forecast_er_surge -- ML per-hour ER arrival forecast via
-- Hospilot /forecast/er-surge (util/forecast_client.py).
-- Follows 052_lab_analyzer_forecast_registry (the analyzer-util
-- reference integration); Python fallback parity lives in
-- SUB_AGENTS['er_agent'] in workflows/planner.py and the runtime
-- block in workflows/graph/agents/simple.py.
-- Idempotent -- safe to re-run.
-- Sub-agent ordering: triage(10) -> acuity(20) -> disposition(30)
--                     -> boarding(40) -> surge_prediction(50).
-- ============================================================

-- 1. New forward-looking surge-prediction sub-agent under er_agent.
INSERT INTO hospilot_app.subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_er_surge_prediction', 'er_agent', 'Surge Predictor',
   'FORWARD-LOOKING forecast of INCOMING ER arrival volume for the next several hours via the ML model — drives pre-emptive triage/nurse staffing ahead of a surge. Include when the goal is about anticipating demand, upcoming/expected arrivals, proactive staffing, or ER surge prediction. Independent of the current queue: it reads recent arrival rate, not who is waiting now. Do NOT include for handling the patients already in the ED (that is the Triage Monitor) or admitted boarders (Boarding Monitor).',
   '["Arrival Forecast","Surge Detection","Proactive Staffing"]', false, 50)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

-- 2. The forecast task under the new sub-agent.
INSERT INTO hospilot_app.task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_er_surge', 'sa_er_surge_prediction',
   'Forecast of ER arrival volume per hour over the next few hours, tagging each hour normal/elevated/surge',
   '["forecast_available","total_expected","peak_volume","surge_hour_count","hours"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

-- Keep the label in sync when the row already exists (INSERT above is DO NOTHING).
UPDATE hospilot_app.task_registry
   SET label      = 'Forecast of ER arrival volume per hour over the next few hours, tagging each hour normal/elevated/surge',
       updated_at = now()
 WHERE id = 'ta_forecast_er_surge';
