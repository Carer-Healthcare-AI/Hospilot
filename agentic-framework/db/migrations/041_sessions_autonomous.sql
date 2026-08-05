-- Migration 041: Add the `autonomous` flag to sessions (autonomous mode, Phase 3).
-- An autonomous query skips the plan-approval wait: the planning graph auto-approves
-- its own plan and launches execution in the background immediately (see
-- workflows/graph/planning_graph._await_plan_approval). Assisted (default) sessions
-- keep parking for human plan approval. Per-query runtime flag -- both modes coexist
-- in the same deployment.
ALTER TABLE hospilot_app.sessions
  ADD COLUMN IF NOT EXISTS autonomous boolean NOT NULL DEFAULT false;
