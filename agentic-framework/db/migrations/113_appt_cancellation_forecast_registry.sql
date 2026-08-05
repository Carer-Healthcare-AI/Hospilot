-- ============================================================
-- 113_appt_cancellation_forecast_registry.sql
-- Registers sa_appt_cancellation_forecast (appointment_agent, REGISTRY-DRIVEN) and its task ta_forecast_cancellation.
-- Handler lives in agents/appointment/activities.py::APPOINTMENT_TASKS
-- (signature (session_id, ta_results, ctx)); executed via run_registry_body.
-- No planner SUB_AGENTS entry / no dispatch body -- appointment_agent is
-- registry-driven, so this works on the default source only.
-- NOTE: rate-driving inputs (prior no-show/cancellation history, doctor
-- absence, reminders) have no data source and use documented constants
-- (result flags rate_inputs_assumed=True). Built on explicit request.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/113_appt_cancellation_forecast_registry.sql
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
  ('sa_appt_cancellation_forecast', 'appointment_agent', 'Cancellation Forecast',
   'Forward-looking forecast of appointment CANCELLATIONS across the days in a horizon inferred from the request (24h-14d), plus the peak day, to tell the front desk which slots can still be refilled. Include when the goal asks about expected cancellations, waitlist backfill, or slot-refill planning over a time horizon. NOTE: rate-driving inputs (prior cancellation history, doctor absence) are not sourced -- rests on a base rate.',
   '["Cancellation Rate","Waitlist Backfill","Slot Refill"]', false, 36)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_cancellation', 'sa_appt_cancellation_forecast',
   'Forecast appointment cancellations across the days in a horizon derived from the goal, with a recommended action, from booked slots, session structure and a base cancellation rate',
   '["forecast_available","predicted_cancellations","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label = 'Forecast appointment cancellations across the days in a horizon derived from the goal, with a recommended action, from booked slots, session structure and a base cancellation rate', updated_at = now()
 WHERE id = 'ta_forecast_cancellation';
