-- ============================================================
-- 107_absenteeism_forecast_forecast_registry.sql
-- Registers sa_absenteeism_forecast (staff_agent) and its task ta_forecast_absenteeism, backed by the
-- Hospilot staffing forecast service (util/forecast_client.py). Executed
-- by run_staff_body in workflows/graph/agents/clinical.py (folded into the
-- staff `extras`). Parent staff_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/107_absenteeism_forecast_forecast_registry.sql
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
  ('sa_absenteeism_forecast', 'staff_agent', 'Absenteeism Forecast',
   'Forward-looking forecast of unplanned staff ABSENCE (headcount + share) over a horizon inferred from the request (3h-3d). Include when the goal asks about expected absenteeism, sickness/no-show among staff, or unplanned coverage loss over a time horizon. Distinct from sa_shift_coverage (staff needed) and sa_overtime_forecast (overtime hours) -- this projects staff ABSENT.',
   '["Absenteeism","Staff Sickness","Coverage Loss"]', false, 60)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_absenteeism', 'sa_absenteeism_forecast',
   'Forecast the number of staff on unplanned absence over a horizon derived from the goal, with a recommended action, from scheduled staff, census, acuity and workload',
   '["forecast_available","predicted_absent_staff","absent_share_pct","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label = 'Forecast the number of staff on unplanned absence over a horizon derived from the goal, with a recommended action, from scheduled staff, census, acuity and workload', updated_at = now()
 WHERE id = 'ta_forecast_absenteeism';
