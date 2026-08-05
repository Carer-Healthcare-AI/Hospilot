-- 068_advisory_rules_discharge.sql
--
-- Discharge advisory rules. Evaluators: workflows/graph/advisory_evaluators.py
-- (Discharge section). Data: Redis projections -- admission:* (discharge_ready),
-- invoice:*/claim:* (financial REST syncs, NO Kafka topic -> clock-fresh only),
-- pharmacy_order:*, discharge_summary:*.
--
-- Notes:
--   * fit-pending grace uses an in-memory ready-since clock (no timestamp in the
--     data; resets on API restart)
--   * discharge_fit_pending overlaps Bed Management's discharged_bed_blocked by
--     design (discharge-team vs bed-team view); tune grace or disable one if the
--     double alert is unwanted
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/068_advisory_rules_discharge.sql
-- (no --track-only needed: only new TABLES need tracking, not rows)
-- Keep in sync with db/init/tenant_template.sql. Idempotent: safe to re-run --
-- ON CONFLICT DO NOTHING never clobbers operator-edited thresholds.

INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('discharge_fit_pending', 'Discharge', 'Medically fit, discharge pending',
   'Patient medically fit but pending discharge',
   'Launch discharge completion workflow',
   'warning', '{"min_pending": 1, "grace_minutes": 60}',
   '["admission", "discharge_ready"]', 600, 3600),

  ('discharge_billing_pending', 'Discharge', 'Billing blocking discharge',
   'Billing pending',
   'Trigger Billing Agent for immediate processing',
   'warning', '{"min_pending": 1}',
   '["admission", "discharge_ready"]', 600, 3600),

  ('discharge_pharmacy_pending', 'Discharge', 'Discharge medication not ready',
   'Pharmacy pending',
   'Prioritize discharge medication preparation',
   'warning', '{"min_pending": 1, "pending_statuses": ["pending", "on_hold"]}',
   '["admission", "discharge_ready", "pharmacy_order"]', 600, 3600),

  ('discharge_summary_pending', 'Discharge', 'Discharge summary missing',
   'Discharge summary pending',
   'Notify treating physician automatically',
   'warning', '{"min_pending": 1}',
   '["admission", "discharge_ready", "discharge_summary"]', 600, 3600),

  ('discharge_insurance_pending', 'Discharge', 'Insurance approval stuck',
   'Insurance approval pending',
   'Escalate to insurance desk',
   'info', '{"min_pending": 1, "pending_hours": 4}',
   '["admission", "discharge_ready"]', 900, 7200),

  ('discharge_delayed', 'Discharge', 'Discharge delayed',
   'Delayed discharge >2 hours',
   'Notify operations manager and department head',
   'warning', '{"min_pending": 1, "delay_hours": 2}',
   '["admission", "discharge_ready"]', 600, 3600)
ON CONFLICT (rule_key) DO NOTHING;
