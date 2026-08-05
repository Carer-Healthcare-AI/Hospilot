-- ─────────────────────────────────────────────────────────────────────────────
-- Revenue Agent — "Initiate Billing" sub-agent registry rows.
-- Adds the create-a-bill capability so the DB-driven planner routes "initiate
-- billing for patient X" to a dedicated sub-agent instead of falling back to
-- denial-prevention + patient-billing-lookup. Idempotent via ON CONFLICT.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO hospilot_app.subagent_registry (id, agent_id, label, description, is_active, is_prefetch_eligible, sort_order) VALUES
  ('sa_rev_initiate_billing', 'revenue_agent', 'Initiate Billing',
   'Creates a bill-generation request for the resolved patient(s); the DB side turns it into an actual bill when the session is committed',
   true, false, 25)
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot_app.task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  ('ta_create_billing_request', 'sa_rev_initiate_billing',
   'Create a bill-generation request for the resolved patient(s) — always include when the goal is to initiate/generate/raise a bill or invoice for a patient',
   '["billing_requests","patient_count","status"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

-- Sharpen the revenue_agent description so the planner includes it (with
-- task_type 'initiate_billing') for create-a-bill goals.
UPDATE hospilot_app.agent_registry
   SET description = 'owns billing gaps, invoices, collections, claims, financial health, AND initiating bill generation for a patient. Include for explicitly financial/administrative goals (billing review, invoice lookup, collections, claims, financial health, briefing) OR to CREATE/RAISE/GENERATE a bill for a patient. task_type "initiate_billing" creates a bill for a resolved patient; "patient_billing" is a read-only invoice/claim lookup of one named patient; omit task_type for hospital-wide views. Do NOT add to acute clinical goals (triage, ICU, bed placement, staffing, pharmacy).'
 WHERE id = 'revenue_agent';
