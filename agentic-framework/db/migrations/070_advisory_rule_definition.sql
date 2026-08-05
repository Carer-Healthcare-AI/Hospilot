-- 070_advisory_rule_definition.sql
--
-- Move each advisory rule's LOGIC into a DB JSON column so rules are data, not
-- code. Adds advisory_rules.definition (jsonb). The engine
-- (workflows/graph/advisory.py -> advisory_conditions.run_condition) reads
-- definition.condition, which is either:
--   * declarative  {source, aggregate, operator, threshold, filter, ...}  -> the
--                  generic interpreter (workflows/graph/advisory_conditions.py)
--   * handler      {"handler": "<rule_key>"} -> a named function in
--                  advisory_evaluators.EVALUATORS (stateful/join/forecast/financial
--                  rules that cannot be expressed as data) -- identical behaviour.
-- Rules with an empty definition fall back to EVALUATORS[rule_key] in the engine,
-- so this migration is safe to apply before/after a code deploy.
--
-- Apply: python scripts/migrate_all_tenants.py db/migrations/070_advisory_rule_definition.sql
-- Idempotent: the handler-default only fills rules with no condition yet; the
-- declarative overrides only replace rules still on a handler-ref (operator edits
-- via PATCH /advisory-rules are preserved). Keep in sync with tenant_template.sql.

ALTER TABLE hospilot_app.advisory_rules
  ADD COLUMN IF NOT EXISTS definition jsonb NOT NULL DEFAULT '{}'::jsonb;

-- 1. Default EVERY rule to a handler-ref to its existing evaluator (byte-identical
--    behaviour; makes all rules DB-driven). Only fills rules without a condition.
UPDATE hospilot_app.advisory_rules
SET definition = jsonb_build_object('condition', jsonb_build_object('handler', rule_key))
WHERE NOT (definition ? 'condition');

-- 2. Promote the threshold-style rules to declarative conditions. Each replaces
--    the handler-ref set in step 1 (guarded so operator-edited rules are kept).
--    detail_template wording matches the pre-declarative evaluators exactly.

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"beds_summary","kind":"dict","field":"occupancy_pct","operator":">","threshold":90,"detail_template":"Bed occupancy is {occupancy_pct:.0f}% ({occupied_beds}/{total_beds} beds), threshold {threshold:.0f}%"}}'
  WHERE rule_key='bed_occupancy_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"dirty_beds","aggregate":"count","operator":">","threshold":10,"detail_template":"{value} beds awaiting cleaning (threshold {threshold:.0f})"}}'
  WHERE rule_key='dirty_beds_backlog' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"icu_admissions","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"discharge_ready","op":"truthy"}],"detail_template":"{value} ICU patient(s) ready for step-down to ward"}}'
  WHERE rule_key='icu_stepdown_pending' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"er_long_wait","args":{"minutes":30},"aggregate":"count","operator":">=","threshold":1,"labels":{"minutes":30},"detail_template":"{value} ER patient(s) waiting over {minutes:g} min (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='er_wait_time_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"untriaged","aggregate":"count","operator":">","threshold":5,"detail_template":"{value} patient(s) awaiting triage (threshold {threshold:.0f})"}}'
  WHERE rule_key='er_triage_queue_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"critical_vitals","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"is_critical","op":"truthy"}],"detail_template":"{value} critical patient(s) awaiting care (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='er_critical_patient_waiting' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"ambulances","aggregate":"count","operator":">","threshold":3,"filter":[{"field":"status","op":"in","value":["En Route","En-Route","Enroute","Dispatched","Incoming"]}],"detail_template":"{value} ambulance(s) inbound (threshold {threshold:.0f})"}}'
  WHERE rule_key='er_ambulance_arrivals_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"active_er","aggregate":"pct_of","denominator":50,"operator":">","threshold":95,"require_source_nonempty":true,"labels":{"capacity":50},"detail_template":"ER occupancy is {value:.0f}% ({count}/{capacity} capacity), threshold {threshold:.0f}%"}}'
  WHERE rule_key='er_occupancy_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"er_pressure","kind":"dict","field":"est_admissions","operator":">","threshold":20,"detail_template":"{value:.0f} patient(s) awaiting admission/boarding (threshold {threshold:.0f})"}}'
  WHERE rule_key='er_boarding_patients_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"beds_summary","kind":"dict","field":"icu_pct","operator":">","threshold":90,"require_positive":["icu_total"],"detail_template":"ICU occupancy is {icu_pct:.0f}% ({icu_occupied}/{icu_total} beds), threshold {threshold:.0f}%"}}'
  WHERE rule_key='icu_occupancy_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"icu_admissions","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"discharge_ready","op":"truthy"}],"detail_template":"{value} ICU patient(s) eligible for step-down (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='icu_step_down_eligible' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"beds_summary","kind":"dict","field":"icu_available","operator":"<=","threshold":0,"require_positive":["icu_total"],"detail_template":"ICU has {icu_available} free bed(s) of {icu_total} -- incoming admissions will queue"}}'
  WHERE rule_key='icu_admission_pending' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"ventilators","aggregate":"ratio","operator":">","threshold":85,"numerator_filter":[{"field":"status","op":"contains_any","value":["use"]}],"denominator_filter":[{"field":"status","op":"contains_any","value":["use","avail"]}],"detail_template":"Ventilator utilization {value:.0f}% ({numerator}/{denominator} in use), threshold {threshold:.0f}%"}}'
  WHERE rule_key='icu_ventilator_utilization_high' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"ambulances","aggregate":"count","operator":"<","threshold":1,"require_source_nonempty":true,"filter":[{"field":"status","op":"==","value":"available"}],"detail_template":"{value} ambulance(s) available (minimum {threshold:.0f}) of {total} fleet"}}'
  WHERE rule_key='ambulance_none_available' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"ambulances","aggregate":"count","operator":">=","threshold":1,"labels":{"sla":15},"filter":[{"field":"eta_mins","op":"not_null"},{"field":"eta_mins","op":">","value":15}],"detail_template":"{value} ambulance(s) with ETA over {sla:g} min SLA (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='ambulance_eta_exceeds_sla' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"ambulances","aggregate":"count","operator":">","threshold":3,"filter":[{"field":"emergency_type","op":"not_null"}],"detail_template":"{value} active emergency call(s) in progress (threshold {threshold:.0f})"}}'
  WHERE rule_key='ambulance_multiple_emergency_calls' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"ambulances","aggregate":"count","operator":">=","threshold":1,"filter":[{"any":[{"field":"status","op":"in","value":["maintenance","out of service","overdue"]},{"field":"next_maintenance","op":"age_gt_minutes","value":0},{"field":"maintenance_due","op":"age_gt_minutes","value":0},{"field":"next_service","op":"age_gt_minutes","value":0}]}],"detail_template":"{value} ambulance(s) overdue for maintenance (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='ambulance_maintenance_overdue' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"pharmacy_inventory","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"stock_quantity","op":"<=","value":0}],"detail_template":"{value} drug(s) out of stock (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='pharmacy_drug_out_of_stock' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"pharmacy_orders","aggregate":"count","operator":">","threshold":5,"filter":[{"field":"dispensed_at","op":"is_null"},{"field":"status","op":"in","value":["pending","on_hold","dispensing","ordered"]}],"detail_template":"{value} order(s) in the dispensing queue (threshold {threshold:.0f})"}}'
  WHERE rule_key='pharmacy_queue_increasing' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"pharmacy_orders","aggregate":"count","operator":">=","threshold":1,"labels":{"delay":120},"filter":[{"field":"dispensed_at","op":"is_null"},{"field":"prescribed_at","op":"age_gt_minutes","value":120}],"detail_template":"{value} medication order(s) delayed over {delay:g} min (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='pharmacy_delivery_delayed' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"pharmacy_inventory","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"stock_quantity","op":"le_field","value":"reorder_level"}],"detail_template":"{value} item(s) at/below reorder level (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='pharmacy_inventory_below_reorder' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"pharmacy_controlled_logs","args":{"hours":24},"aggregate":"count","operator":">=","threshold":1,"labels":{"hours":24},"filter":[{"any":[{"field":"variance_detected","op":"truthy"},{"field":"documentation_complete","op":"falsy"}]}],"detail_template":"{value} controlled-drug discrepancy/documentation issue(s) in {hours}h (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='pharmacy_controlled_discrepancy' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"admissions_with_wards","aggregate":"count","operator":">=","threshold":1,"labels":{"minutes":30},"filter":[{"field":"bed_id","op":"is_null"},{"field":"admitted_at","op":"age_gt_minutes","value":30}],"detail_template":"{value} admission(s) awaiting bed assignment over {minutes:g} min (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='patient_admission_waiting' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"admissions_with_wards","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"transfer_pending","op":"truthy"}],"detail_template":"{value} patient transfer(s) pending (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='patient_transfer_pending' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"admissions_with_wards","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"discharge_ready","op":"falsy"},{"field":"discharge_blocked_reason","op":"contains_any","value":["pending_tasks","lab","diagnostic","investigation","imaging","result","test"]}],"detail_template":"{value} discharge(s) delayed by pending investigations (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='patient_diagnostic_delay_discharge' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"admissions_with_wards","aggregate":"count","operator":">=","threshold":1,"filter":[{"field":"discharge_blocked_reason","op":"contains_any","value":["referral","consult","specialist","specialty","needs_review","review"]}],"detail_template":"{value} patient(s) with a pending referral/specialty review (alert at {threshold:.0f}+)"}}'
  WHERE rule_key='patient_referral_pending' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"staff","aggregate":"count","operator":"<","threshold":40,"require_source_nonempty":true,"filter":[{"field":"on_duty_status","op":"in","value":["on_duty","on-duty","present","available","working","active","duty"]}],"detail_template":"{value} staff on duty (minimum {threshold:.0f}) of {total} total"}}'
  WHERE rule_key='staffing_shortage_detected' AND definition->'condition' ? 'handler';

UPDATE hospilot_app.advisory_rules SET definition = '{"condition":{"source":"staff","aggregate":"pct","operator":">","threshold":15,"require_source_nonempty":true,"filter":[{"field":"on_duty_status","op":"not_in","value":["on_duty","on-duty","present","available","working","active","duty"]}],"detail_template":"Absenteeism {value:.0f}% ({count}/{total} staff off duty), threshold {threshold:.0f}%"}}'
  WHERE rule_key='staffing_high_absenteeism' AND definition->'condition' ? 'handler';
