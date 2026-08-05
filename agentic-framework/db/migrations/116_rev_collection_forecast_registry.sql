-- 116_rev_collection_forecast_registry.sql -- registers sa_rev_collection (revenue_agent) + task ta_forecast_collection.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/116_rev_collection_forecast_registry.sql

INSERT INTO "hospilot_app".agent_registry (id, label, description, emoji, color, is_active, sort_order)
VALUES ('revenue_agent', 'Revenue',
   'Monitors outstanding invoices, daily collections, and insurance claims to flag financial risks',
   '💰', '#f97316', true, 100)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES ('sa_rev_collection', 'revenue_agent', 'Payment Collection Forecast', 'Forward-looking forecast of CASH COLLECTED (payments actually received) over a horizon inferred from the request (24h-30d). Include when the goal asks about expected collections, cash inflow, payment/collections performance, or AR runoff over a time horizon. Distinct from sa_rev_forecast (outstanding-balance level) -- this projects cash COLLECTED, not the balance. Does NOT forecast cash on hand.', '["Payment Collection","Cash Collected","AR Runoff"]', false, 50)
ON CONFLICT (id) DO UPDATE SET agent_id=EXCLUDED.agent_id, label=EXCLUDED.label, description=EXCLUDED.description,
  capabilities=EXCLUDED.capabilities, is_prefetch_eligible=EXCLUDED.is_prefetch_eligible, sort_order=EXCLUDED.sort_order, updated_at=now();

INSERT INTO "hospilot_app".task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES ('ta_forecast_collection', 'sa_rev_collection', 'Forecast the cash actually collected over a horizon derived from the goal, with a recommended action, from collections/day, daily billing run-rate, AR balance, aged AR and the claim-denial rate', '["forecast_available","predicted_payments_collected","collection_status","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry SET label='Forecast the cash actually collected over a horizon derived from the goal, with a recommended action, from collections/day, daily billing run-rate, AR balance, aged AR and the claim-denial rate', updated_at=now() WHERE id='ta_forecast_collection';
