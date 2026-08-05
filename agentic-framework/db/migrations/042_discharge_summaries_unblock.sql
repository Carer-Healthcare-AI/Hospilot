-- Migration 042: remove the hard results_pending == 0 gate on ta_generate_summaries (G12).
--
-- The condition froze the entire discharge and bed release whenever any pending
-- lab/imaging result existed. The activity ignores results_pending entirely --
-- it generates summaries for all ready admissions regardless. Changing the label
-- to "always include" lets the task planner select it unconditionally; the AI
-- draft naturally notes any pending results rather than blocking on them.

BEGIN;

UPDATE hospilot_app.task_registry
   SET label      = 'Generate AI discharge summaries — always include when discharge-ready patients exist; marks any pending lab/imaging results in the draft rather than blocking on them',
       updated_at = now()
 WHERE id = 'ta_generate_summaries';

COMMIT;
