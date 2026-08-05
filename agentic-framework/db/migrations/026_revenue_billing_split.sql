-- ─────────────────────────────────────────────────────────────────────────────
-- Revenue / Billing split (2026-06): redraw the line as PREDICT (revenue) vs
-- EXECUTE (billing) to remove the routing ambiguity where either agent could be
-- picked for the same denial / leakage goal.
--
--   revenue_agent  = predict & prevent revenue loss (leakage/optimization +
--                    denial-risk forecasting & prevention) -- self-contained.
--   billing_agent  = execute billing ops (structural claim validation,
--                    collections, single-patient invoice lookup, bill generation).
--
-- Three moves, all safe against the task-level condition engine
-- (conditions resolve only within an agent body):
--   1. Move sa_rev_patient_billing + sa_rev_initiate_billing → billing_agent.
--   2. Retire billing's duplicate sa_denial_prevention (+ its action tasks).
--   3. Drop the duplicate ta_predict_denial_risk from sa_claim_validation.
--
-- Subagent ids keep the sa_rev_* prefix by design (renaming churns rows + every
-- code reference for no functional gain). Idempotent; deactivations are soft
-- (is_active=false) -- the registry query filters on is_active.
-- Mirrors the in-code SUB_AGENTS / AVAILABLE_AGENTS fallback in workflows/planner.py.
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. Reassign the two moved sub-agents to billing_agent (after sa_billing_optimization=30).
UPDATE hospilot_app.subagent_registry
   SET agent_id = 'billing_agent', sort_order = 40
 WHERE id = 'sa_rev_patient_billing';

UPDATE hospilot_app.subagent_registry
   SET agent_id = 'billing_agent', sort_order = 50
 WHERE id = 'sa_rev_initiate_billing';

-- 2. Retire billing's denial prevention -- denial prediction + prevention now live
--    solely in revenue_agent (sa_rev_denial_prevention).
UPDATE hospilot_app.subagent_registry
   SET is_active = false
 WHERE id = 'sa_denial_prevention' AND agent_id = 'billing_agent';

UPDATE hospilot_app.task_registry
   SET is_active = false
 WHERE id IN (
   'ta_trigger_presubmission_review',
   'ta_apply_stricter_validation',
   'ta_escalate_claim_review_priority'
 );

-- 3. Drop the duplicate denial predictor from sa_claim_validation (revenue owns
--    denial prediction via ta_predict_denial_risk_rev). claim_validation is now
--    pure structural checks (discrepancies, eligibility, compliance).
UPDATE hospilot_app.task_registry
   SET is_active = false
 WHERE id = 'ta_predict_denial_risk' AND subagent_id = 'sa_claim_validation';

-- 4. Re-frame the two agent descriptions (predict vs execute). Wording matches
--    AVAILABLE_AGENTS in workflows/planner.py.
UPDATE hospilot_app.agent_registry
   SET description = 'PREDICT & PREVENT revenue loss: hospital-wide billing-gap & leakage review, package/department profitability, resource utilization, AND insurance denial-risk PREDICTION & PREVENTION (forecast risk, pre-submission validation, payer rules, missing docs). Include when the goal is explicitly financial/analytical (billing review, leakage/profitability analysis, denial-risk review, financial health, daily/shift briefing, performance review). Do NOT add to acute clinical goals (triage, ICU, bed placement, staffing, pharmacy). Does NOT create bills or look up a single patient''s invoices -- that is billing_agent.'
 WHERE id = 'revenue_agent';

UPDATE hospilot_app.agent_registry
   SET description = 'EXECUTE billing operations: structural claim validation (discrepancies, eligibility, compliance), collections / payment recovery, single-patient invoice & claim LOOKUP, and BILL GENERATION. Include for explicit claims-quality / compliance / collection goals, OR to CREATE/RAISE/GENERATE a bill, OR to look up one named patient''s invoices/claims. task_type "initiate_billing" creates a bill for a resolved patient (lead with patient_verification_agent); "patient_billing" is a read-only invoice/claim lookup of one named patient; omit task_type for hospital-wide claims/collections review.'
 WHERE id = 'billing_agent';
