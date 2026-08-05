-- Migration 037: register the non-OPD slot tasks (sample collection / pharmacy
-- pickup) so the planner can select them for Q5 ("book the sample collection
-- appointments") and Q11 ("notify patients of pickup appointment windows").
-- (planner-query-gaps G23 + G39.)
--
-- Both run under sa_appt_scheduling after the OPD/reschedule tasks:
--   ta_appt_find_service_slots (85) -- open sample_collection / pharmacy_pickup slots
--   ta_appt_book_service_slot  (90) -- book a cohort into them + notify of the window
-- The off-approval staging is done by ta_appt_confirm_service_booking (post-approval,
-- not a registry task -- like ta_appt_confirm_booking).
--
-- Mirrors the seed literals in 009_appointment_agent_registry.sql. Schema hospilot_app
-- (planner's registry). Idempotent.

BEGIN;

INSERT INTO "hospilot_app".task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  ('ta_appt_find_service_slots', 'sa_appt_scheduling',
   'Find open non-OPD slots (sample collection / phlebotomy, pharmacy pickup)',
   '["slot_type","available_slot_count","slots_by_type","earliest_slot"]', true, false, 85),
  ('ta_appt_book_service_slot', 'sa_appt_scheduling',
   'Book patients into sample-collection or pharmacy-pickup windows and notify them',
   '["slot_reserved","booked_count","slot_type","matched_count","windows"]', true, false, 90)
ON CONFLICT (id) DO UPDATE SET
  label       = EXCLUDED.label,
  outputs     = EXCLUDED.outputs,
  subagent_id = EXCLUDED.subagent_id,
  sort_order  = EXCLUDED.sort_order,
  is_active   = true,
  updated_at  = now();

COMMIT;
