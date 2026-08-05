-- Migration 024: ICU agent registry corrections
-- Reconciles the icu_agent registry with src/services/planner.py (fallback) and the
-- runtime (src/graph/agents/clinical.py + temporal/activities/icu_activities.py).
-- Idempotent — safe to re-run.
--
--   * ta_get_icu_census declared outputs corrected to the keys the activity actually
--     returns (removes phantom occupancy_pct/ventilator_beds and the step_down/
--     escalation candidates that are really produced by sa_icu_stepdown — they were
--     declared under two sub-agents, making output ownership ambiguous for edges).
--   * ta_analyze_icu_status outputs corrected (step_down/escalations were broadcast
--     counts, never return keys) and gains transfer_candidate_count.
--   * ta_create_icu_approval is now gated on transfer_candidate_count > 0 (fires when
--     there is a step-down OR escalation candidate; single-symbol since typed
--     conditions have no OR) and its output corrected to {created}.
--   * ta_confirm_icu_actions outputs corrected to the staged keys it returns.
--   * sa_icu_census description/capabilities tightened to data-only (step-down flagging
--     belongs to the Step-Down Coordinator).

BEGIN;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Tasks
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE hospilot.task_registry
   SET label   = 'Query ICU occupancy, current admissions, and available beds from Redis',
       outputs = '["icu_available","available_beds","icu_admissions","non_icu_admissions"]',
       updated_at = now()
 WHERE id = 'ta_get_icu_census';

UPDATE hospilot.task_registry
   SET label   = 'Analyse step-down and escalation eligibility with Claude',
       outputs = '["step_down_candidates","escalation_candidates","summary","critical_vital_ids","transfer_candidate_count"]',
       updated_at = now()
 WHERE id = 'ta_analyze_icu_status';

UPDATE hospilot.task_registry
   SET label   = 'Create ICU transfer approval task in Hasura — condition: ta_analyze_icu_status.transfer_candidate_count > 0',
       outputs = '["created"]',
       updated_at = now()
 WHERE id = 'ta_create_icu_approval';

UPDATE hospilot.task_registry
   SET outputs = '["critical_vitals_flagged","transfers_staged"]',
       updated_at = now()
 WHERE id = 'ta_confirm_icu_actions';

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Sub-agent
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE hospilot.subagent_registry
   SET description  = 'Reviews current ICU occupancy, active admissions, and available beds (data only; step-down flagging belongs to the Step-Down Coordinator)',
       capabilities = '["ICU Occupancy","Available Beds","Ward Admissions"]',
       updated_at   = now()
 WHERE id = 'sa_icu_census';

COMMIT;
