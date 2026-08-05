-- ============================================================
-- 084_pharmacy_stockout_forecast_registry.sql
-- Registers sa_pharmacy_stockout (pharmacy_agent) and its task
-- ta_forecast_pharmacy_stockout -- forecasts medication stock-out
-- events over a goal-derived horizon with stock-out rate, projected
-- low-stock drugs and shortage risk, via Hospilot
-- /pharmacy/stockout-forecast (util/forecast_client.py). Executed by
-- run_pharmacy_body in workflows/graph/agents/simple.py. Distinct from
-- sa_pharmacy_capacity (/pharmacy/demand, mig 055). Parent pharmacy_agent
-- upserted first (mirrors 016).
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/084_pharmacy_stockout_forecast_registry.sql
-- Idempotent -- safe to re-run.
-- ============================================================

INSERT INTO "hospilot_app".agent_registry
  (id, label, description, emoji, color, is_active, sort_order)
VALUES
  ('pharmacy_agent', 'Pharmacy',
   'Manages medication lifecycle: order fulfillment, drug availability, prescription validation, interaction checking, dispensing, substitution, queue optimization, and controlled drug compliance.',
   '💊', '#0ea5e9', true, 140)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  ('sa_pharmacy_stockout', 'pharmacy_agent', 'Stock-out Forecast',
   'Forward-looking forecast of how many medication STOCK-OUT events the pharmacy will see over a horizon inferred from the request (6h-7d), with stock-out rate, projected low-stock drugs and shortage risk. Include when the goal asks about predicted stock-outs, medication shortages, inventory coverage/runway, or reorder urgency over a time horizon. Distinct from sa_stock_monitor (current low-stock snapshot) and sa_pharmacy_capacity (dispensing-demand surge).',
   '["Stock-out Events","Inventory Coverage","Shortage Risk"]', false, 110)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_pharmacy_stockout', 'sa_pharmacy_stockout',
   'Forecast medication stock-out events over a horizon derived from the goal, with stock-out rate, projected low-stock drugs and shortage risk, from current inventory, low-stock count and daily dispensing rate',
   '["forecast_available","predicted_stockout_events","predicted_low_stock_medications","stockout_risk","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast medication stock-out events over a horizon derived from the goal, with stock-out rate, projected low-stock drugs and shortage risk, from current inventory, low-stock count and daily dispensing rate',
       updated_at = now()
 WHERE id = 'ta_forecast_pharmacy_stockout';
