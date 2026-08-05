-- Migration 052: Add an editable display `name` to sessions (Workflows page).
-- The Workflows table lists autonomous runs; each row shows an editable name that
-- defaults to "New Workflow" in the UI until the user renames it. Nullable, so no
-- backfill is needed (the UI renders `name || 'New Workflow'`). Per-tenant column:
-- sessions lives in each org's DB source, so this is applied via migrate_all_tenants.
ALTER TABLE hospilot_app.sessions
  ADD COLUMN IF NOT EXISTS name text;
