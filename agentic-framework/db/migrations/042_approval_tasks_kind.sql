-- Migration 042: paused-queue `kind` discriminator on approval_tasks (autonomous mode, Phase 4).
--
-- The Paused queue (GET /api/queues/paused) surfaces every flow waiting on a human
-- through one table: approval-waiting, patient-identification, patient-registration,
-- and user-paused flows. `kind` distinguishes them so the reaper (which only times
-- out real approvals) and the queue reader can tell them apart. Existing rows and all
-- current callers default to 'approval', so this is backward compatible.
--
-- Values: approval | patient_identification | patient_registration
--         | step_recommendation | user_paused
--
-- Apply live (Hasura and the checkpointer are on DIFFERENT Postgres DBs here, so this
-- must go through Hasura, not psycopg):
--   POST {HASURA_URL sans /v1/graphql}/v2/query
--        {"type":"run_sql","args":{"source":"default","sql":"<this file>"}}
--   POST {HASURA_URL sans /v1/graphql}/v1/metadata
--        {"type":"reload_metadata","args":{"reload_remote_schemas":false}}
-- (header x-hasura-admin-secret) so the new column is exposed in GraphQL.

ALTER TABLE hospilot_app.approval_tasks
  ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'approval';

-- Phase 4 also introduces a user-cancelled terminal state on sessions. The status
-- column is plain text (no enum type), so 'cancelled' writes without DDL; this block
-- only matters if a CHECK constraint was added out-of-band. Drop-and-recreate is
-- idempotent and widens the allowed set to include 'cancelled'.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'sessions_status_check'
      AND conrelid = 'hospilot_app.sessions'::regclass
  ) THEN
    ALTER TABLE hospilot_app.sessions DROP CONSTRAINT sessions_status_check;
    ALTER TABLE hospilot_app.sessions ADD CONSTRAINT sessions_status_check
      CHECK (status IN ('pending','running','completed','failed','cancelled'));
  END IF;
END $$;

-- approval_tasks.status has a CHECK constraint limiting it to pending|approved|rejected.
-- Phase 4 resolves paused-queue rows to terminal states when a flow leaves the queue:
-- 'resolved' (patient input / user resume) and 'cancelled' (user cancel). Both are
-- non-pending, so the reaper (kind='approval' AND status='pending') and the Paused
-- queue (status='pending') correctly ignore them. Widen the constraint to allow them.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'approval_tasks_status_check'
      AND conrelid = 'hospilot_app.approval_tasks'::regclass
  ) THEN
    ALTER TABLE hospilot_app.approval_tasks DROP CONSTRAINT approval_tasks_status_check;
    ALTER TABLE hospilot_app.approval_tasks ADD CONSTRAINT approval_tasks_status_check
      CHECK (status IN ('pending','approved','rejected','resolved','cancelled'));
  END IF;
END $$;
