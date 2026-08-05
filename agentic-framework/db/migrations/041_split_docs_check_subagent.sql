-- Migration 041: sharpen ta_check_documentation_gaps label so the task planner
-- excludes it from live emergency / real-time staffing goals (G1 gap fix).
--
-- The subagent_tasks prompt defaults to exclusion for tasks with a restrictive
-- "ONLY when ..." semantic phrase. Adding that phrase to the label is sufficient
-- to prevent the docs check running during acute staffing scenarios without
-- splitting sa_ratio_monitor or adding a new sub-agent.

BEGIN;

UPDATE hospilot_app.task_registry
   SET label      = 'Detect staffing documentation gaps (missing / overdue care notes, charting, unsigned records) by ward — ONLY for administrative/audit/compliance goals; exclude during live emergency, real-time staffing, or float-pool dispatch',
       updated_at = now()
 WHERE id = 'ta_check_documentation_gaps';

COMMIT;
