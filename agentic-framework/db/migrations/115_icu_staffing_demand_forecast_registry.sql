-- ============================================================
-- 115_icu_staffing_demand_forecast_registry.sql
-- Registers sa_icu_staffing_demand (icu_agent) and its task ta_forecast_icu_staffing_demand, backed by the Hospilot
-- forecast service (util/forecast_client.py). Parent icu_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/115_icu_staffing_demand_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('icu_agent', 'ICU Operations',
   'Monitors ICU capacity, tracks ventilated patients, and identifies patients ready for step-down',
   '🫀', '#dc2626', true, 20)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_icu_staffing_demand', 'icu_agent', 'ICU Staffing Demand Forecast',
   'Forward-looking forecast of ICU STAFFING DEMAND -- the peak acuity-weighted ICU NURSES required over a horizon inferred from the request (6h-3d), plus hours likely short-staffed. Include when the goal asks about ICU nurse requirements, critical-care staffing gaps, or ICU nurse coverage over a time horizon. Distinct from sa_icu_ventilator_demand (equipment) and sa_nurse_demand (hospital-wide nursing) -- this is the ICU-specific nurse-staffing slice.',
   '["ICU Staffing","Critical Care Nurses","ICU Nurse Gap"]', false, 70)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_icu_staffing_demand', 'sa_icu_staffing_demand',
   'Forecast the peak acuity-weighted ICU NURSES required over a horizon derived from the goal, plus hours likely short-staffed, with a recommended action, from ICU census, ventilated patients, nurses rostered and average LOS',
   '["forecast_available","predicted_peak_nurses_required","hours_likely_short_staffed","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label = 'Forecast the peak acuity-weighted ICU NURSES required over a horizon derived from the goal, plus hours likely short-staffed, with a recommended action, from ICU census, ventilated patients, nurses rostered and average LOS', updated_at = now()
 WHERE id = 'ta_forecast_icu_staffing_demand';
