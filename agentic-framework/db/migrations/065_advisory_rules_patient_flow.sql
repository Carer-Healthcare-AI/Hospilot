-- 065_advisory_rules_patient_flow.sql
--
-- Patient Flow advisory rules (notify-only). Evaluators: eval_patient_* in
-- workflows/graph/advisory_evaluators.py (Fabric /admissions/* reads).
-- Thresholds live in each row's params (operator-editable in the DB or via
-- PATCH /api/advisory-rules/{id}).
--
-- patient_referral_pending and patient_readmission_risk are HEURISTIC proxies:
-- the system has no referral table or readmission-risk model, so they derive
-- signal from discharge-block reasons and recently-discharged/re-admitted
-- patient overlap respectively. Both fully tunable via params.
--
-- Triggers: event-driven on admission/bed/lab changes PLUS a clock fallback.
-- See docs/agentic-framework/ADVISORY_ENGINE.md.
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/065_advisory_rules_patient_flow.sql
-- (no --track-only needed: only new TABLES need tracking, not rows)
-- Keep in sync with db/init/tenant_template.sql. Idempotent (ON CONFLICT DO NOTHING).

INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('patient_admission_waiting', 'Patient Flow', 'Admission waiting >30 min',
   'Admitted patients awaiting bed assignment longer than 30 minutes',
   'Optimize bed assignment',
   'warning', '{"wait_threshold_minutes": 30, "min_waiting": 1}',
   '["admission", "bed"]', 300, 1800),

  ('patient_transfer_pending', 'Patient Flow', 'Transfer pending',
   'One or more patient transfers are pending',
   'Initiate patient transfer workflow',
   'warning', '{"min_transfers": 1}',
   '["admission"]', 300, 1800),

  ('patient_diagnostic_delay_discharge', 'Patient Flow', 'Diagnostic delay affecting discharge',
   'Discharge blocked by pending diagnostics/investigations',
   'Escalate pending investigations',
   'warning', '{"min_delayed": 1}',
   '["admission", "lab_result", "lab_order"]', 600, 1800),

  ('patient_referral_pending', 'Patient Flow', 'Referral pending',
   'Patients awaiting a specialty referral/consult (heuristic)',
   'Notify specialty department',
   'info', '{"min_pending": 1}',
   '["admission"]', 600, 3600),

  ('patient_readmission_risk', 'Patient Flow', 'Readmission risk identified',
   'Patients discharged then readmitted (readmission-risk proxy)',
   'Schedule care coordination follow-up',
   'warning', '{"min_readmissions": 1}',
   '["admission", "discharge_ready"]', 600, 3600)
ON CONFLICT (rule_key) DO NOTHING;
