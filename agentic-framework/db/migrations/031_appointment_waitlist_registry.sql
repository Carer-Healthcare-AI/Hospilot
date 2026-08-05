-- Migration 031: register the waitlist matcher task in the live registry so the
-- DB-driven planner can select it for waitlist queries (planner-query-gaps G3).
--
-- ta_appt_match_waitlist runs under sa_appt_scheduling AFTER ta_appt_find_available_slots
-- (10) / ta_appt_match_specialty (20) and BEFORE ta_appt_reserve_slot -- so the
-- registry-loop task order becomes find_slots -> match_specialty -> match_waitlist ->
-- reserve_slot (sort_order 25). reserve_slot now consumes the emitted `matches` (G4).
--
-- Mirrors the seed literals in 009_appointment_agent_registry.sql. Schema is
-- hospilot_app (planner's registry). Idempotent (ON CONFLICT DO NOTHING + outputs
-- UPDATE for reserve_slot so an already-seeded row picks up the new contract).

BEGIN;

INSERT INTO "hospilot_app".task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  ('ta_appt_match_waitlist', 'sa_appt_scheduling',
   'Match waitlisted patients to open slots: order by priority, pair each to the earliest suitable same-specialty slot (feeds booking)',
   '["waitlist_count","matched_count","unmatched_count","matches"]', true, false, 25)
ON CONFLICT (id) DO UPDATE SET
  label      = EXCLUDED.label,
  outputs    = EXCLUDED.outputs,
  subagent_id = EXCLUDED.subagent_id,
  sort_order = EXCLUDED.sort_order,
  is_active  = true,
  updated_at = now();

-- reserve_slot now books the matched cohort; widen its declared outputs.
UPDATE "hospilot_app".task_registry
   SET label      = 'Book the matched patient<->slot pairs (or the earliest suitable slot)',
       outputs    = '["slot_reserved","matched_count","appointment_id"]'::jsonb,
       updated_at = now()
 WHERE id = 'ta_appt_reserve_slot';

COMMIT;
