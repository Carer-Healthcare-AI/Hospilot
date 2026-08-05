-- ============================================================
-- 093_claim_denial_forecast_registry.sql
-- Registers sa_rev_claim_denial (revenue_agent) and its task
-- ta_forecast_claim_denial -- forecasts the insurance claim denial RATE
-- (%) and denied-value rate over a goal-derived horizon with a denial
-- status, via Hospilot /revenue/claim-denial (util/forecast_client.py).
-- Executed by run_revenue_body in workflows/graph/agents/simple.py.
-- Parent revenue_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/093_claim_denial_forecast_registry.sql
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
  ('sa_rev_claim_denial', 'revenue_agent', 'Claim Denial Forecast',
   'Forward-looking forecast of the insurance CLAIM DENIAL RATE (% of claims, plus denied-value %) over a horizon inferred from the request (24h/3d/7d), with a denial status (on_target/watch/act). Include when the goal asks about future/expected denial rate, denial trend, or how many claims/how much value will be denied over a time horizon. Distinct from sa_rev_denial_prevention (pre-submission per-claim risk scoring & validation on CURRENT claims) -- this projects the aggregate denial RATE forward.',
   '["Claim Denial","Denial Rate","Payer Risk"]', false, 30)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_claim_denial', 'sa_rev_claim_denial',
   'Forecast the insurance claim denial RATE (%) and denied-value rate over a horizon derived from the goal, with a denial status and recommended action, from claims submitted (7d), the trailing-30d denial rate, average claim amount, high-value claims and processing staff',
   '["forecast_available","predicted_denial_rate","predicted_denied_value_rate","denial_status","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast the insurance claim denial RATE (%) and denied-value rate over a horizon derived from the goal, with a denial status and recommended action, from claims submitted (7d), the trailing-30d denial rate, average claim amount, high-value claims and processing staff',
       updated_at = now()
 WHERE id = 'ta_forecast_claim_denial';
