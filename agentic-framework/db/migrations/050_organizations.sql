-- Migration 050: organizations registry (multi-tenancy, control plane).
--
-- Tenancy model: DB-per-tenant. This table lives ONLY in the control-plane DB
-- (the original hospilot Postgres) and maps each organization to its tenant
-- database + Hasura source. The default tenant "Carer" points at THIS database
-- (hasura_source 'default', root_prefix '') so all pre-existing data belongs to
-- it with zero data migration. New tenants are provisioned with
-- scripts/provision_org.py, which creates hospilot_org_<slug>, applies
-- db/init/tenant_template.sql, and registers the DB as Hasura source
-- 'org_<slug>' with GraphQL root-field prefix 't_<slug>_'.
--
-- Apply live (via Hasura, same as 042):
--   POST {HASURA_URL sans /v1/graphql}/v2/query
--        {"type":"run_sql","args":{"source":"default","sql":"<this file>"}}
-- then track the new table + reload metadata:
--   POST {HASURA_URL sans /v1/graphql}/v1/metadata
--        {"type":"pg_track_table","args":{"source":"default","table":{"schema":"hospilot_app","name":"organizations"}}}
--   POST {HASURA_URL sans /v1/graphql}/v1/metadata
--        {"type":"reload_metadata","args":{"reload_remote_schemas":false}}
-- (header x-hasura-admin-secret)

CREATE TABLE IF NOT EXISTS hospilot_app.organizations (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text NOT NULL UNIQUE,
    slug          text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]*$'),
    status        text NOT NULL DEFAULT 'provisioning'
                  CHECK (status IN ('provisioning', 'active', 'disabled')),
    -- Tenant-DB routing (filled by scripts/provision_org.py)
    db_name       text,                       -- e.g. hospilot_org_acme
    hasura_source text,                       -- e.g. org_acme
    root_prefix   text NOT NULL DEFAULT '',   -- GraphQL root-field prefix, e.g. t_acme_
    created_by    uuid,                       -- users.id of the super_admin who created it
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- Default tenant: the existing DB doubles as Carer's tenant DB (empty prefix,
-- default source). Fixed UUID so backfills in 051 can reference it.
INSERT INTO hospilot_app.organizations (id, name, slug, status, db_name, hasura_source, root_prefix)
VALUES ('00000000-0000-0000-0000-000000000001', 'Carer', 'carer', 'active', NULL, 'default', '')
ON CONFLICT (id) DO NOTHING;
