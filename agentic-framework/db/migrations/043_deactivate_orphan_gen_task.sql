-- ─────────────────────────────────────────────────────────────────────────────
-- Gap G45 — Orphan / typo filler dynamic task with unclear purpose.
--
-- ta_gen_4c925e3c ("Lookup patient names and token", with a typo) is a user-added
-- dynamic task (POST /registry/tasks -> is_dynamic=true). It passed the
-- data-availability guardrail but duplicates patient_verification_agent and its
-- output has no downstream consumer, so the planner selected it into query #40's
-- plan ("ER wait 4.2h, SLA breach, 28 in queue") as dead-weight filler.
--
-- Fix: soft-remove it. Setting is_active=false drops the task from BOTH
-- planner-facing paths -- fetch_agent_registry (plan catalog) and
-- fetch_all_function_codes (worker function load) both filter is_active=true --
-- so it can never be selected or executed again, while preserving the row and
-- its generated code for audit. Idempotent: 0-row no-op if already removed.
--
-- NOTE: the id is instance-specific (ta_gen_<random hex>). Confirm it exists in
-- your DB first:
--   SELECT id, subagent_id, label, is_active FROM hospilot_app.task_registry
--    WHERE id = 'ta_gen_4c925e3c';
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE hospilot_app.task_registry
   SET is_active  = false,
       updated_at = now()
 WHERE id = 'ta_gen_4c925e3c';
