-- Migration 025: relabel sa_icu_transfer -> "ICU Admissions"
-- The sub-agent only handles INCOMING admissions (rank requests, reserve bed,
-- overflow, escalate deterioration); "Transfer" was misleading because transfers
-- OUT of ICU (step-down) are owned by sa_icu_stepdown. ID is kept stable — it is a
-- FK target for the task rows and is broadcast to the frontend by the runtime
-- (temporal/activities/icu_activities.py). Label/description only. Mirrors the
-- planner.py fallback. Idempotent — safe to re-run.

BEGIN;

UPDATE hospilot.subagent_registry
   SET label       = 'ICU Admissions',
       description = 'Handles incoming ICU admissions: ranks pending admission requests by acuity, reserves a bed for the most critical patient, and triggers overflow evaluation when ICU is full. Transfers OUT of ICU (step-down) are handled by the Step-Down Coordinator.',
       updated_at  = now()
 WHERE id = 'sa_icu_transfer';

COMMIT;
