-- Migration 022: OT agent restructure
-- Reconciles the OT agent registry with src/services/planner.py (fallback) and the
-- runtime (src/graph/agents/simple.py). Idempotent — safe to re-run.
--
--   * sa_ot_analysis becomes the terminal synthesis sub-agent (sort_order 40) and
--     gains ta_ot_score_efficiency, moved out of sa_ot_turnaround so it can read the
--     real conflict_count produced by sa_ot_scheduling (previously hardcoded to 0).
--   * ta_get_ot_cases is removed — its post-op bed data is folded into ta_get_ot_census,
--     making census the single OT data-fetch task (no duplicate schedule fetch).
--   * ta_ot_prioritise_cases is removed — subsumed by ta_ot_optimise_slots +
--     ta_analyze_ot_capacity (it was computed at runtime but never surfaced).
--   * Declared outputs for ta_get_ot_census / ta_analyze_ot_capacity are corrected to
--     match the activity return keys (stale outputs fail-open in condition resolution).
--   * Sub-agent ordering fixed: census(10) -> turnaround(20) -> scheduling(30) -> analysis(40).

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Drop superseded tasks
-- ─────────────────────────────────────────────────────────────────────────────
DELETE FROM hospilot.task_registry WHERE id IN ('ta_get_ot_cases', 'ta_ot_prioritise_cases');

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Re-home the efficiency score under the terminal analysis sub-agent
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE hospilot.task_registry
   SET subagent_id = 'sa_ot_analysis',
       label       = 'OT efficiency optimisation — calculate overall OT efficiency score from delays, instrument gaps, and conflicts — always include',
       sort_order  = 10,
       updated_at  = now()
 WHERE id = 'ta_ot_score_efficiency';

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Correct census + capacity declared outputs (and disambiguate emergency labels)
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE hospilot.task_registry
   SET label   = 'Fetch OT schedule, rooms, room status, today''s equipment, and available post-op (ICU/HDU) beds from cache — always include',
       outputs = '["schedule","rooms","room_status","upcoming_surgeries","upcoming_count","post_op_beds_available","icu_available","hdu_available"]',
       updated_at = now()
 WHERE id = 'ta_get_ot_census';

UPDATE hospilot.task_registry
   SET label   = 'Per-case disposition — recommend proceed/delay/escalate for each case from conflicts, emergencies, and capacity — always include',
       outputs = '["case_recommendations","recommendation_count","escalate_count","delay_count","proceed_count","summary"]',
       sort_order = 20,
       updated_at = now()
 WHERE id = 'ta_analyze_ot_capacity';

UPDATE hospilot.task_registry
   SET label = 'Emergency detection — identify emergency and urgent cases in the schedule — always include',
       updated_at = now()
 WHERE id = 'ta_ot_find_emergencies';

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Re-order / relabel sub-agents
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE hospilot.subagent_registry
   SET label        = 'OT Analysis',
       sort_order   = 40,
       description  = 'Terminal synthesis — scores OT efficiency and produces per-case proceed/delay/escalate recommendations from turnaround and scheduling outputs',
       capabilities = '["OT Efficiency Optimisation","Capacity Risk","Conflict Resolution","Per-case Disposition"]',
       updated_at   = now()
 WHERE id = 'sa_ot_analysis';

UPDATE hospilot.subagent_registry
   SET sort_order = 20, updated_at = now()
 WHERE id = 'sa_ot_turnaround';

UPDATE hospilot.subagent_registry
   SET sort_order = 30, updated_at = now()
 WHERE id = 'sa_ot_scheduling';

UPDATE hospilot.subagent_registry
   SET description  = 'Reviews today''s surgical list, theatre status, equipment, and available post-operative (ICU/HDU) beds',
       capabilities = '["Surgical Case List","Theatre Status","Post-op Beds"]',
       updated_at   = now()
 WHERE id = 'sa_ot_census';
