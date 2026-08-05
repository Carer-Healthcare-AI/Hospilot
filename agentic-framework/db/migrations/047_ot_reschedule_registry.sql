-- ─────────────────────────────────────────────────────────────────────────────
-- Gap G32 (+ part of G31/G33) — executable OT theatre-slot search + surgical reschedule.
--
-- Adds two tasks under sa_ot_scheduling:
--   ta_ot_find_theatre_slots  — derive open theatre time-windows (rooms minus booked
--                               cases within the operating window) for (re)scheduling.
--   ta_ot_reschedule_surgery  — stage an executable move of a cancelled surgery to the
--                               earliest open slot (committed to Fabric -> ot_surgeries).
-- Also fixes routing (G32): sa_ot_scheduling OWNS surgical rescheduling; sa_appt_scheduling
-- is scoped to OPD only so a cancelled SURGERY is never sent to the OPD slot search.
-- Sub-agent/task selection reads ONLY the registry descriptions/labels. Idempotent.
-- Python parity: SUB_AGENTS['ot_agent'] sa_ot_scheduling in workflows/planner.py.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO hospilot_app.task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  ('ta_ot_find_theatre_slots', 'sa_ot_scheduling',
   'Find open theatre slots — derive bookable OT time-windows (by room type + duration) for (re)scheduling a surgery — include when a surgery must be scheduled or rescheduled into a slot',
   '["open_slots","open_slot_count"]', true, false, 50),
  ('ta_ot_reschedule_surgery', 'sa_ot_scheduling',
   'Reschedule a cancelled surgery to the earliest open theatre slot; stages an executable move for commit — ONLY when a surgery is cancelled / must be moved to a new slot',
   '["rescheduled","proposals","status"]', true, false, 60)
ON CONFLICT (id) DO UPDATE SET
  subagent_id = EXCLUDED.subagent_id, label = EXCLUDED.label, outputs = EXCLUDED.outputs,
  is_active = true, sort_order = EXCLUDED.sort_order, updated_at = now();

UPDATE hospilot_app.subagent_registry
   SET description = 'Owns the surgical plan AND surgical (re)scheduling: detects room/surgeon conflicts, checks resources, balances theatre load, and — when a surgery is cancelled or must be moved — finds an open theatre slot and stages an executable reschedule. This is the owner of moving/rescheduling a theatre case (never the OPD appointment agent).',
       updated_at  = now()
 WHERE id = 'sa_ot_scheduling';

UPDATE hospilot_app.subagent_registry
   SET description = 'Finds bookable OPD (outpatient consult) slots only, matches them to the requested specialty, books the earliest suitable slot, and reconciles OPD cancellations against open capacity. NOT for surgical/theatre/OT (re)scheduling — a cancelled or to-be-moved SURGERY is handled by the OT Scheduling Agent (ot_agent), not here.',
       updated_at  = now()
 WHERE id = 'sa_appt_scheduling';
