-- 053_rag_conversation_memory.sql
--
-- Conversation storage + cross-session memory for the RAG/Q&A assistant
-- (POST /api/ask). Per-tenant app tables, so this MUST be applied through
-- scripts/migrate_all_tenants.py (runs against every org's Hasura source,
-- including the default/Carer source) and the three tables MUST then be
-- tracked in Hasura (migrate_all_tenants.py --track-only, or provision_org.py
-- for fresh tenants). Idempotent: safe to re-run.
--
-- Keep in sync with db/init/tenant_template.sql (fresh tenants are created from
-- the template, existing tenants get this migration).

CREATE SCHEMA IF NOT EXISTS hospilot_app;

-- updated_at trigger helper (already present in tenant DBs; CREATE OR REPLACE is safe)
CREATE OR REPLACE FUNCTION hospilot_app.set_current_timestamp_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ── rag_conversation ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot_app.rag_conversation (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            uuid,                       -- control-plane users.id (no cross-DB FK)
    title              text,
    running_summary    text,                       -- rolling summary of turns <= summary_through_seq
    summary_through_seq integer NOT NULL DEFAULT 0, -- highest message seq folded into running_summary
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rag_conversation_user ON hospilot_app.rag_conversation (user_id);
CREATE INDEX IF NOT EXISTS idx_rag_conversation_created ON hospilot_app.rag_conversation (created_at DESC);

DROP TRIGGER IF EXISTS set_hospilot_app_rag_conversation_updated_at ON hospilot_app.rag_conversation;
CREATE TRIGGER set_hospilot_app_rag_conversation_updated_at
  BEFORE UPDATE ON hospilot_app.rag_conversation
  FOR EACH ROW EXECUTE FUNCTION hospilot_app.set_current_timestamp_updated_at();

-- ── rag_message ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot_app.rag_message (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL REFERENCES hospilot_app.rag_conversation(id) ON DELETE CASCADE,
    seq             integer NOT NULL,                 -- 1-based ordering within the conversation
    role            text NOT NULL
        CONSTRAINT rag_message_role_check CHECK (role IN ('user', 'assistant')),
    content         text NOT NULL,
    sql             text,       -- assistant turns: the executed SQL (audit / debug), null otherwise
    mode            text,       -- 'sql' | 'fabric'
    row_count       integer,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT rag_message_conversation_seq_key UNIQUE (conversation_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_rag_message_conversation ON hospilot_app.rag_message (conversation_id, seq);

-- ── rag_memory (cross-session, per user) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot_app.rag_memory (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL,                         -- control-plane users.id (no cross-DB FK)
    kind       text NOT NULL DEFAULT 'semantic'
        CONSTRAINT rag_memory_kind_check CHECK (kind IN ('semantic', 'episodic', 'procedural')),
    content    jsonb NOT NULL DEFAULT '{}'::jsonb,     -- langmem ExtractedMemory content
    salience   real NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rag_memory_user ON hospilot_app.rag_memory (user_id, updated_at DESC);

DROP TRIGGER IF EXISTS set_hospilot_app_rag_memory_updated_at ON hospilot_app.rag_memory;
CREATE TRIGGER set_hospilot_app_rag_memory_updated_at
  BEFORE UPDATE ON hospilot_app.rag_memory
  FOR EACH ROW EXECUTE FUNCTION hospilot_app.set_current_timestamp_updated_at();
