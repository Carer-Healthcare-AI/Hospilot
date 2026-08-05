-- 061_advisory_rules_icu.sql
--
-- ICU advisory rules (notify-only). Evaluators: eval_icu_* in
-- workflows/graph/advisory_evaluators.py. Thresholds live in each row's params
-- (operator-editable in the DB or via PATCH /api/advisory-rules/{id}).
--
-- Triggers: event-driven (seconds-fast when Kafka is on) PLUS a clock fallback
-- interval. icu_predicted_full is clock-only (ML forecast, time-rolling).
-- See docs/agentic-framework/ADVISORY_ENGINE.md.
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/061_advisory_rules_icu.sql
-- (no --track-only needed: only new TABLES need tracking, not rows)
-- Keep in sync with db/init/tenant_template.sql. Idempotent (ON CONFLICT DO NOTHING).

INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('icu_occupancy_high', 'ICU', 'High ICU occupancy',
   'ICU occupancy > 90%',
   'Expedite step-down transfers and review pending ICU admissions',
   'warning', '{"occupancy_pct_threshold": 90}',
   '["bed", "admission"]', 300, 3600),

  ('icu_predicted_full', 'ICU', 'ICU predicted full',
   'ML forecast projects ICU census to reach capacity within 24h',
   'Pre-plan overnight ICU beds and anaesthetist/intensivist cover',
   'warning', '{}',
   '[]', 900, 3600),

  ('icu_ventilator_utilization_high', 'ICU', 'Ventilator utilization high',
   'In-use ventilators > 85% of operational units',
   'Audit weaning candidates and check maintenance-held ventilators',
   'warning', '{"utilization_pct_threshold": 85}',
   '["ventilator"]', 300, 3600),

  ('icu_step_down_eligible', 'ICU', 'Step-down eligible patients',
   'ICU patients flagged ready for step-down / discharge',
   'Coordinate step-down transfers to free ICU capacity',
   'info', '{"min_eligible": 1}',
   '["admission", "discharge_ready"]', 300, 3600),

  ('icu_nurse_ratio_below_policy', 'ICU', 'ICU nurse ratio below policy',
   'ICU nurse-to-patient load exceeds policy',
   'Reallocate nursing staff or call in additional ICU nurses',
   'warning', '{"roster_area": "icu", "max_patients_per_nurse": 2}',
   '["staff_roster", "admission"]', 600, 3600),

  ('icu_admission_pending', 'ICU', 'ICU admission pending',
   'ICU has no free beds -- incoming admissions will queue',
   'Trigger overflow protocol and expedite discharges out of ICU',
   'critical', '{"max_free_beds": 0}',
   '["bed", "admission"]', 300, 1800)
ON CONFLICT (rule_key) DO NOTHING;
