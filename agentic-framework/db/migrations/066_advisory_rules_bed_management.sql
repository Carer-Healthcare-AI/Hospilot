-- 066_advisory_rules_bed_management.sql
--
-- Remaining 7 Bed Management advisory rules (bed_occupancy_high shipped in 059).
-- Evaluators: workflows/graph/advisory_evaluators.py. Thresholds live in params
-- (operator-editable in the DB or via PATCH /api/advisory-rules/{id}).
-- See docs/agentic-framework/ADVISORY_ENGINE.md.
--
-- Trigger choices: change-driven rules get trigger_entities + a clock fallback
-- (works Kafka-off; re-alerts after cooldown in quiet periods). The forecast and
-- turnaround-SLA rules are clock-only -- no event can carry a time-based condition.
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/066_advisory_rules_bed_management.sql
-- (no --track-only needed: only new TABLES need tracking, not rows)
-- Keep in sync with db/init/tenant_template.sql. Idempotent: safe to re-run --
-- ON CONFLICT DO NOTHING never clobbers operator-edited thresholds.

INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('bed_occupancy_forecast_critical', 'Bed Management', 'Occupancy forecast critical',
   'Bed occupancy predicted >95% in next 6 hours',
   'Run capacity simulation and notify operations command center',
   'critical', '{"predicted_occupancy_pct_threshold": 95, "horizon_hours": 6}',
   '[]', 900, 7200),

  ('dirty_beds_backlog', 'Bed Management', 'Housekeeping backlog',
   'Beds awaiting cleaning > 10',
   'Create housekeeping tasks and reprioritize cleaning staff',
   'warning', '{"max_dirty_beds": 10}',
   '["bed"]', 300, 3600),

  ('er_boarding_pressure', 'Bed Management', 'ER boarding pressure',
   'ER boarding patients > threshold',
   'Reserve next available inpatient beds automatically',
   'warning', '{"max_boarders": 5}',
   '["visit"]', 300, 3600),

  ('isolation_beds_full', 'Bed Management', 'Isolation capacity exhausted',
   'Isolation beds full',
   'Identify suitable isolation candidates and trigger escalation',
   'critical', '{"min_available_isolation_beds": 1}',
   '["bed"]', 600, 7200),

  ('icu_stepdown_pending', 'Bed Management', 'ICU step-down candidates',
   'ICU step-down patients identified',
   'Initiate transfer workflow to ward',
   'info', '{"min_candidates": 1}',
   '["admission", "discharge_ready"]', 900, 14400),

  ('discharged_bed_blocked', 'Bed Management', 'Discharged patient still in bed',
   'Discharged patient still occupying bed',
   'Notify ward, billing, pharmacy, and housekeeping teams',
   'warning', '{"min_blocked_beds": 1}',
   '["bed", "admission", "discharge_ready"]', 600, 3600),

  ('bed_turnaround_sla', 'Bed Management', 'Bed turnaround SLA breach',
   'Bed turnaround time exceeds SLA',
   'Escalate housekeeping and monitor completion',
   'warning', '{"sla_minutes": 90}',
   '[]', 600, 3600)
ON CONFLICT (rule_key) DO NOTHING;
