-- Migration 004: dynamic task support
-- Adds is_dynamic flag to task_registry so UI-created tasks are distinguishable
-- from seeded tasks. Also adds description column (used by guardrail prompts).

ALTER TABLE hospilot.task_registry
  ADD COLUMN IF NOT EXISTS is_dynamic   BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS description  TEXT    NOT NULL DEFAULT '';

-- Index for fast lookup of dynamic tasks (e.g. for admin views)
CREATE INDEX IF NOT EXISTS idx_task_registry_is_dynamic
  ON hospilot.task_registry (is_dynamic)
  WHERE is_dynamic = TRUE;
