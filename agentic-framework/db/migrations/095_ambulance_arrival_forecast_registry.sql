-- ============================================================
-- 095_ambulance_arrival_forecast_registry.sql
-- Registers sa_er_ambulance_arrivals (er_agent) and its task
-- ta_forecast_ambulance_arrivals -- forecasts ambulances arriving at the
-- ED over a goal-derived horizon, via Hospilot /er/ambulance-arrivals
-- (util/forecast_client.py). Executed by run_er_body in
-- workflows/graph/agents/simple.py. Parent er_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/095_ambulance_arrival_forecast_registry.sql
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
  ('sa_er_ambulance_arrivals', 'er_agent', 'Ambulance Arrival Forecast',
   'Forward-looking forecast of AMBULANCE ARRIVALS at the ED -- how many ambulances will bring patients to this hospital over a horizon inferred from the request (6h-7d). Include when the goal asks about incoming/expected ambulance arrivals, EMS inflow, or pre-positioning triage/trauma for inbound ambulances. Distinct from sa_er_surge_prediction (total walk-in + ambulance arrival VOLUME) and the ambulance_agent live fleet dispatch -- this predicts EMS arrival count to the ED.',
   '["Ambulance Arrivals","EMS Inflow","Trauma Pre-positioning"]', false, 100)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_ambulance_arrivals', 'sa_er_ambulance_arrivals',
   'Forecast how many ambulances will arrive at the ED over a horizon derived from the goal, with a recommended action, from recent ambulance activity (live fleet) and ED load',
   '["forecast_available","predicted_ambulance_arrivals","arrival_level","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast how many ambulances will arrive at the ED over a horizon derived from the goal, with a recommended action, from recent ambulance activity (live fleet) and ED load',
       updated_at = now()
 WHERE id = 'ta_forecast_ambulance_arrivals';
