-- Migration 026: patient registration sub-agent
-- Adds sa_patient_registration (task ta_register_patient) under patient_verification_agent.
-- Idempotent -- safe to re-run. Run with Database = default in Hasura Raw SQL.
--
-- Runtime: the sub-agent is executed inline by run_patient_verification_body
-- (src/graph/agents/simple.py) whenever ta_identify_patients finds incoming patient(s)
-- with no DB record (unknown_count > 0). It requests their registration via Fabric
-- (POST /patients/register), PAUSES the flow on the patient_registration interrupt
-- (graph.patient.register_patients), and rebinds the now-known contexts once Fabric
-- reports the new record(s) back via the `patient` Kafka data event. A long-timeout
-- reaper (graph.reaper.reap_stale_registrations) escalates if staff never register them.
--
-- Seeding it here makes the sub-agent a first-class catalogue entry so the planner and
-- the plan UI can see it; execution does NOT depend on the planner selecting it (it is
-- an always-on safety step gated on the runtime unknown_count).

-- 1. Ensure the parent agent exists (preserve its existing label/desc if already seeded).
INSERT INTO hospilot.agent_registry (id, label, description, emoji, color, sort_order) VALUES
  ('patient_verification_agent', 'Patient Verification',
   'Establishes incoming patient identity (mobile -> patient_token + vitals) and registers patients with no record yet.',
   '🪪', '#14b8a6', 5)
ON CONFLICT (id) DO NOTHING;

-- 2. The registration sub-agent.
INSERT INTO hospilot.subagent_registry (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order) VALUES
  ('sa_patient_registration', 'patient_verification_agent', 'Patient Registration',
   'Registers an incoming patient that has no DB record yet -- sends a registration request to Fabric (forwarded to the DB side, created manually by hospital staff) and pauses the flow until the new record is reported back, then rebinds the patient. Runs only when identification finds an unknown patient.',
   '["New Patient Registration","Fabric Request","Await Confirmation"]', false, 20)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

-- 3. Its single task.
INSERT INTO hospilot.task_registry (id, subagent_id, label, description, outputs, sort_order) VALUES
  ('ta_register_patient', 'sa_patient_registration',
   'Request registration of the unknown incoming patient(s) and pause until created',
   'Posts a registration request to Fabric for each incoming patient with no record, pauses the flow until the DB creates them and Fabric reports back (or the timeout reaper fires), then rebinds the now-known patient context(s).',
   '["requested","registered","still_unknown","patients"]', 10)
ON CONFLICT (id) DO UPDATE SET
  subagent_id = EXCLUDED.subagent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  outputs = EXCLUDED.outputs, sort_order = EXCLUDED.sort_order, updated_at = now();
