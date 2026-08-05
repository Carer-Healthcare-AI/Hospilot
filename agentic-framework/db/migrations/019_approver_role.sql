-- Migration 019: Add 'approver' role
-- Approvers see the Approvals screen only; doctors and admins no longer do.
ALTER TABLE hospilot_app.users
  DROP CONSTRAINT IF EXISTS users_role_check;

ALTER TABLE hospilot_app.users
  ADD CONSTRAINT users_role_check
  CHECK (role IN ('doctor', 'admin', 'approver'));
