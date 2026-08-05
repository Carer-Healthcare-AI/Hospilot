-- Migration 035: register the staff-area staffing task so the planner can select it
-- for "check X staffing" goals on non-inpatient-nursing areas -- front desk (Q2),
-- recovery/PACU (Q4), phlebotomy (Q5), OT + recovery (Q6), lab (Q8).
-- (planner-query-gaps G11 + G20 + G24 + G28 + lab-staff.)
--
-- ta_get_area_staffing runs under sa_ratio_monitor (sort 17, after ward + hourly
-- workload, before analyze). It reads hospilot.staff_roster, scopes to the goal's
-- area(s) + current shift, and flags understaffed areas with recommended adds.
--
-- Mirrors the seed literals in 003_agent_registry_seed.sql. Schema hospilot_app
-- (planner's registry). Idempotent.

BEGIN;

INSERT INTO "hospilot_app".task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  ('ta_get_area_staffing', 'sa_ratio_monitor',
   'Assess staffing for a specific area (front desk, OPD, phlebotomy, OT, recovery/PACU, lab, inpatient nursing); flag understaffed areas',
   '["shift","areas_assessed","areas","understaffed_areas"]', true, false, 17)
ON CONFLICT (id) DO UPDATE SET
  label       = EXCLUDED.label,
  outputs     = EXCLUDED.outputs,
  subagent_id = EXCLUDED.subagent_id,
  sort_order  = EXCLUDED.sort_order,
  is_active   = true,
  updated_at  = now();

COMMIT;
