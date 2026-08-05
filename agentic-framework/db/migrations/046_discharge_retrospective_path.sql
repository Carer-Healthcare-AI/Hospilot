-- Migration 046: add retrospective discharge review path (G21).
-- sa_discharge_ready was being selected for already-discharged / closed encounters
-- because no alternative sub-agent existed. It queries active admissions only
-- (status = 'admitted'), so retrospective goals got an empty result and returned
-- immediately without reviewing any records.
--
-- Fix: add sa_discharge_retrospective + ta_get_discharge_records; update
-- sa_discharge_ready description to exclude retrospective goals so the planner
-- selects the right path.

BEGIN;

UPDATE hospilot_app.subagent_registry
   SET description  = 'Reviews each admitted patient to determine if they are clinically ready for discharge — ONLY for active admissions and forward-looking readiness goals; exclude for retrospective, audit, or post-discharge review',
       updated_at   = now()
 WHERE id = 'sa_discharge_ready';

INSERT INTO hospilot_app.subagent_registry
    (id, agent_id, label, description, sort_order)
VALUES (
    'sa_discharge_retrospective',
    'discharge_agent',
    'Retrospective Discharge Reviewer',
    'Reviews completed/closed discharge encounters for documentation and audit goals — ONLY for retrospective, historical, or post-discharge review; NOT for active admissions',
    15
);

INSERT INTO hospilot_app.task_registry
    (id, subagent_id, label, outputs, sort_order)
VALUES (
    'ta_get_discharge_records',
    'sa_discharge_retrospective',
    'Fetch recently discharged / closed encounters — ONLY for retrospective, audit, or post-discharge review goals; exclude for active admission readiness assessment',
    '["records", "count"]',
    10
);

COMMIT;
