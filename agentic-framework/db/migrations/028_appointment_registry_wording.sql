-- Migration 028: sharpen appointment-agent registry wording so the DB-driven
-- planner selects the right sub-agents/tasks for slot-finding and waitlist queries.
--
-- WHY: the planner's selection step reads ONLY subagent_registry.description (for
-- sub-agent selection, planner._sa_line) and task_registry.label (for task
-- selection). For "find slots + match waitlist" goals the scheduling sub-agent
-- was under-selected and the two "waitlist" tasks read as more than they do.
--
-- TRUTHFUL wording: ta_appt_fill_cancellation and ta_appt_waitlist_replacement
-- only COUNT candidates (cancelled-appt pool vs open slots / high-risk count);
-- they do NOT match patients to specific slots and there is no waitlist table.
-- Labels/descriptions are reworded to be discoverable AND accurate -- they must
-- not imply patient-to-slot assignment the code does not perform.
--
-- IDs are kept stable (FK targets + broadcast to the frontend). Description/label
-- only. Mirrors the seed literals in 009_appointment_agent_registry.sql.
-- Schema is hospilot_app (where the planner's Hasura registry query resolves).
-- Idempotent -- safe to re-run.

BEGIN;

-- Sub-agent descriptions (drive sub-agent selection) --------------------------

UPDATE "hospilot_app".subagent_registry
   SET description = 'Finds bookable OPD slots, matches them to the requested specialty, books the earliest suitable slot, and reconciles cancellations against open capacity. Handles slot-finding and booking for scheduling and waitlist/cancellation queries.',
       updated_at  = now()
 WHERE id = 'sa_appt_scheduling';

UPDATE "hospilot_app".subagent_registry
   SET description = 'Predicts no-show risk and engages high-risk patients; prepares replacement candidates from the cancellation pool to backfill predicted no-show slots (counts candidates; does not auto-assign to slots).',
       updated_at  = now()
 WHERE id = 'sa_appt_noshow';

-- Task labels (drive task selection) ------------------------------------------

UPDATE "hospilot_app".task_registry
   SET label      = 'Count cancelled appointments and open slots; report how many cancellations are fillable from the cancellation pool (no patient-slot assignment)',
       updated_at = now()
 WHERE id = 'ta_appt_fill_cancellation';

UPDATE "hospilot_app".task_registry
   SET label      = 'Count replacement candidates from the cancellation pool for predicted no-show slots (after no-show prediction; no auto-assignment)',
       updated_at = now()
 WHERE id = 'ta_appt_waitlist_replacement';

COMMIT;
