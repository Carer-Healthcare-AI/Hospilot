-- ── Agent / Sub-agent / Task Registry ────────────────────────────────────────
-- Three-table hierarchy: agent → subagent → task
-- Tables live in the hospilot schema inside the default database.
-- Run with Database = default in Hasura Raw SQL.

CREATE TABLE IF NOT EXISTS hospilot.agent_registry (
    id          TEXT PRIMARY KEY,                      -- e.g. 'bed_agent'
    label       TEXT        NOT NULL,
    description TEXT        NOT NULL DEFAULT '',
    emoji       TEXT        NOT NULL DEFAULT '🤖',
    color       TEXT        NOT NULL DEFAULT '#94a3b8',
    is_active   BOOLEAN     NOT NULL DEFAULT true,
    sort_order  INT         NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.subagent_registry (
    id                   TEXT PRIMARY KEY,             -- e.g. 'sa_bed_availability'
    agent_id             TEXT        NOT NULL REFERENCES hospilot.agent_registry(id) ON DELETE CASCADE,
    label                TEXT        NOT NULL,
    description          TEXT        NOT NULL DEFAULT '',
    capabilities         JSONB       NOT NULL DEFAULT '[]',
    is_active            BOOLEAN     NOT NULL DEFAULT true,
    is_prefetch_eligible BOOLEAN     NOT NULL DEFAULT false,
    sort_order           INT         NOT NULL DEFAULT 0,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.task_registry (
    id          TEXT PRIMARY KEY,                      -- e.g. 'ta_query_beds'
    subagent_id TEXT        NOT NULL REFERENCES hospilot.subagent_registry(id) ON DELETE CASCADE,
    label       TEXT        NOT NULL,
    description TEXT        NOT NULL DEFAULT '',
    outputs     JSONB       NOT NULL DEFAULT '[]',
    is_active   BOOLEAN     NOT NULL DEFAULT true,
    sort_order  INT         NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_subagent_agent_id ON hospilot.subagent_registry(agent_id);
CREATE INDEX IF NOT EXISTS idx_task_subagent_id  ON hospilot.task_registry(subagent_id);
