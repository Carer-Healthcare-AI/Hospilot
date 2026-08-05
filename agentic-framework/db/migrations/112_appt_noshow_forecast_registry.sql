-- ============================================================
-- 112_appt_noshow_forecast_registry.sql
-- Registers sa_appt_noshow_forecast (appointment_agent, REGISTRY-DRIVEN) and its task ta_forecast_noshow.
-- Handler lives in agents/appointment/activities.py::APPOINTMENT_TASKS
-- (signature (session_id, ta_results, ctx)); executed via run_registry_body.
-- No planner SUB_AGENTS entry / no dispatch body -- appointment_agent is
-- registry-driven, so this works on the default source only.
-- NOTE: rate-driving inputs (prior no-show/cancellation history, doctor
-- absence, reminders) have no data source and use documented constants
-- (result flags rate_inputs_assumed=True). Built on explicit request.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/112_appt_noshow_forecast_registry.sql
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
  ('sa_appt_noshow_forecast', 'appointment_agent', 'No-Show Rate Forecast',
   'Forward-looking forecast of the appointment NO-SHOW RATE (percent) across sessions over a horizon inferred from the request (12h-7d), plus the worst single session, to size per-session overbooking. Include when the goal asks about expected no-shows, overbooking guidance, or attendance risk over a time horizon. Distinct from per-patient no-show flagging. NOTE: rate-driving inputs (prior no-show history, reminders, cohort) are not sourced -- rests on a base rate.',
   '["No-Show Rate","Overbooking","Attendance Risk"]', false, 35)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_noshow', 'sa_appt_noshow_forecast',
   'Forecast the no-show rate (percent) across sessions over a horizon derived from the goal, with a recommended action, from booked slots, session structure and a base no-show rate',
   '["forecast_available","predicted_noshow_rate_percent","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label = 'Forecast the no-show rate (percent) across sessions over a horizon derived from the goal, with a recommended action, from booked slots, session structure and a base no-show rate', updated_at = now()
 WHERE id = 'ta_forecast_noshow';
