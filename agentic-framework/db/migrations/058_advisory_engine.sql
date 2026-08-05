-- 058_advisory_engine.sql
--
-- Advisory engine base (notify-only rules). The engine (workflows/graph/advisory.py)
-- evaluates advisory_rules and, when a rule's condition holds and it is out of
-- cooldown, inserts a notification row into advisories (served by GET /api/advisories).
-- Rules are triggered two ways, per row:
--   trigger_entities (jsonb list)  -- evaluated when a matching hospilot.data.* Kafka
--                                     change event arrives (event-first, seconds-fast)
--   check_interval_seconds         -- evaluated on a clock cadence (for conditions no
--                                     event can carry: SLA timeouts, forecasts)
-- A rule may use either or both (CHECK below enforces at least one).
--
-- This migration ships NO rules -- the framework only. Rules are contributed as their
-- own numbered migrations inserting one advisory_rules row each with
-- ON CONFLICT (rule_key) DO NOTHING (never clobber operator-edited thresholds).
-- See docs/agentic-framework/ADVISORY_ENGINE.md.
--
-- Per-tenant app tables, so this MUST be applied through
-- scripts/migrate_all_tenants.py (runs against every org's Hasura source, including
-- the default/Carer source). Because it CREATEs new tables, track them afterwards:
--   python scripts/migrate_all_tenants.py db/migrations/058_advisory_engine.sql
--   python scripts/migrate_all_tenants.py --track-only advisory_rules,advisories
-- (Hasura and the LangGraph checkpointer are on DIFFERENT Postgres DBs -- always go
-- through Hasura run_sql, never psycopg.) Idempotent: safe to re-run.
--
-- Keep in sync with db/init/tenant_template.sql (fresh tenants are created from the
-- template, existing tenants get this migration).

CREATE SCHEMA IF NOT EXISTS hospilot_app;

-- updated_at trigger helper (already present in tenant DBs; CREATE OR REPLACE is safe)
CREATE OR REPLACE FUNCTION hospilot_app.set_current_timestamp_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── advisory_rules ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot_app.advisory_rules (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_key               text NOT NULL UNIQUE,   -- binds the row to its python evaluator
    topic                  text NOT NULL,          -- grouping, e.g. 'Bed Management'
    label                  text NOT NULL,          -- becomes the advisory title at fire time
    condition_description  text NOT NULL,          -- human-readable, e.g. 'Bed occupancy > 90%'
    suggested_action       text NOT NULL,          -- what the recipient should do about it
    severity               text NOT NULL DEFAULT 'warning'
        CONSTRAINT advisory_rules_severity_check
        CHECK (severity IN ('info', 'warning', 'critical')),
    params                 jsonb NOT NULL DEFAULT '{}'::jsonb,  -- thresholds etc., operator-editable
    trigger_entities       jsonb NOT NULL DEFAULT '[]'::jsonb,  -- hospilot.data.* entity names
    check_interval_seconds integer                              -- null = event-only rule
        CONSTRAINT advisory_rules_interval_positive
        CHECK (check_interval_seconds IS NULL OR check_interval_seconds > 0),
    cooldown_seconds       integer NOT NULL DEFAULT 3600        -- min gap between fires
        CONSTRAINT advisory_rules_cooldown_nonneg
        CHECK (cooldown_seconds >= 0),
    enabled                boolean NOT NULL DEFAULT true,
    next_check_at          timestamptz DEFAULT now(),           -- clock path only
    last_checked_at        timestamptz,
    last_fired_at          timestamptz,
    fire_count             integer NOT NULL DEFAULT 0,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT advisory_rules_has_trigger
        CHECK (jsonb_array_length(trigger_entities) > 0 OR check_interval_seconds IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_advisory_rules_due ON hospilot_app.advisory_rules (enabled, next_check_at);

DROP TRIGGER IF EXISTS set_hospilot_app_advisory_rules_updated_at ON hospilot_app.advisory_rules;
CREATE TRIGGER set_hospilot_app_advisory_rules_updated_at
  BEFORE UPDATE ON hospilot_app.advisory_rules
  FOR EACH ROW EXECUTE FUNCTION hospilot_app.set_current_timestamp_updated_at();

-- ── advisories (fire records) ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot_app.advisories (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_key         text NOT NULL,          -- no FK: rule rows are editable/deletable
    topic            text NOT NULL,
    severity         text NOT NULL DEFAULT 'warning',
    title            text NOT NULL,          -- rule label at fire time
    detail           text,                   -- human sentence from the evaluator
    data             jsonb NOT NULL DEFAULT '{}'::jsonb,  -- evaluator evidence snapshot
    suggested_action text,
    status           text NOT NULL DEFAULT 'active'
        CONSTRAINT advisories_status_check
        CHECK (status IN ('active', 'acknowledged', 'resolved')),
    acknowledged_by  uuid,                   -- control-plane users.id (no cross-DB FK)
    acknowledged_at  timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_advisories_status_created ON hospilot_app.advisories (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_advisories_rule           ON hospilot_app.advisories (rule_key, created_at DESC);

DROP TRIGGER IF EXISTS set_hospilot_app_advisories_updated_at ON hospilot_app.advisories;
CREATE TRIGGER set_hospilot_app_advisories_updated_at
  BEFORE UPDATE ON hospilot_app.advisories
  FOR EACH ROW EXECUTE FUNCTION hospilot_app.set_current_timestamp_updated_at();
