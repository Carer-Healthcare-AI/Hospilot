-- 062_advisory_rules_staffing.sql
--
-- Staffing advisory rules (notify-only). Evaluators: eval_staffing_* in
-- workflows/graph/advisory_evaluators.py (Redis staff / staff_roster projections).
-- Thresholds live in each row's params (operator-editable in the DB or via
-- PATCH /api/advisory-rules/{id}).
--
-- Triggers: event-driven on staff/staff_roster changes PLUS a clock fallback.
-- See docs/agentic-framework/ADVISORY_ENGINE.md.
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/062_advisory_rules_staffing.sql
-- (no --track-only needed: only new TABLES need tracking, not rows)
-- Keep in sync with db/init/tenant_template.sql. Idempotent (ON CONFLICT DO NOTHING).

INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('staffing_shortage_detected', 'Staffing', 'Staff shortage detected',
   'On-duty staff below the required minimum',
   'Call in on-call staff and redistribute workload across departments',
   'warning', '{"min_on_duty_staff": 40}',
   '["staff"]', 600, 3600),

  ('staffing_nurse_ratio_below_threshold', 'Staffing', 'Nurse ratio below threshold',
   'A nursing area exceeds the patients-per-nurse threshold',
   'Reallocate nurses to the affected area or call in float-pool nurses',
   'warning', '{"max_patients_per_nurse": 6}',
   '["staff_roster"]', 600, 3600),

  ('staffing_high_absenteeism', 'Staffing', 'High absenteeism',
   'Share of staff off duty exceeds the absenteeism threshold',
   'Activate absence-cover roster and confirm shift backfills',
   'warning', '{"absenteeism_pct_threshold": 15}',
   '["staff"]', 600, 3600),

  ('staffing_overtime_above_limit', 'Staffing', 'Overtime above limit',
   'Staff recorded overtime hours above the per-staff limit',
   'Rotate rest, backfill shifts and review overtime spend',
   'info', '{"overtime_hours_limit": 12, "min_staff_over": 1}',
   '["staff"]', 900, 3600),

  ('staffing_icu_shortage', 'Staffing', 'ICU staffing shortage',
   'ICU nurse-to-patient load exceeds policy',
   'Assign additional ICU-certified nurses or trigger critical-care cover',
   'critical', '{"roster_area": "icu", "max_patients_per_nurse": 2}',
   '["staff_roster", "admission"]', 600, 1800)
ON CONFLICT (rule_key) DO NOTHING;
