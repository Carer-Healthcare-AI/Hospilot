-- 063_advisory_rules_ambulance.sql
--
-- Ambulance advisory rules (notify-only). Evaluators: eval_ambulance_* in
-- workflows/graph/advisory_evaluators.py (Redis ambulance fleet projection;
-- demand-surge reuses the /forecast/er-surge model). Thresholds live in each
-- row's params (operator-editable in the DB or via PATCH /api/advisory-rules/{id}).
--
-- Triggers: event-driven on ambulance changes PLUS a clock fallback.
-- See docs/agentic-framework/ADVISORY_ENGINE.md.
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/063_advisory_rules_ambulance.sql
-- (no --track-only needed: only new TABLES need tracking, not rows)
-- Keep in sync with db/init/tenant_template.sql. Idempotent (ON CONFLICT DO NOTHING).

INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('ambulance_none_available', 'Ambulance', 'No ambulance available',
   'No ambulance available in the active fleet',
   'Dispatch nearest available ambulance',
   'critical', '{"min_available": 1}',
   '["ambulance"]', 120, 900),

  ('ambulance_eta_exceeds_sla', 'Ambulance', 'ETA exceeds SLA',
   'A dispatched ambulance ETA exceeds the SLA',
   'Notify ER and suggest alternate ambulance',
   'warning', '{"sla_minutes": 15, "min_over": 1}',
   '["ambulance"]', 120, 900),

  ('ambulance_multiple_emergency_calls', 'Ambulance', 'Multiple emergency calls',
   'Multiple concurrent emergency calls in progress',
   'Prioritize cases based on severity',
   'warning', '{"max_concurrent_emergencies": 3}',
   '["ambulance"]', 120, 900),

  ('ambulance_maintenance_overdue', 'Ambulance', 'Ambulance maintenance overdue',
   'One or more ambulances are overdue for maintenance',
   'Remove vehicle from active fleet',
   'info', '{"min_overdue": 1}',
   '["ambulance"]', 3600, 86400),

  ('ambulance_demand_surge_predicted', 'Ambulance', 'Demand surge predicted',
   'ML forecast predicts an emergency-demand surge',
   'Position ambulances strategically',
   'info', '{}',
   '["ambulance"]', 900, 3600)
ON CONFLICT (rule_key) DO NOTHING;
