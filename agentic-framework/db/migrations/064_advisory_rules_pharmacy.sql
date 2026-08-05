-- 064_advisory_rules_pharmacy.sql
--
-- Pharmacy advisory rules (notify-only). Evaluators: eval_pharmacy_* in
-- workflows/graph/advisory_evaluators.py (Fabric /pharmacy/* reads). Thresholds
-- live in each row's params (operator-editable in the DB or via
-- PATCH /api/advisory-rules/{id}).
--
-- Triggers: event-driven on pharmacy_order/pharmacy_inventory changes PLUS a
-- clock fallback. See docs/agentic-framework/ADVISORY_ENGINE.md.
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/064_advisory_rules_pharmacy.sql
-- (no --track-only needed: only new TABLES need tracking, not rows)
-- Keep in sync with db/init/tenant_template.sql. Idempotent (ON CONFLICT DO NOTHING).

INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('pharmacy_drug_out_of_stock', 'Pharmacy', 'Drug out of stock',
   'One or more drugs have zero stock on hand',
   'Recommend substitute medication and notify procurement',
   'critical', '{"min_out_of_stock": 1}',
   '["pharmacy_inventory"]', 600, 3600),

  ('pharmacy_queue_increasing', 'Pharmacy', 'Pharmacy queue increasing',
   'Undispensed pharmacy orders exceed the queue threshold',
   'Open additional dispensing counter',
   'warning', '{"max_queue": 5}',
   '["pharmacy_order"]', 300, 1800),

  ('pharmacy_delivery_delayed', 'Pharmacy', 'Medication delivery delayed',
   'Medication orders undispensed beyond the delivery SLA',
   'Prioritize discharge medications',
   'warning', '{"delay_minutes": 120, "min_delayed": 1}',
   '["pharmacy_order"]', 300, 1800),

  ('pharmacy_controlled_discrepancy', 'Pharmacy', 'Controlled drug discrepancy',
   'Controlled-drug log shows a count variance or incomplete documentation',
   'Trigger compliance audit',
   'critical', '{"min_issues": 1, "lookback_hours": 24}',
   '["pharmacy_order"]', 600, 1800),

  ('pharmacy_inventory_below_reorder', 'Pharmacy', 'Inventory below reorder level',
   'Inventory items at or below their reorder level',
   'Create procurement request',
   'warning', '{"min_below": 1}',
   '["pharmacy_inventory"]', 600, 3600)
ON CONFLICT (rule_key) DO NOTHING;
