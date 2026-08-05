-- ============================================================
-- 105_shift_coverage_forecast_registry.sql
-- Registers sa_shift_coverage (staff_agent) and its task ta_forecast_shift_coverage, backed by the
-- Hospilot staffing forecast service (util/forecast_client.py). Executed
-- by run_staff_body in workflows/graph/agents/clinical.py (folded into the
-- staff `extras`). Parent staff_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/105_shift_coverage_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('staff_agent', 'Staffing',
   'Monitors staffing levels across all wards and deploys additional nurses where needed',
   '👥', '#f59e0b', true, 40)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_shift_coverage', 'staff_agent', 'Shift Coverage Forecast',
   'Forward-looking forecast of the TOTAL STAFF (nursing + medical + support combined) that must be present to safely cover the shift over a horizon inferred from the request (3h-3d), with the gap vs staff on duty. Include when the goal asks about overall shift coverage, total staffing needs, or whether the shift is adequately staffed across all roles. Distinct from sa_nurse_demand / sa_doctor_demand (single-role counts) -- this is the combined all-roles headcount.',
   '["Shift Coverage","Total Staffing","Coverage Gap"]', false, 40)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_shift_coverage', 'sa_shift_coverage',
   'Forecast the total staff (nursing, medical, support) needed to safely cover the shift over a horizon derived from the goal, with the staffing gap and a recommended action, from census, acuity, scheduled staff and ER/ICU/surgery load',
   '["forecast_available","predicted_staff_required","staffing_gap","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label = 'Forecast the total staff (nursing, medical, support) needed to safely cover the shift over a horizon derived from the goal, with the staffing gap and a recommended action, from census, acuity, scheduled staff and ER/ICU/surgery load', updated_at = now()
 WHERE id = 'ta_forecast_shift_coverage';
