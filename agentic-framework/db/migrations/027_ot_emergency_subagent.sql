-- Migration 027: split OT emergency handling into its own sub-agent
-- Extracts the acuity-reactive concern out of sa_ot_scheduling so each OT
-- sub-agent carries a single subgoal and the emergency path is a clear edge.
-- Idempotent -- safe to re-run. Run with Database = default in Hasura Raw SQL.
--
--   * NEW sa_ot_emergency (sort 35, between scheduling 30 and analysis 40):
--     detects non-elective/emergency cases and plans immediate theatre
--     assignment. find_emergencies always runs; handle_emergencies is gated
--     on emergency_count > 0.
--   * sa_ot_scheduling is refocused to the elective plan only (conflicts,
--     resources, slot/load optimisation) -- emergency capabilities removed,
--     plus the stale "Dynamic OT Prioritisation" capability (its task
--     ta_ot_prioritise_cases was dropped in 022) is cleaned up.
--   * ta_ot_find_emergencies / ta_ot_handle_emergencies are re-homed under
--     sa_ot_emergency. find_emergencies label updated to the Non-Elective model
--     (the activity now classifies via priority AND surgery_type).
--
-- Runtime: run_ot_body (src/graph/agents/simple.py) executes the two tasks in a
-- dedicated sa_ot_emergency block; analyze_ot_capacity (sa_ot_analysis) still
-- reads find_emergencies' output, so emergency stays ordered before analysis.

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. New emergency sub-agent
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO hospilot.subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_ot_emergency', 'ot_agent', 'OT Emergency Response',
   'Acuity-reactive -- detects non-elective/emergency surgical cases and plans immediate theatre assignment for them. The handling step runs only when emergency cases are present.',
   '["Emergency Detection","Emergency OT Handling","Acuity Triage"]', false, 35)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Re-home the two emergency tasks under sa_ot_emergency
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE hospilot.task_registry
   SET subagent_id = 'sa_ot_emergency',
       label       = 'Emergency detection -- identify non-elective and emergency cases in the schedule -- always include',
       sort_order  = 10,
       updated_at  = now()
 WHERE id = 'ta_ot_find_emergencies';

UPDATE hospilot.task_registry
   SET subagent_id = 'sa_ot_emergency',
       label       = 'Emergency OT handling -- plan immediate actions for emergency cases -- condition: ta_ot_find_emergencies.emergency_count > 0',
       sort_order  = 20,
       updated_at  = now()
 WHERE id = 'ta_ot_handle_emergencies';

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Refocus sa_ot_scheduling on the elective plan (drop emergency + stale caps)
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE hospilot.subagent_registry
   SET description  = 'Optimises the elective surgical plan -- detects room/surgeon conflicts, checks resource availability, recommends slot swaps, and balances theatre load',
       capabilities = '["Conflict Detection","Surgery Slot Optimisation","Resource-Aware Scheduling","OT Load Balancing"]',
       updated_at   = now()
 WHERE id = 'sa_ot_scheduling';
