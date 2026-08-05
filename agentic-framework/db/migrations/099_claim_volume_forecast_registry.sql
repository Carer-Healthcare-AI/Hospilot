-- ============================================================
-- 099_claim_volume_forecast_registry.sql
-- Registers sa_rev_claim_volume (revenue_agent) and its task
-- ta_forecast_claim_volume -- forecasts how many insurance claims will be
-- submitted over a goal-derived horizon with a per-staff load status, via
-- Hospilot /revenue/claim-volume (util/forecast_client.py). Executed by
-- run_revenue_body in workflows/graph/agents/simple.py. Parent
-- revenue_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/099_claim_volume_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('revenue_agent', 'Revenue',
   'Monitors outstanding invoices, daily collections, and insurance claims to flag financial risks',
   '💰', '#f97316', true, 100)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_rev_claim_volume', 'revenue_agent', 'Claim Volume Forecast',
   'Forward-looking forecast of insurance CLAIM SUBMISSION VOLUME -- how many claims will be submitted over a horizon inferred from the request (6h-7d), with a per-staff load status (normal/moderate/high/critical). Include when the goal asks about upcoming claim volume, claims workload/throughput, or claims-team staffing over a time horizon. Distinct from sa_rev_claim_denial (denial RATE of claims) and sa_billing_workload (billing-dept work items) -- this projects claim SUBMISSION count.',
   '["Claim Volume","Claims Workload","Claims Staffing"]', false, 40)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_claim_volume', 'sa_rev_claim_volume',
   'Forecast how many insurance claims will be submitted over a horizon derived from the goal, with a per-staff load status and recommended action, from recent discharges, completed billing cases, pending claims, high-value claims and processing staff',
   '["forecast_available","predicted_claim_volume","load_status","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast how many insurance claims will be submitted over a horizon derived from the goal, with a per-staff load status and recommended action, from recent discharges, completed billing cases, pending claims, high-value claims and processing staff',
       updated_at = now()
 WHERE id = 'ta_forecast_claim_volume';
