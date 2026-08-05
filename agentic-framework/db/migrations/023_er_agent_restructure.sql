-- Migration 023: ER agent restructure
-- Reconciles the ER agent registry with src/services/planner.py (fallback) and the
-- runtime (src/graph/agents/simple.py). Idempotent — safe to re-run.
--
--   * The 9-task sa_er_triage is split into three cohesive sub-agents:
--       sa_er_triage          — census + scoring spine (always-run)
--       sa_er_acuity_response — code-blue / SpO2 / protocol / specialist (gated on triage flags)
--       sa_er_disposition     — fast-track + admission selection (gated on triage counts)
--     sa_er_boarding is unchanged.
--   * Phantom declared outputs removed: ta_get_er_visits "active", ta_route_fasttrack
--     "routed", ta_select_critical "selected" (none are produced; they fail-open in
--     condition resolution).
--   * ta_triage_patients gains fasttrack_count + admission_candidate_count so the
--     disposition tasks can be conditioned (CTAS 4-5 / CTAS 1-3 presence).
--   * Conditions normalised from shorthand ("condition on ctas1 > 0") to the
--     fully-qualified form ("condition: ta_triage_patients.ctas1 > 0").
--   * Sub-agent ordering: triage(10) -> acuity_response(20) -> disposition(30) -> boarding(40).

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. New sub-agents (must exist before tasks are re-homed under them)
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO hospilot.subagent_registry (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order) VALUES
  ('sa_er_acuity_response', 'er_agent', 'Acuity Response',         'Reactive emergency handling for triaged patients — code-blue, SpO2 stabilization, sepsis/stroke/trauma protocols, specialist paging; runs ONLY when triage flags criticality', '["Code Blue","Stabilization","Clinical Protocol","Specialist Paging"]', false, 20),
  ('sa_er_disposition',     'er_agent', 'Disposition Coordinator', 'Routes triaged patients to their next setting — fast-track/OPD for low acuity, admission selection with bed-type tagging for high acuity',                                  '["Fast-track Routing","Admission Selection","Bed-type Tagging"]',       false, 30)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Relabel / re-order the retained sub-agents
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE hospilot.subagent_registry
   SET label        = 'Triage Monitor',
       description  = 'Pulls the active ER queue and scores/persists CTAS triage for every patient; the always-run spine that emits the criticality counts every other ER sub-agent branches on',
       capabilities = '["ER Queue","CTAS Score","Criticality Flags"]',
       is_prefetch_eligible = true,
       sort_order   = 10,
       updated_at   = now()
 WHERE id = 'sa_er_triage';

UPDATE hospilot.subagent_registry
   SET description  = 'Checks ER boarders (admitted patients still awaiting an inpatient bed) and escalates SLA breaches; independent of the triage flow',
       is_prefetch_eligible = false,
       sort_order   = 40,
       updated_at   = now()
 WHERE id = 'sa_er_boarding';

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Triage spine — fix outputs (drop phantom "active", add disposition counts)
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE hospilot.task_registry
   SET label      = 'Query the active ER visit queue from Redis — always include; the data spine every downstream ER sub-agent reads from',
       outputs    = '["visits"]',
       sort_order = 10,
       updated_at = now()
 WHERE id = 'ta_get_er_visits';

UPDATE hospilot.task_registry
   SET label      = 'Score and triage ER patients with Claude — always include when ER visits exist; emits the criticality counts downstream tasks branch on',
       outputs    = '["triaged","ctas1","ctas2","critical","spo2_critical_count","protocol_flags_count","specialist_needed_count","fasttrack_count","admission_candidate_count"]',
       sort_order = 20,
       updated_at = now()
 WHERE id = 'ta_triage_patients';

UPDATE hospilot.task_registry
   SET label      = 'Persist triage scores to Redis — always include after triage',
       sort_order = 30,
       updated_at = now()
 WHERE id = 'ta_save_triage_scores';

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Re-home the acuity-response tasks (+ normalise conditions)
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE hospilot.task_registry
   SET subagent_id = 'sa_er_acuity_response',
       label       = 'Trigger code-blue workflow when cardiac arrest is suspected — condition: ta_triage_patients.ctas1 > 0',
       sort_order  = 10,
       updated_at  = now()
 WHERE id = 'ta_detect_cardiac_arrest';

UPDATE hospilot.task_registry
   SET subagent_id = 'sa_er_acuity_response',
       label       = 'Escalate stabilization protocol when SpO2 is critically low — condition: ta_triage_patients.spo2_critical_count > 0',
       sort_order  = 20,
       updated_at  = now()
 WHERE id = 'ta_check_spo2_critical';

UPDATE hospilot.task_registry
   SET subagent_id = 'sa_er_acuity_response',
       label       = 'Activate sepsis/stroke/trauma protocol pathway — condition: ta_triage_patients.protocol_flags_count > 0',
       sort_order  = 30,
       updated_at  = now()
 WHERE id = 'ta_detect_clinical_protocol';

UPDATE hospilot.task_registry
   SET subagent_id = 'sa_er_acuity_response',
       label       = 'Notify relevant specialist team based on detected condition — condition: ta_triage_patients.specialist_needed_count > 0',
       sort_order  = 40,
       updated_at  = now()
 WHERE id = 'ta_notify_specialist';

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Re-home the disposition tasks (fix phantom outputs + add conditions)
-- ─────────────────────────────────────────────────────────────────────────────
UPDATE hospilot.task_registry
   SET subagent_id = 'sa_er_disposition',
       label       = 'Route low-acuity patients (CTAS 4-5) to fast-track / OPD diversion — condition: ta_triage_patients.fasttrack_count > 0',
       outputs     = '["fasttrack_candidates"]',
       sort_order  = 10,
       updated_at  = now()
 WHERE id = 'ta_route_fasttrack';

UPDATE hospilot.task_registry
   SET subagent_id = 'sa_er_disposition',
       label       = 'Select critical patients (CTAS 1-3) for admission and tag bed_type_needed for the Bed Agent — condition: ta_triage_patients.admission_candidate_count > 0',
       outputs     = '["critical_patients"]',
       sort_order  = 20,
       updated_at  = now()
 WHERE id = 'ta_select_critical';
