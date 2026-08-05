-- Migration 033: register the appointment movability + reschedule tasks in the
-- live registry so the planner can select them for Q3 ("reschedule non-urgent
-- appointments away from peak understaffed hours") -- planner-query-gaps G16 + G14.
--
-- Both run under sa_appt_scheduling after the booking tasks:
--   ta_appt_classify_movable (70) -- urgent vs non-urgent/movable classification (G16)
--   ta_appt_reschedule       (80) -- move movable appts off peak hours -> off-peak slots (G14)
-- ta_appt_reschedule consumes the staffing agent's peak_understaffed_hours (G15) via ctx,
-- and the off-peak moves are staged on approval by ta_appt_confirm_reschedule (post-approval,
-- not a registry task -- like ta_appt_confirm_booking).
--
-- Mirrors the seed literals in 009_appointment_agent_registry.sql. Schema hospilot_app
-- (planner's registry). Idempotent.

BEGIN;

INSERT INTO "hospilot_app".task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  ('ta_appt_classify_movable', 'sa_appt_scheduling',
   'Classify upcoming appointments as urgent (keep) vs non-urgent / movable',
   '["assessed","movable_count","urgent_count","movable"]', true, false, 70),
  ('ta_appt_reschedule', 'sa_appt_scheduling',
   'Reschedule non-urgent appointments away from peak understaffed hours to off-peak slots',
   '["rescheduled","proposed_count","avoid_hours","avoid_source"]', true, false, 80)
ON CONFLICT (id) DO UPDATE SET
  label       = EXCLUDED.label,
  outputs     = EXCLUDED.outputs,
  subagent_id = EXCLUDED.subagent_id,
  sort_order  = EXCLUDED.sort_order,
  is_active   = true,
  updated_at  = now();

COMMIT;
