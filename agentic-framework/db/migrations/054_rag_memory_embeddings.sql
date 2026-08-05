-- 054_rag_memory_embeddings.sql
--
-- Semantic cross-session fact retrieval: store an OpenAI embedding per fact so
-- read-time ranking is by cosine similarity to the question rather than recency.
-- The vector is JSONB (a float array) because this Postgres has no pgvector;
-- cosine is computed app-side in rag/memory.py. If pgvector is later installed,
-- migrate this column to `vector(1536)` and push the distance sort into SQL.
--
-- Per-tenant: apply via scripts/migrate_all_tenants.py (adds columns to the
-- already-tracked rag_memory on every source; reload_metadata re-exposes them
-- over GraphQL -- no pg_track_table needed for new columns). Idempotent.
-- Keep in sync with db/init/tenant_template.sql.

ALTER TABLE hospilot_app.rag_memory ADD COLUMN IF NOT EXISTS embedding       jsonb;
ALTER TABLE hospilot_app.rag_memory ADD COLUMN IF NOT EXISTS embedding_model text;
ALTER TABLE hospilot_app.rag_memory ADD COLUMN IF NOT EXISTS embedding_dim   integer;
