-- 060_advisory_rules_er.sql
--
-- Emergency (ER) advisory rules (notify-only). Evaluators live in
-- workflows/graph/advisory_evaluators.py (eval_er_*). Thresholds live in each
-- row's params -- operator-editable in the DB or via PATCH /api/advisory-rules/{id}.
--
-- All six are event-triggered (seconds-fast when Kafka is on) PLUS a clock
-- fallback interval (works Kafka-off; re-alerts after cooldown in quiet periods;
-- carries the time-based conditions events can't). See docs/agentic-framework/ADVISORY_ENGINE.md.
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/060_advisory_rules_er.sql
-- (no --track-only needed: only new TABLES need tracking, not rows)
-- Keep in sync with db/init/tenant_template.sql. Idempotent: safe to re-run --
-- ON CONFLICT DO NOTHING never clobbers operator-edited thresholds.

INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('er_wait_time_high', 'Emergency', 'ER wait time exceeded',
   'ER patients waiting longer than 30 minutes',
   'Review the ER triage queue and allocate staff to reduce wait times',
   'warning', '{"wait_threshold_minutes": 30, "min_waiting_patients": 1}',
   '["visit"]', 300, 1800),

  ('er_triage_queue_high', 'Emergency', 'Triage queue increasing',
   'Untriaged ER patients exceed the backlog threshold',
   'Assign a triage nurse and fast-track waiting patients',
   'warning', '{"max_untriaged": 5}',
   '["visit"]', 300, 1800),

  ('er_critical_patient_waiting', 'Emergency', 'Critical patient waiting',
   'One or more critical patients are awaiting care in the ER',
   'Escalate to the on-call physician and prioritize critical patients immediately',
   'critical', '{"min_critical": 1}',
   '["visit"]', 180, 900),

  ('er_ambulance_arrivals_high', 'Emergency', 'Ambulance arrivals increasing',
   'Inbound ambulances exceed the threshold',
   'Prepare resuscitation bays and pre-stage triage staff for incoming patients',
   'info', '{"max_incoming_ambulances": 3}',
   '["ambulance"]', 300, 1800),

  ('er_occupancy_high', 'Emergency', 'ER occupancy critical',
   'ER occupancy above 95% of capacity',
   'Open surge capacity and expedite admissions/discharges out of the ER',
   'critical', '{"occupancy_pct_threshold": 95, "er_capacity": 50}',
   '["visit"]', 300, 1800),

  ('er_boarding_patients_high', 'Emergency', 'Boarding patients increasing',
   'Patients awaiting inpatient admission (boarding) exceed the threshold',
   'Coordinate with bed management to expedite inpatient bed assignment',
   'warning', '{"max_boarding": 20}',
   '["visit", "admission"]', 300, 3600)
ON CONFLICT (rule_key) DO NOTHING;
