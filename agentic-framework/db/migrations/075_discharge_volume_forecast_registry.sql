-- ============================================================
-- 075_discharge_volume_forecast_registry.sql
-- Registers sa_discharge_volume (discharge_agent) and its task
-- ta_forecast_discharge_volume -- hospital-wide discharge-volume
-- forecast (beds freed + completion %) over a goal-derived horizon
-- via Hospilot /discharge/volume (util/forecast_client.py).
-- Executed by run_discharge_body in workflows/graph/agents/clinical.py.
-- Parent discharge_agent is upserted first (pre-005 seed may not have
-- carried into hospilot_app in every DB -- avoids the FK trap).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/075_discharge_volume_forecast_registry.sql
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
  ('sa_discharge_volume', 'discharge_agent', 'Discharge Volume Forecast',
   'Forward-looking HOSPITAL-WIDE forecast of how many patients will be discharged over a horizon inferred from the request (shift/24h/3d/7d), plus beds freed and expected completion %. Include when the goal asks how many discharges to expect, predicted discharge throughput, or beds freeing up over a horizon. Distinct from the per-patient readiness assessment (sa_discharge_ready) and retrospective review — it projects aggregate volume, not who is ready now.',
   '["Discharge Volume","Beds Freed","Throughput Forecast"]', false, 30)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_discharge_volume', 'sa_discharge_volume',
   'Forecast hospital-wide discharge volume over a horizon derived from the goal, with beds freed and expected completion %, from current census, the discharge outlook and the medically-cleared count',
   '["forecast_available","predicted_discharges","beds_freed","capacity_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast hospital-wide discharge volume over a horizon derived from the goal, with beds freed and expected completion %, from current census, the discharge outlook and the medically-cleared count',
       updated_at = now()
 WHERE id = 'ta_forecast_discharge_volume';
