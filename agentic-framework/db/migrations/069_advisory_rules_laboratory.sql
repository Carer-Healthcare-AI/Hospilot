-- 069_advisory_rules_laboratory.sql
--
-- Laboratory advisory rules. Evaluators: workflows/graph/advisory_evaluators.py
-- (Laboratory section). Data: Redis projections lab:* (orders), lab_result:*,
-- lab_sample:*, lab_analyzer:* -- all four have Kafka topics, so these rules are
-- genuinely event-driven with clock backstops.
--
-- Proxies imposed by the current data (documented in the evaluators):
--   * lab_result has NO communicated/acknowledged field -> the critical rule
--     fires on recent critical-flagged results; the (short) cooldown is the nag
--   * current result flags are only Normal/Low/High -> critical_flags param;
--     add "High"/"Low" per org if those count as critical
--   * no "Rejected" status seen yet -> rejected_statuses matches receipt/
--     collection status (default Rejected+Missing) plus is_misplaced samples
--   * order priority is NULL in current data -> stat_sla applies when priority
--     matches stat_priorities (stat/urgent/asap)
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/069_advisory_rules_laboratory.sql
-- (no --track-only needed: only new TABLES need tracking, not rows)
-- Keep in sync with db/init/tenant_template.sql. Idempotent: safe to re-run --
-- ON CONFLICT DO NOTHING never clobbers operator-edited thresholds.

INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('lab_tat_sla', 'Laboratory', 'Lab turnaround SLA breach',
   'Lab TAT exceeds SLA',
   'Prioritize pending samples',
   'warning', '{"sla_minutes": 120, "stat_sla_minutes": 60, "stat_priorities": ["stat", "urgent", "asap"]}',
   '["lab_order", "lab_result"]', 300, 3600),

  ('lab_critical_result', 'Laboratory', 'Critical result uncommunicated',
   'Critical result pending communication',
   'Notify treating physician immediately',
   'critical', '{"min_pending": 1, "pending_minutes": 15, "max_age_hours": 24, "critical_flags": ["critical", "critical high", "critical low", "panic"]}',
   '["lab_result"]', 120, 900),

  ('lab_analyzer_down', 'Laboratory', 'Analyzer down',
   'Analyzer downtime',
   'Redirect samples to alternate analyzer',
   'warning', '{"min_down": 1, "up_statuses": ["Online"]}',
   '["lab_analyzer"]', 300, 3600),

  ('lab_collection_delayed', 'Laboratory', 'Sample collection delayed',
   'Sample collection delayed',
   'Notify nursing staff',
   'warning', '{"min_pending": 1, "delay_minutes": 60}',
   '["lab_order", "lab_sample"]', 300, 3600),

  ('lab_sample_rejections', 'Laboratory', 'Sample rejections rising',
   'Sample rejection increasing',
   'Alert lab supervisor',
   'warning', '{"max_rejections": 3, "window_hours": 24, "rejected_statuses": ["Rejected", "Missing"]}',
   '["lab_sample"]', 600, 7200)
ON CONFLICT (rule_key) DO NOTHING;
