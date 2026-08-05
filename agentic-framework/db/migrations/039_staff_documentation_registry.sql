-- Migration 039: register the staffing documentation-gap task so the planner can
-- select it for Q10 ("flag staffing-related documentation gaps") -- planner-query-gaps G37.
--
-- ta_check_documentation_gaps runs under sa_ratio_monitor (sort 18, after area
-- staffing, before analyze). It derives incomplete/overdue documentation-type nursing
-- tasks (care notes, charting, signatures, shift records) by ward from the same data
-- as ward workload -- staff previously measured only nurse-to-patient ratios.
--
-- Mirrors the seed literals in 003_agent_registry_seed.sql. Schema hospilot_app
-- (planner's registry). Idempotent.

BEGIN;

INSERT INTO "hospilot_app".task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  ('ta_check_documentation_gaps', 'sa_ratio_monitor',
   'Detect staffing documentation gaps (missing / overdue care notes, charting, unsigned records) by ward',
   '["documentation_tasks_pending","documentation_tasks_overdue","by_ward","flagged_wards","has_gaps"]', true, false, 18)
ON CONFLICT (id) DO UPDATE SET
  label       = EXCLUDED.label,
  outputs     = EXCLUDED.outputs,
  subagent_id = EXCLUDED.subagent_id,
  sort_order  = EXCLUDED.sort_order,
  is_active   = true,
  updated_at  = now();

COMMIT;
