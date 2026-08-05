-- 070_advisory_rules_revenue_cycle.sql
--
-- Revenue Cycle advisory rules. Evaluators: workflows/graph/advisory_evaluators.py
-- (Revenue Cycle section). Data: Redis projections invoice:*/claim:*/payment:*
-- (financial REST syncs -- NO Kafka topics, so all rules are clock-only and
-- freshness is the financial sync cadence).
--
-- Proxies imposed by the data (documented in the evaluators):
--   * no revenue target exists -> "collections below target" = overdue
--     receivables past due_date+grace exceeding max_overdue_amount; the
--     evidence payload is the recovery list
--   * leakage = underpayment gap (claim_amount - approved_amount) on settled
--     claims; unbilled-service leakage is not detectable in this data
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/070_advisory_rules_revenue_cycle.sql
-- (no --track-only needed: only new TABLES need tracking, not rows)
-- Keep in sync with db/init/tenant_template.sql. Idempotent: safe to re-run --
-- ON CONFLICT DO NOTHING never clobbers operator-edited thresholds.

INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('rc_claims_pending', 'Revenue Cycle', 'Claims pending high',
   'Claims pending > threshold',
   'Prioritize claim submission',
   'warning', '{"max_pending": 10, "pending_statuses": ["Submitted", "Query"]}',
   '[]', 1800, 14400),

  ('rc_claim_denial_spike', 'Revenue Cycle', 'Claim denial spike',
   'Claim rejection spike',
   'Launch denial management workflow',
   'warning', '{"max_denials": 3, "window_days": 7}',
   '[]', 1800, 14400),

  ('rc_billing_backlog', 'Revenue Cycle', 'Billing backlog',
   'Billing backlog',
   'Redistribute billing workload',
   'warning', '{"max_draft_invoices": 15}',
   '[]', 1800, 14400),

  ('rc_collections_overdue', 'Revenue Cycle', 'Collections below target',
   'Collections below target',
   'Notify finance team with recovery list',
   'warning', '{"max_overdue_amount": 100000, "overdue_grace_days": 7}',
   '[]', 3600, 28800),

  ('rc_revenue_leakage', 'Revenue Cycle', 'Revenue leakage detected',
   'Revenue leakage detected',
   'Trigger billing audit',
   'warning', '{"min_leakage_amount": 50000, "window_days": 30, "settled_statuses": ["Paid", "Approved"]}',
   '[]', 3600, 28800)
ON CONFLICT (rule_key) DO NOTHING;
