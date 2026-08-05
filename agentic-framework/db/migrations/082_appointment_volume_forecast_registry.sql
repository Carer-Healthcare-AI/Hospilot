-- ============================================================
-- 082_appointment_volume_forecast_registry.sql
-- Registers sa_appt_volume (appointment_agent) and its task
-- ta_forecast_appointment_volume -- forecasts appointment booking
-- volume + slot utilisation over a goal-derived horizon with
-- capacity-shortage risk, via Hospilot /appointments/volume-forecast
-- (util/forecast_client.py). appointment_agent is REGISTRY-DRIVEN
-- (generic run_registry_body -> run_builtin_task), so the handler lives
-- in agents/appointment/activities.py::APPOINTMENT_TASKS -- there is NO
-- planner SUB_AGENTS entry and no hand-written dispatch body to edit.
-- Parent appointment_agent upserted first (mirrors 009).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/082_appointment_volume_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('appointment_agent', 'Appointments',
   'Schedules OPD appointments, sends reminders, and prevents no-shows.',
   '📅', '#14b8a6', true, 120)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_appt_volume', 'appointment_agent', 'Appointment Volume Forecast',
   'Forward-looking forecast of how many APPOINTMENTS patients will book over a horizon inferred from the request (6h-7d), plus slot utilisation, remaining slots and capacity-shortage risk. Include when the goal asks about predicted appointment/booking volume, OPD demand, slot utilisation, or clinic capacity over a time horizon. Distinct from sa_appt_scheduling (books one slot now) and sa_appt_noshow (per-patient no-show risk).',
   '["Appointment Volume","Slot Utilisation","Capacity Shortage Risk"]', false, 40)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_appointment_volume', 'sa_appt_volume',
   'Forecast appointment booking volume and slot utilisation over a horizon derived from the goal, with remaining slots and capacity-shortage risk, from open OPD slots, booked appointments and available doctors',
   '["forecast_available","predicted_appointment_volume","predicted_slot_utilization","capacity_shortage_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast appointment booking volume and slot utilisation over a horizon derived from the goal, with remaining slots and capacity-shortage risk, from open OPD slots, booked appointments and available doctors',
       updated_at = now()
 WHERE id = 'ta_forecast_appointment_volume';
