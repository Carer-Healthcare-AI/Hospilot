-- 067_advisory_rules_ot.sql
--
-- OT (operating theatre) advisory rules. Evaluators:
-- workflows/graph/advisory_evaluators.py (OT section). OT data reaches the
-- backend only via the Redis projection (Kafka hospilot.data.ot_* topics), so
-- these rules are dormant until the data feed is live in the deployment.
--
-- Proxies imposed by the current HIS feed (documented in the evaluators):
--   * no needs-ICU flag on surgeries -> icu_surgery_types param
--   * equipment-usage feed is empty  -> "equipment unavailable" = theatre in
--     Maintenance with cases still scheduled today
--   * priority values are Elective/Non Elective only -> emergency_priorities
--     defaults to Emergency/Urgent (add "Non Elective" per-org if that is the
--     org's emergency semantics)
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/067_advisory_rules_ot.sql
-- (no --track-only needed: only new TABLES need tracking, not rows)
-- Keep in sync with db/init/tenant_template.sql. Idempotent: safe to re-run --
-- ON CONFLICT DO NOTHING never clobbers operator-edited thresholds.

INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('ot_first_case_delayed', 'OT', 'First surgery delayed',
   'First surgery delayed',
   'Notify OT coordinator and adjust downstream schedule',
   'warning', '{"delay_minutes": 15}',
   '["ot_surgery", "ot_schedule"]', 300, 3600),

  ('ot_surgery_overrun', 'OT', 'Surgery overrun',
   'Surgery overrun >30 min',
   'Recalculate OT schedule and notify affected teams',
   'warning', '{"overrun_minutes": 30}',
   '["ot_surgery"]', 300, 3600),

  ('ot_room_idle', 'OT', 'Theatre idle with pending cases',
   'OT idle >1 hour',
   'Suggest advancing next surgery',
   'info', '{"idle_minutes": 60}',
   '["ot_room_status"]', 600, 3600),

  ('ot_emergency_waiting', 'OT', 'Emergency surgery waiting',
   'Emergency surgery waiting',
   'Reprioritize OT schedule automatically',
   'critical', '{"min_waiting": 1, "emergency_priorities": ["Emergency", "Urgent"]}',
   '["ot_surgery", "ot_schedule"]', 300, 1800),

  ('ot_icu_capacity_post_surgery', 'OT', 'ICU capacity short for surgery',
   'ICU bed unavailable after surgery',
   'Delay elective case or reserve ICU capacity',
   'critical', '{"min_free_icu_beds": 1, "lookahead_hours": 4, "icu_surgery_types": ["Cardiac", "Neuro", "Transplant"]}',
   '["ot_surgery", "bed"]', 600, 7200),

  ('ot_equipment_unavailable', 'OT', 'Theatre equipment unavailable',
   'Equipment unavailable',
   'Notify biomedical team and suggest alternate OT',
   'warning', '{"min_affected": 1}',
   '["ot_room_status", "ot_room"]', 600, 3600)
ON CONFLICT (rule_key) DO NOTHING;
