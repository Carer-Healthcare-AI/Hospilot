-- Migration 032: register the hourly-workload task for the staffing agent so the
-- planner can select it for "peak understaffed hours" goals (planner-query-gaps G15).
--
-- ta_get_hourly_workload runs under sa_ratio_monitor between ta_get_ward_workload (10)
-- and ta_analyze_staff_workload (20). It buckets incomplete clinical-task load by hour
-- of day and emits peak/understaffed hours, which the appointment reschedule task (G14)
-- consumes via ctx to move non-urgent appointments away from peak hours.
--
-- Mirrors the seed literals in 003_agent_registry_seed.sql. Schema hospilot_app
-- (planner's registry). Idempotent.

BEGIN;

INSERT INTO "hospilot_app".task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  ('ta_get_hourly_workload', 'sa_ratio_monitor',
   'Bucket ward task-load by hour of day; flag peak / understaffed hours',
   '["by_hour","peak_hours","understaffed_hours","total_tasks"]', true, false, 15)
ON CONFLICT (id) DO UPDATE SET
  label       = EXCLUDED.label,
  outputs     = EXCLUDED.outputs,
  subagent_id = EXCLUDED.subagent_id,
  sort_order  = EXCLUDED.sort_order,
  is_active   = true,
  updated_at  = now();

COMMIT;
