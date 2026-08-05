-- ============================================================
-- 114_weekend_discharge_forecast_registry.sql
-- Registers sa_weekend_discharge (discharge_agent) and its task ta_forecast_weekend_discharge, backed by the Hospilot
-- forecast service (util/forecast_client.py). Parent discharge_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/114_weekend_discharge_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('discharge_agent', 'Discharge Planning',
   'Identifies patients ready for discharge, resolves barriers, and generates discharge documentation',
   '📤', '#10b981', true, 50)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_weekend_discharge', 'discharge_agent', 'Weekend Discharge Forecast',
   'Forward-looking forecast of WEEKEND discharge volume -- how many patients will be discharged on a weekend day and across the full weekend. Include when the goal asks specifically about weekend discharges, weekend patient flow, or weekend bed availability. Distinct from sa_discharge_volume (any-horizon total discharges) -- this pins the calendar to the weekend.',
   '["Weekend Discharge","Weekend Flow","Weekend Beds"]', false, 30)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_weekend_discharge', 'sa_weekend_discharge',
   'Forecast discharges on a weekend day and across the full weekend, derived from the goal, with a recommended action, from current census, the discharge outlook and the medically-cleared count',
   '["forecast_available","discharges_per_weekend_day","discharges_full_weekend","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label = 'Forecast discharges on a weekend day and across the full weekend, derived from the goal, with a recommended action, from current census, the discharge outlook and the medically-cleared count', updated_at = now()
 WHERE id = 'ta_forecast_weekend_discharge';
