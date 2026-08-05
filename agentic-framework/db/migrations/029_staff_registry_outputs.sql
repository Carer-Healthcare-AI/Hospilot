-- Migration 029: correct staffing-agent registry task outputs/labels so they
-- match the keys the runtime actually emits (planner-query-gaps G9).
--
-- WHY: the DB-driven planner reads task_registry.outputs to know what fields a
-- task emits, and the plan UI shows them. The staff rows seeded in 003 drifted
-- from the code:
--   ta_get_ward_workload         emits {"workload": [ward dicts]}  (was breaches/wards)
--   ta_analyze_staff_workload    emits recommendations/high_pressure_wards/summary
--                                                                  (was ratio_breaches/float_needed)
--   ta_create_staff_approval     stores {"created": true}          (was approval_id)
--   ta_confirm_staff_recommendation returns {"status","recommendations"} (was confirmed)
-- The wrong contract mis-feeds typed conditions (they gate on phantom keys) and
-- the UI. The label "Query shift assignments from Redis" was also wrong -- the
-- task reads inpatient admissions (FHIR Encounters) + incomplete clinical tasks.
--
-- IDs are kept stable (FK targets + frontend broadcast). Outputs/label only.
-- Mirrors the seed literals in 003_agent_registry_seed.sql. Schema is
-- hospilot_app (where the planner's Hasura registry query resolves). Idempotent.

BEGIN;

UPDATE "hospilot_app".task_registry
   SET label      = 'Aggregate patients and incomplete/overdue task load per ward (from admissions + clinical tasks)',
       outputs    = '["workload"]'::jsonb,
       updated_at = now()
 WHERE id = 'ta_get_ward_workload';

UPDATE "hospilot_app".task_registry
   SET label      = 'Analyse ward workload; flag high-pressure wards and recommend same-type staff moves (Claude)',
       outputs    = '["recommendations","high_pressure_wards","summary"]'::jsonb,
       updated_at = now()
 WHERE id = 'ta_analyze_staff_workload';

UPDATE "hospilot_app".task_registry
   SET outputs    = '["created"]'::jsonb,
       updated_at = now()
 WHERE id = 'ta_create_staff_approval';

UPDATE "hospilot_app".task_registry
   SET outputs    = '["status","recommendations"]'::jsonb,
       updated_at = now()
 WHERE id = 'ta_confirm_staff_recommendation';

COMMIT;
