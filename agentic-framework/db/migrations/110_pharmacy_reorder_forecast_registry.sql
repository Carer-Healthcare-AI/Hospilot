-- ============================================================
-- 110_pharmacy_reorder_forecast_registry.sql
-- Registers sa_pharmacy_reorder (pharmacy_agent) and its task ta_forecast_pharmacy_reorder, backed by the Hospilot
-- forecast service (util/forecast_client.py). Parent pharmacy_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/110_pharmacy_reorder_forecast_registry.sql
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
  ('sa_pharmacy_reorder', 'pharmacy_agent', 'Reorder Forecast',
   'Forward-looking forecast of how many medication SKUs purchasing should REORDER today -- the share whose days-of-cover drops below the 7-day reorder point over a horizon inferred from the request (24h-14d). Include when the goal asks what to reorder/purchase, procurement/purchasing priorities, or which lines to raise POs for. Distinct from sa_pharmacy_stockout (predicts stock-out EVENTS) and sa_pharmacy_inventory (minimum days of cover) -- this is the purchasing action list.',
   '["Reorder","Purchasing","Procurement"]', false, 20)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_pharmacy_reorder', 'sa_pharmacy_reorder',
   'Forecast how many SKUs will drop below their 7-day reorder point over a horizon derived from the goal, with a recommended action, from days-of-cover, lines below reorder and per-line consumption',
   '["forecast_available","predicted_drugs_to_reorder","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label = 'Forecast how many SKUs will drop below their 7-day reorder point over a horizon derived from the goal, with a recommended action, from days-of-cover, lines below reorder and per-line consumption', updated_at = now()
 WHERE id = 'ta_forecast_pharmacy_reorder';
