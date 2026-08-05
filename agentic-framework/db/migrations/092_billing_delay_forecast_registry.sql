-- ============================================================
-- 092_billing_delay_forecast_registry.sql
-- Registers sa_billing_delay (billing_agent) and its task
-- ta_forecast_billing_delay -- forecasts the SLA-breach share (%) of
-- billing cases over a goal-derived horizon with a delay severity, via
-- Hospilot /revenue/billing-delay (util/forecast_client.py). Executed by
-- run_billing_body in workflows/graph/agents/simple.py. Parent
-- billing_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/092_billing_delay_forecast_registry.sql
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
  ('sa_billing_delay', 'billing_agent', 'Billing Delay Forecast',
   'Forward-looking forecast of the SLA-BREACH SHARE of billing cases — the percentage of due bills expected to miss the billing turnaround target — over a horizon inferred from the request (24h/3d/7d), with a delay severity (moderate/high/critical). Include when the goal asks about billing SLA/turnaround-target risk, expected billing delays, or how much of the queue will age out. Distinct from sa_billing_backlog (projects the pending-invoice COUNT/VALUE) — this projects the SLA-breach RATE.',
   '["Billing SLA","Turnaround Risk","Billing Delay"]', false, 20)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_billing_delay', 'sa_billing_delay',
   'Forecast the share of billing cases that will breach the SLA target (%) over a horizon derived from the goal, with delay severity, from pending cases, new cases today, currently delayed cases, billing staff, pending claims and high-value bills',
   '["forecast_available","predicted_sla_breach_share_pct","delay_severity","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast the share of billing cases that will breach the SLA target (%) over a horizon derived from the goal, with delay severity, from pending cases, new cases today, currently delayed cases, billing staff, pending claims and high-value bills',
       updated_at = now()
 WHERE id = 'ta_forecast_billing_delay';
