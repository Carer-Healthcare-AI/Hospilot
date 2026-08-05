-- ============================================================
-- 108_workforce_utilization_forecast_registry.sql
-- Registers sa_workforce_utilization (staff_agent) and its task ta_forecast_workforce_utilization, backed by the
-- Hospilot staffing forecast service (util/forecast_client.py). Executed
-- by run_staff_body in workflows/graph/agents/clinical.py (folded into the
-- staff `extras`). Parent staff_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/108_workforce_utilization_forecast_registry.sql
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
  ('sa_workforce_utilization', 'staff_agent', 'Workforce Utilization Forecast',
   'Forward-looking forecast of peak WORKFORCE UTILIZATION -- the worst-hour share of deployable staff capacity consumed over a horizon inferred from the request (3h-3d), plus hours in the critical band. Include when the goal asks about how stretched staff will be, workforce saturation, or peak staff-capacity load over a time horizon. Distinct from sa_shift_coverage (headcount needed) -- this projects capacity UTILIZATION %.',
   '["Workforce Utilization","Staff Saturation","Capacity Load"]', false, 70)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_workforce_utilization', 'sa_workforce_utilization',
   'Forecast the worst-hour percent of deployable workforce capacity consumed over a horizon derived from the goal, plus critical-band hours, with a recommended action, from staff available/on-duty, census, acuity and workload',
   '["forecast_available","predicted_peak_workforce_utilization","predicted_critical_hours","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label = 'Forecast the worst-hour percent of deployable workforce capacity consumed over a horizon derived from the goal, plus critical-band hours, with a recommended action, from staff available/on-duty, census, acuity and workload', updated_at = now()
 WHERE id = 'ta_forecast_workforce_utilization';
