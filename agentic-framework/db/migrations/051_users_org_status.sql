-- Migration 051: users -> org membership + approval status + super_admin role.
-- Requires 050_organizations.sql.
--
-- New-user flow after this migration: signup creates status='pending' (no
-- login possible); the org's admin approves doctors/approvers, super_admin
-- approves admins. Existing users are backfilled into the Carer org and stay
-- 'active' so nobody is locked out. super_admin is the only org-less role
-- (platform level, created by the main.py bootstrap hook -- never via signup).
--
-- Apply live (via Hasura, same as 042):
--   POST {HASURA_URL sans /v1/graphql}/v2/query   {"type":"run_sql", ...}
--   POST {HASURA_URL sans /v1/graphql}/v1/metadata {"type":"reload_metadata", ...}

ALTER TABLE hospilot_app.users
  ADD COLUMN IF NOT EXISTS org_id      uuid REFERENCES hospilot_app.organizations(id),
  ADD COLUMN IF NOT EXISTS status      text NOT NULL DEFAULT 'active'
      CHECK (status IN ('pending', 'active', 'rejected', 'disabled')),
  ADD COLUMN IF NOT EXISTS approved_by uuid REFERENCES hospilot_app.users(id),
  ADD COLUMN IF NOT EXISTS approved_at timestamptz;

-- Backfill: every pre-migration user belongs to the Carer org, already active.
UPDATE hospilot_app.users
SET org_id = '00000000-0000-0000-0000-000000000001'
WHERE org_id IS NULL AND role <> 'super_admin';

-- Widen role set (drop/recreate, pattern from 019_approver_role.sql).
ALTER TABLE hospilot_app.users
  DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE hospilot_app.users
  ADD CONSTRAINT users_role_check
  CHECK (role IN ('doctor', 'admin', 'approver', 'super_admin'));

-- Only super_admin may be org-less.
ALTER TABLE hospilot_app.users
  DROP CONSTRAINT IF EXISTS users_org_required;
ALTER TABLE hospilot_app.users
  ADD CONSTRAINT users_org_required
  CHECK (role = 'super_admin' OR org_id IS NOT NULL);

-- From now on new rows default to pending (signup flow); the backfill above
-- ran while the default was still 'active', so existing users are untouched.
ALTER TABLE hospilot_app.users ALTER COLUMN status SET DEFAULT 'pending';

CREATE INDEX IF NOT EXISTS idx_users_org_status ON hospilot_app.users (org_id, status);
