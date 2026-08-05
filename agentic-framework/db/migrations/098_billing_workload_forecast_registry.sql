-- ============================================================
-- 098_billing_workload_forecast_registry.sql
-- Registers sa_billing_workload (billing_agent) and its task
-- ta_forecast_billing_workload -- forecasts total billing work items
-- arriving over a goal-derived horizon (for billing-department staffing)
-- with a workload status, via Hospilot /revenue/workload-forecast
-- (util/forecast_client.py). Executed by run_billing_body in
-- workflows/graph/agents/simple.py. Parent billing_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/098_billing_workload_forecast_registry.sql
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
  ('sa_billing_workload', 'billing_agent', 'Billing Workload Forecast',
   'Forward-looking forecast of total BILLING WORKLOAD -- the volume of billing work items arriving over a horizon inferred from the request (6h-7d), for billing-department staffing, with a workload status (minimal/low/moderate/high) vs staff capacity. Include when the goal asks about billing team workload, staffing needs, or whether the billing department can keep up. Distinct from sa_billing_backlog (pending-invoice COUNT/VALUE) and sa_billing_delay (SLA-breach RATE) -- this projects total WORK VOLUME for staffing.',
   '["Billing Workload","Billing Staffing","Work Volume"]', false, 30)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_billing_workload', 'sa_billing_workload',
   'Forecast total billing work items arriving over a horizon derived from the goal (for billing-department staffing), with a workload status and recommended action, from new/pending/completed cases, billing staff, pending claims and discharges',
   '["forecast_available","predicted_workload","workload_status","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast total billing work items arriving over a horizon derived from the goal (for billing-department staffing), with a workload status and recommended action, from new/pending/completed cases, billing staff, pending claims and discharges',
       updated_at = now()
 WHERE id = 'ta_forecast_billing_workload';
