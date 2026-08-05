-- ============================================================
-- 109_skill_mix_forecast_registry.sql
-- Registers sa_skill_mix (staff_agent) and its task ta_forecast_skill_mix, backed by the Hospilot
-- forecast service (util/forecast_client.py). Parent staff_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/109_skill_mix_forecast_registry.sql
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
  ('sa_skill_mix', 'staff_agent', 'Skill Mix Forecast',
   'Forward-looking forecast of the clinical SKILL MIX -- WHICH specialties will be needed (not just how many staff): concurrent headcount per specialty (ICU nurses, ER nurses, anesthesiologists, respiratory therapists, critical-care physicians, OR staff) over a horizon inferred from the request (3h-3d). Include when the goal asks about specialty/skill requirements, which clinical skills to roster, or specialist coverage over a time horizon. Distinct from sa_shift_coverage / sa_nurse_demand (total or single-role counts) -- this breaks demand down BY SPECIALTY.',
   '["Skill Mix","Specialty Demand","Clinical Skills"]', false, 80)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_skill_mix', 'sa_skill_mix',
   'Forecast which clinical SKILLS will be needed (concurrent headcount per specialty) over a horizon derived from the goal, from census, ventilated patients, ICU load and the surgical schedule',
   '["forecast_available","predicted_skill_requirements","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label = 'Forecast which clinical SKILLS will be needed (concurrent headcount per specialty) over a horizon derived from the goal, from census, ventilated patients, ICU load and the surgical schedule', updated_at = now()
 WHERE id = 'ta_forecast_skill_mix';
