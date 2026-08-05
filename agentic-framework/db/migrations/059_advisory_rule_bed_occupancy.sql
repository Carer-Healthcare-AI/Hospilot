-- 059_advisory_rule_bed_occupancy.sql
--
-- First advisory rule: bed occupancy > 90% -> "Prioritize discharge-ready
-- patients and optimize bed allocation". Evaluator: eval_bed_occupancy_high in
-- workflows/graph/advisory_evaluators.py (hasura.get_beds_summary()).
-- Threshold lives in params (occupancy_pct_threshold) -- operator-editable in
-- the DB or via PATCH /api/advisory-rules/{id}.
--
-- Triggers: bed/admission change events (seconds-fast when Kafka is on) PLUS a
-- 5-min fallback interval (works Kafka-off; re-alerts after cooldown in quiet
-- periods). See docs/agentic-framework/ADVISORY_ENGINE.md.
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/059_advisory_rule_bed_occupancy.sql
-- (no --track-only needed: only new TABLES need tracking, not rows)
-- Keep in sync with db/init/tenant_template.sql. Idempotent: safe to re-run --
-- ON CONFLICT DO NOTHING never clobbers operator-edited thresholds.

INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('bed_occupancy_high', 'Bed Management', 'High bed occupancy',
   'Bed occupancy > 90%',
   'Prioritize discharge-ready patients and optimize bed allocation',
   'warning', '{"occupancy_pct_threshold": 90}', '["bed", "admission"]', 300, 3600)
ON CONFLICT (rule_key) DO NOTHING;
