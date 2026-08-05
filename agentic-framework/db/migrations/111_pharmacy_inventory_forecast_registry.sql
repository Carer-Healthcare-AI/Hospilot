-- ============================================================
-- 111_pharmacy_inventory_forecast_registry.sql
-- Registers sa_pharmacy_inventory (pharmacy_agent) and its task ta_forecast_pharmacy_inventory, backed by the Hospilot
-- forecast service (util/forecast_client.py). Parent pharmacy_agent upserted first.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/111_pharmacy_inventory_forecast_registry.sql
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
  ('sa_pharmacy_inventory', 'pharmacy_agent', 'Inventory Forecast',
   'Forward-looking forecast of the MINIMUM days of stock cover across a horizon inferred from the request (7d-90d) -- how thin the pharmacy shelf gets at its worst. Include when the goal asks about inventory runway, worst-case stock cover, or medium-term stock adequacy over days/weeks/months. Distinct from sa_pharmacy_stockout (near-term stock-out events) and sa_pharmacy_reorder (what to buy today) -- this projects the minimum cover trough. NOTE: expiry exposure is not sourced (no batch/shelf-life data).',
   '["Inventory Cover","Stock Runway","Minimum Days Stock"]', false, 30)
ON CONFLICT (id) DO UPDATE SET
  agent_id = EXCLUDED.agent_id, label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities, is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  sort_order = EXCLUDED.sort_order, updated_at = now();

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_pharmacy_inventory', 'sa_pharmacy_inventory',
   'Forecast the minimum days of stock cover across a horizon derived from the goal, with a recommended action, from opening stock, per-line consumption and consumption variability',
   '["forecast_available","predicted_minimum_days_of_stock","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label = 'Forecast the minimum days of stock cover across a horizon derived from the goal, with a recommended action, from opening stock, per-line consumption and consumption variability', updated_at = now()
 WHERE id = 'ta_forecast_pharmacy_inventory';
