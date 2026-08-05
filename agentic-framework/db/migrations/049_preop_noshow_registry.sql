-- ─────────────────────────────────────────────────────────────────────────────
-- Gap G31 (No-Show Prevention slice) — identify OT cases that lost their pre-op.
--
-- Query #48 ("6 pre-op assessment no-shows, tomorrow's OT list"): the No-Show
-- Prevention agent ran generic housekeeping but never identified WHICH of tomorrow's
-- surgical cases are at risk from a missed pre-op. New task ta_appt_flag_preop_noshows
-- flags tomorrow's OT cases (ot_surgery_schedule) whose patient has no-show
-- appointment(s) — a proxy, since the data has no 'Pre-op' appointment type or
-- appointment->surgery link (join is by patient_id). Read-only: flags + alerts so the
-- OT team can verify/expedite the pre-op or reschedule. Idempotent.
-- Dispatch: APPOINTMENT_TASKS in agents/appointment/activities.py (run_registry_body).
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO hospilot_app.task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  ('ta_appt_flag_preop_noshows', 'sa_appt_noshow',
   'Identify tomorrow''s OT cases at risk of a missed pre-op — flag surgeries scheduled tomorrow whose patient has no-show appointment(s) so the pre-op assessment can be verified/expedited before proceeding — include when the goal concerns pre-op no-shows or tomorrow''s OT list readiness',
   '["at_risk_count","tomorrow_case_count","at_risk_cases"]', true, false, 60)
ON CONFLICT (id) DO UPDATE SET
  subagent_id = EXCLUDED.subagent_id, label = EXCLUDED.label, outputs = EXCLUDED.outputs,
  is_active = true, sort_order = EXCLUDED.sort_order, updated_at = now();
