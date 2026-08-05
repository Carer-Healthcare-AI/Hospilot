-- 071_advisory_fold_params.sql
--
-- Make advisory_rules.definition the SINGLE source of rule logic + config. The
-- 28 declarative rules already carry their thresholds inside definition.condition
-- (migration 070). The 39 handler rules still read thresholds from the legacy
-- `params` column -- fold those into definition.condition.params, then drop the
-- now-redundant `params` column. Operational columns (severity, trigger_entities,
-- schedule, bookkeeping) stay as indexed columns.
--
-- Engine: advisory_conditions.run_condition passes cond.get("params") to handlers.
--
-- Apply: python scripts/migrate_all_tenants.py db/migrations/071_advisory_fold_params.sql
-- (reloads Hasura metadata so the dropped column leaves the GraphQL schema).
-- Keep in sync with db/init/tenant_template.sql. Idempotent: DROP COLUMN IF EXISTS,
-- and the fold is a no-op once params is gone.

UPDATE hospilot_app.advisory_rules
SET definition = jsonb_set(definition, '{condition,params}', COALESCE(params, '{}'::jsonb), true)
WHERE definition ? 'condition'
  AND definition->'condition' ? 'handler'
  AND EXISTS (SELECT 1 FROM information_schema.columns
              WHERE table_schema = 'hospilot_app' AND table_name = 'advisory_rules'
                AND column_name = 'params');

ALTER TABLE hospilot_app.advisory_rules DROP COLUMN IF EXISTS params;
