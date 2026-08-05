-- ============================================================
-- 106_overtime_forecast_forecast_registry.sql
-- Registers sa_overtime_forecast (staff_agent) and its task ta_forecast_overtime, backed by the
-- Hospilot staffing forecast service (util/forecast_client.py). Executed
-- by run_staff_body in workflows/graph/agents/clinical.py (folded into the
-- staff `extras`). Parent staff_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/106_overtime_forecast_forecast_registry.sql
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
  ('sa_overtime_forecast', 'staff_agent', 'Overtime Forecast',
   'Forward-looking forecast of staff OVERTIME HOURS over a horizon inferred from the request (3h-3d). Include when the goal asks about expected overtime, overtime cost/burden, or fatigue-driven overtime risk over a time horizon. Distinct from sa_shift_coverage (headcount needed) and sa_absenteeism_forecast (staff absent) -- this projects overtime HOURS.',
   '["Overtime","Staff Fatigue","Overtime Cost"]', false, 50)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_overtime', 'sa_overtime_forecast',
   'Forecast staff overtime hours over a horizon derived from the goal, with a recommended action, from census, scheduled staff, acuity and ER/ICU/surgery load',
   '["forecast_available","predicted_overtime_hours","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label = 'Forecast staff overtime hours over a horizon derived from the goal, with a recommended action, from census, scheduled staff, acuity and ER/ICU/surgery load', updated_at = now()
 WHERE id = 'ta_forecast_overtime';
