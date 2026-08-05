-- 054_scheduled_queries.sql
--
-- Scheduled recurring queries (autonomous mode, Phase 6). Lets an operator save a
-- query and have the system re-run it as an unattended background job on a cadence
-- (a fixed interval like every 6h/24h, or a cron calendar). A background loop
-- (workflows/graph/scheduler.py) scans this table each tick and fires every due row
-- down the existing autonomous submission path (hasura.create_session(autonomous=True)
-- -> runner.start_planning(autonomous=True)); the spawned session is linked back via
-- sessions.scheduled_query_id.
--
-- Per-tenant app tables, so this MUST be applied through
-- scripts/migrate_all_tenants.py (runs against every org's Hasura source, including
-- the default/Carer source). Because it CREATEs a new table, track it afterwards:
--   python scripts/migrate_all_tenants.py db/migrations/054_scheduled_queries.sql
--   python scripts/migrate_all_tenants.py --track-only scheduled_queries
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

-- ── scheduled_queries ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot_app.scheduled_queries (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name              text,                               -- editable display name
    goal              text NOT NULL,                      -- the query to (re)run each fire
    constraints       text,
    schedule_kind     text NOT NULL DEFAULT 'interval'
        CONSTRAINT scheduled_queries_kind_check
        CHECK (schedule_kind IN ('interval', 'cron')),
    interval_seconds  integer                             -- for kind='interval'
        CONSTRAINT scheduled_queries_interval_positive
        CHECK (interval_seconds IS NULL OR interval_seconds > 0),
    cron_expr         text,                               -- 5-field crontab, for kind='cron'
    timezone          text NOT NULL DEFAULT 'UTC',        -- tz for cron next-fire calc
    enabled           boolean NOT NULL DEFAULT true,      -- pause/resume without deleting
    autonomous        boolean NOT NULL DEFAULT true,      -- runs are always autonomous (unattended)
    next_run_at       timestamptz NOT NULL,               -- when the loop next fires it
    last_run_at       timestamptz,
    last_session_id   uuid,                               -- most-recent spawned session (overlap check)
    run_count         integer NOT NULL DEFAULT 0,
    user_id           uuid,                               -- owner (control-plane users.id, no cross-DB FK)
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scheduled_queries_due  ON hospilot_app.scheduled_queries (enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_scheduled_queries_user ON hospilot_app.scheduled_queries (user_id);

DROP TRIGGER IF EXISTS set_hospilot_app_scheduled_queries_updated_at ON hospilot_app.scheduled_queries;
CREATE TRIGGER set_hospilot_app_scheduled_queries_updated_at
  BEFORE UPDATE ON hospilot_app.scheduled_queries
  FOR EACH ROW EXECUTE FUNCTION hospilot_app.set_current_timestamp_updated_at();

-- ── sessions.scheduled_query_id (run-history link) ───────────────────────────
-- Nullable: normal (ad-hoc) sessions leave it null; scheduler-spawned sessions
-- carry the originating schedule so GET /api/schedules/{id}/runs can list them.
ALTER TABLE hospilot_app.sessions
  ADD COLUMN IF NOT EXISTS scheduled_query_id uuid;

CREATE INDEX IF NOT EXISTS idx_sessions_scheduled_query
  ON hospilot_app.sessions (scheduled_query_id);
