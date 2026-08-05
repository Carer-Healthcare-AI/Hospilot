-- ============================================================
-- 081_doctor_demand_forecast_registry.sql
-- Registers sa_doctor_demand (staff_agent) and its task
-- ta_forecast_doctor_demand -- forecasts required physicians over a
-- goal-derived horizon with the staffing gap and clinical-capacity
-- risk, via Hospilot /staffing/doctor-demand (util/forecast_client.py).
-- Physician sibling of 080 (sa_nurse_demand). Executed by run_staff_body
-- in workflows/graph/agents/clinical.py. Parent staff_agent is upserted
-- first (pre-005 seed may not have carried into hospilot_app in every DB).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/081_doctor_demand_forecast_registry.sql
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
  ('sa_doctor_demand', 'staff_agent', 'Doctor Demand Forecast',
   'Forward-looking forecast of how many PHYSICIANS/DOCTORS will be required over a horizon inferred from the request (6h-7d) from expected clinical workload (ward, ICU, ER, surgeries, consults), with the staffing gap and clinical-capacity risk. Include when the goal asks about predicted doctor/physician demand, medical-staff coverage gaps, or consultant/registrar requirements over a time horizon. Distinct from sa_nurse_demand (nursing) and sa_ratio_monitor (current ratios).',
   '["Doctor Demand","Staffing Gap","Clinical Capacity Risk"]', false, 40)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_doctor_demand', 'sa_doctor_demand',
   'Forecast required physicians over a horizon derived from the goal, with the staffing gap and clinical-capacity risk, from census, patient acuity, doctors on duty, and ER/ICU/critical/discharge workload',
   '["forecast_available","predicted_required_doctors","staffing_gap","clinical_capacity_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast required physicians over a horizon derived from the goal, with the staffing gap and clinical-capacity risk, from census, patient acuity, doctors on duty, and ER/ICU/critical/discharge workload',
       updated_at = now()
 WHERE id = 'ta_forecast_doctor_demand';
