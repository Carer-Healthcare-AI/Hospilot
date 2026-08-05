-- ============================================================
-- 078_pending_invoice_forecast_registry.sql
-- Registers sa_billing_backlog (billing_agent) and its task
-- ta_forecast_pending_invoices -- forecasts the billing backlog
-- (pending invoice count + value in INR) over a goal-derived horizon
-- with backlog risk, via Hospilot /revenue/pending-invoices
-- (util/forecast_client.py). Executed by run_billing_body in
-- workflows/graph/agents/simple.py. Parent billing_agent is upserted
-- first (pre-005 seed may not have carried into hospilot_app in every
-- DB -- avoids the FK trap).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/078_pending_invoice_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('billing_agent', 'Billing & Insurance',
   'Claim validation, denial risk, insurance eligibility, compliance checks, and payment recovery',
   '📋', '#84cc16', true, 110)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_billing_backlog', 'billing_agent', 'Pending Invoice Forecast',
   'Forward-looking forecast of the BILLING BACKLOG — pending invoice count and value (INR) — over a horizon inferred from the request (24h/7d/30d), with backlog risk (Low/Medium/High). Include when the goal asks about the pending-invoice backlog, billing throughput, or unbilled/uncollected buildup over a time horizon. Distinct from sa_billing_optimization (tracks/chases CURRENT overdue invoices) — this projects the backlog forward.',
   '["Pending Invoices","Billing Backlog","Backlog Risk"]', false, 40)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_pending_invoices', 'sa_billing_backlog',
   'Forecast the billing backlog (pending invoice count + value in INR) over a horizon derived from the goal, with backlog risk, from current pending invoices, invoices created today, avg invoice value, pending claims and today''s discharges',
   '["forecast_available","predicted_pending_invoices","predicted_pending_amount","backlog_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast the billing backlog (pending invoice count + value in INR) over a horizon derived from the goal, with backlog risk, from current pending invoices, invoices created today, avg invoice value, pending claims and today''s discharges',
       updated_at = now()
 WHERE id = 'ta_forecast_pending_invoices';
