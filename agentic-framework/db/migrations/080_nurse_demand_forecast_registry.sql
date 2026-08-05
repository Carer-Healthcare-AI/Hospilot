-- ============================================================
-- 080_nurse_demand_forecast_registry.sql
-- Registers sa_nurse_demand (staff_agent) and its task
-- ta_forecast_nurse_demand -- forecasts required nurses over a
-- goal-derived horizon with the staffing gap and patient-care risk,
-- via Hospilot /staffing/nurse-demand (util/forecast_client.py).
-- Executed by run_staff_body in workflows/graph/agents/clinical.py.
-- Parent staff_agent is upserted first (pre-005 seed may not have
-- carried into hospilot_app in every DB -- avoids the FK trap).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/080_nurse_demand_forecast_registry.sql
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
  ('sa_nurse_demand', 'staff_agent', 'Nurse Demand Forecast',
   'Forward-looking forecast of how many NURSES will be required over a horizon inferred from the request (6h-7d) from expected patient load and acuity, with the staffing gap and patient-care risk. Include when the goal asks about predicted nurse demand, future staffing gaps, or nurse requirements over a time horizon. Distinct from sa_ratio_monitor (measures CURRENT ward ratios) and sa_float_pool (deploys nurses now).',
   '["Nurse Demand","Staffing Gap","Care Risk"]', false, 30)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_nurse_demand', 'sa_nurse_demand',
   'Forecast required nurses over a horizon derived from the goal, with the staffing gap and patient-care risk, from census, patient acuity, current nurses on duty, and ER/ICU/discharge load',
   '["forecast_available","predicted_required_nurses","staffing_gap","patient_care_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast required nurses over a horizon derived from the goal, with the staffing gap and patient-care risk, from census, patient acuity, current nurses on duty, and ER/ICU/discharge load',
       updated_at = now()
 WHERE id = 'ta_forecast_nurse_demand';
