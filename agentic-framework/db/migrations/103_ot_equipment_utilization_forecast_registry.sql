-- ============================================================
-- 103_ot_equipment_utilization_forecast_registry.sql
-- Registers sa_ot_equipment_utilization (ot_agent) and its task
-- ta_forecast_ot_equipment_utilization -- forecasts the peak share of
-- critical theatre equipment committed over a goal-derived horizon, via
-- Hospilot /ot/equipment-utilization (util/forecast_client.py). Executed
-- by run_ot_body in workflows/graph/agents/simple.py. Parent ot_agent
-- upserted first.
--
-- CAVEAT: no OT equipment inventory/status data source exists -- the
-- activity sends an ASSUMED total_equipment (and 0 reserved/maintenance/
-- out-of-order); result carries equipment_data_assumed=True. The endpoint's
-- maintenance-window head is vendor-flagged BLOCKED, so that output is not
-- surfaced. Built on explicit request; revisit when an equipment inventory
-- source exists.
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/103_ot_equipment_utilization_forecast_registry.sql
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
  ('sa_ot_equipment_utilization', 'ot_agent', 'Equipment Utilization Forecast',
   'Forward-looking forecast of the peak share of critical theatre EQUIPMENT committed over a horizon inferred from the request (3h-7d). Include when the goal asks about surgical equipment/device contention, equipment availability for upcoming cases, or equipment bottlenecks over a time horizon. Distinct from sa_ot_utilization (theatre-room % capacity) -- this projects EQUIPMENT commitment. NOTE: rests on an ASSUMED equipment inventory (no inventory/status data source); the maintenance-window output is not surfaced (endpoint head blocked).',
   '["Equipment Utilization","Device Contention","OT Equipment"]', false, 60)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_ot_equipment_utilization', 'sa_ot_equipment_utilization',
   'Forecast the peak share of critical theatre EQUIPMENT committed over a horizon derived from the goal, with a recommended action, from equipment committed to active cases, scheduled/emergency surgeries and theatre utilization (equipment inventory is assumed -- no inventory source)',
   '["forecast_available","predicted_peak_equipment_utilization","equipment_data_assumed","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast the peak share of critical theatre EQUIPMENT committed over a horizon derived from the goal, with a recommended action, from equipment committed to active cases, scheduled/emergency surgeries and theatre utilization (equipment inventory is assumed -- no inventory source)',
       updated_at = now()
 WHERE id = 'ta_forecast_ot_equipment_utilization';
