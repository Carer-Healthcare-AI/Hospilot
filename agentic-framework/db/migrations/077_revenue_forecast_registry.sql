-- ============================================================
-- 077_revenue_forecast_registry.sql
-- Registers sa_rev_forecast (revenue_agent) and its task
-- ta_forecast_revenue -- organization-wide total-revenue forecast
-- (INR) over a goal-derived horizon with per-patient revenue and
-- risk, via Hospilot /revenue/forecast (util/forecast_client.py).
-- Executed by run_revenue_body in workflows/graph/agents/simple.py.
-- Parent revenue_agent is upserted first (pre-005 seed may not have
-- carried into hospilot_app in every DB -- avoids the FK trap).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/077_revenue_forecast_registry.sql
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
  ('sa_rev_forecast', 'revenue_agent', 'Revenue Forecast',
   'Forward-looking ORGANIZATION-WIDE forecast of total hospital revenue (INR) over a horizon inferred from the request (24h/7d/30d), with per-patient revenue and revenue risk. Include when the goal asks about predicted/expected revenue, financial projections, or the revenue KPI over a time horizon. Distinct from the leakage/denial analytics (sa_rev_optimization / sa_rev_denial_prevention) which review current billing, not future revenue.',
   '["Revenue Forecast","Per-patient Revenue","Financial KPI"]', false, 30)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_revenue', 'sa_rev_forecast',
   'Forecast total hospital revenue (INR) over a horizon derived from the goal, with per-patient revenue and risk, from average invoice value and today''s admissions / ER / surgery / lab activity',
   '["forecast_available","predicted_revenue","revenue_per_patient","revenue_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast total hospital revenue (INR) over a horizon derived from the goal, with per-patient revenue and risk, from average invoice value and today''s admissions / ER / surgery / lab activity',
       updated_at = now()
 WHERE id = 'ta_forecast_revenue';
