-- ─────────────────────────────────────────────────────────────────────────────
-- Pharmacy Agent — registry rows (source of truth for the DB-driven planner)
-- Idempotent via ON CONFLICT (id) DO NOTHING.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO hospilot_app.agent_registry (id, label, description, emoji, color, is_active, sort_order) VALUES
  ('pharmacy_agent', 'Pharmacy',
   'Manages medication lifecycle: order fulfillment, drug availability, prescription validation, interaction checking, dispensing, substitution, queue optimization, and controlled drug compliance.',
   '💊', '#0ea5e9', true, 140)
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot_app.subagent_registry (id, agent_id, label, description, is_active, is_prefetch_eligible, sort_order) VALUES
  ('sa_medication_prioritization', 'pharmacy_agent', 'Medication Prioritization Agent', 'Prioritizes STAT and critical patient medication orders; escalates unavailability', true, true,  10),
  ('sa_medication_fulfillment',    'pharmacy_agent', 'Medication Fulfillment Agent',    'Tracks prescription receipt, availability check, and dispensing lifecycle',        true, true,  20),
  ('sa_drug_availability',         'pharmacy_agent', 'Drug Availability Agent',         'Monitors stock levels, searches alternate locations, reserves inventory',           true, false, 30),
  ('sa_prescription_validation',   'pharmacy_agent', 'Prescription Validation Agent',   'Validates completeness, dosage safety, and duplicate detection before dispensing', true, false, 40),
  ('sa_clinical_interaction',      'pharmacy_agent', 'Clinical Interaction Agent',      'Runs drug-drug interaction and allergy conflict checks',                           true, false, 50),
  ('sa_dispensing_validation',     'pharmacy_agent', 'Dispensing Validation Agent',     'Right patient / right drug / right dose validation before medication release',     true, false, 60),
  ('sa_medication_substitution',   'pharmacy_agent', 'Medication Substitution Agent',   'Recommends formulary alternatives and manages physician approval workflow',        true, false, 70),
  ('sa_pharmacy_queue',            'pharmacy_agent', 'Pharmacy Queue Optimization Agent','Monitors dispensing queue, identifies bottlenecks, prioritizes STAT workload',   true, false, 80),
  ('sa_controlled_drug_compliance','pharmacy_agent', 'Controlled Drug Compliance Agent','Audits controlled substance dispensing for authorization and inventory accuracy',  true, false, 90),
  ('sa_pharmacy_capacity',         'pharmacy_agent', 'Pharmacy Capacity Prediction Agent','Forecasts dispensing demand and alerts command center of surge risk',           true, false, 100)
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot_app.task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  -- sa_medication_prioritization
  ('ta_check_stat_medication_orders',    'sa_medication_prioritization', 'Check for STAT medication orders',                              '["stat_count","stat_orders"]',                 true, false, 10),
  ('ta_apply_critical_patient_priority', 'sa_medication_prioritization', 'Apply highest priority for critical and ICU/ER patients',       '["critical_patient_count","prioritized_count"]',true, false, 20),
  ('ta_check_stat_availability',         'sa_medication_prioritization', 'Check if STAT medications are in stock',                        '["stat_available_count","stat_unavailable_count"]', true, false, 30),
  ('ta_escalate_stat_shortage',          'sa_medication_prioritization', 'Escalate when STAT medication is unavailable',                  '["escalated"]',                                true, false, 40),

  -- sa_medication_fulfillment
  ('ta_check_prescription_received',     'sa_medication_fulfillment', 'Check if prescriptions have been received for pending orders',    '["prescription_count","pending_count"]',        true, false, 10),
  ('ta_check_medication_availability',   'sa_medication_fulfillment', 'Verify medication is available in inventory',                     '["available_count","unavailable_count"]',       true, false, 20),
  ('ta_track_dispensing_progress',       'sa_medication_fulfillment', 'Track in-progress dispensing orders',                            '["dispensing_count","delayed_count"]',          true, false, 30),
  ('ta_close_fulfilled_orders',          'sa_medication_fulfillment', 'Mark completed medication orders as closed',                     '["closed_count"]',                             true, false, 40),

  -- sa_drug_availability
  ('ta_check_stock_levels',              'sa_drug_availability', 'Check current medication stock vs reorder levels',                    '["low_stock_count","out_of_stock_count","adequate_count"]', true, false, 10),
  ('ta_search_alternate_location',       'sa_drug_availability', 'Search satellite pharmacy and ICU cart for alternative stock',       '["alternate_found","alternate_location"]',      true, false, 20),
  ('ta_reserve_inventory',               'sa_drug_availability', 'Reserve available inventory for STAT and critical orders',           '["reserved_count"]',                           true, false, 30),
  ('ta_escalate_critical_shortage',      'sa_drug_availability', 'Escalate shortage impacting patient care to pharmacy lead',         '["escalated","shortage_medications"]',          true, false, 40),

  -- sa_prescription_validation
  ('ta_validate_prescription_completeness','sa_prescription_validation','Check prescriptions are complete with all required fields',    '["complete_count","incomplete_count"]',         true, false, 10),
  ('ta_validate_dosage_range',           'sa_prescription_validation', 'Validate dose is within therapeutic safe range',              '["safe_count","unsafe_dose_count"]',            true, false, 20),
  ('ta_detect_duplicate_medications',    'sa_prescription_validation', 'Detect duplicate medication orders for same patient',         '["duplicate_count","duplicates"]',              true, false, 30),
  ('ta_approve_or_hold_prescription',    'sa_prescription_validation', 'Release prescription for dispensing or hold for review',      '["approved_count","held_count"]',               true, false, 40),

  -- sa_clinical_interaction
  ('ta_check_polypharmacy',              'sa_clinical_interaction', 'Check patients prescribed multiple concurrent medications',       '["polypharmacy_count","patient_count"]',         true, false, 10),
  ('ta_run_interaction_check',           'sa_clinical_interaction', 'Run drug-drug interaction check against known rules',            '["major_interaction_count","total_interactions"]',true, false, 20),
  ('ta_check_allergy_conflict',          'sa_clinical_interaction', 'Check for allergy conflicts in pending orders',                  '["allergy_conflict_count"]',                    true, false, 30),
  ('ta_approve_safe_dispense',           'sa_clinical_interaction', 'Approve or hold medication based on interaction findings',       '["approved_count","held_count"]',               true, false, 40),

  -- sa_dispensing_validation
  ('ta_verify_patient_identity',         'sa_dispensing_validation', 'Verify patient identity before medication dispensing',          '["verified_count","unverified_count"]',          true, false, 10),
  ('ta_match_medication_prescription',   'sa_dispensing_validation', 'Match dispensed medication to prescription',                   '["match_failed_count","matched_count"]',         true, false, 20),
  ('ta_validate_dispensing_dosage',      'sa_dispensing_validation', 'Validate correct dosage before medication release',            '["dose_mismatch_count","dose_correct_count"]',   true, false, 30),
  ('ta_release_or_hold_dispensing',      'sa_dispensing_validation', 'Release verified medication or hold discrepancy for review',   '["released_count","held_count"]',               true, false, 40),

  -- sa_medication_substitution
  ('ta_check_unavailable_medications',   'sa_medication_substitution', 'Identify prescribed medications that are unavailable',        '["unavailable_count","unavailable_meds"]',       true, false, 10),
  ('ta_search_formulary_alternatives',   'sa_medication_substitution', 'Search formulary for therapeutic substitutes',               '["substitute_available","substitute_count","substitutes"]', true, false, 20),
  ('ta_request_physician_approval',      'sa_medication_substitution', 'Send substitution approval request to prescribing physician', '["approved_count","pending_approval"]',         true, false, 30),
  ('ta_update_substitution_order',       'sa_medication_substitution', 'Update order with approved substitute medication',           '["substitution_updated","substituted_meds"]',   true, false, 40),

  -- sa_pharmacy_queue
  ('ta_check_queue_length',              'sa_pharmacy_queue', 'Check current dispensing queue length and STAT backlog',              '["queue_length","stat_waiting_count","queue_above_threshold"]', true, false, 10),
  ('ta_analyze_queue_bottleneck',        'sa_pharmacy_queue', 'Identify bottleneck causing queue buildup',                          '["bottleneck_dept","in_progress_count","dept_breakdown"]', true, false, 20),
  ('ta_prioritize_stat_medications',     'sa_pharmacy_queue', 'Reprioritize queue for pending STAT medication orders',              '["stat_prioritized","stat_orders"]',            true, false, 30),
  ('ta_escalate_tat_breach',             'sa_pharmacy_queue', 'Escalate to pharmacy supervisor when TAT SLA is breached',          '["tat_breach_count","escalated","breach_orders"]', true, false, 40),

  -- sa_controlled_drug_compliance
  ('ta_identify_controlled_orders',      'sa_controlled_drug_compliance', 'Identify controlled substance orders requiring audit',    '["controlled_count","controlled_orders"]',       true, false, 10),
  ('ta_verify_controlled_authorization', 'sa_controlled_drug_compliance', 'Verify authorization and witness documentation',         '["authorized_count","missing_auth_count"]',      true, false, 20),
  ('ta_check_inventory_variance',        'sa_controlled_drug_compliance', 'Check for controlled drug inventory discrepancies',      '["variance_detected","variance_count"]',         true, false, 30),
  ('ta_escalate_compliance_issue',       'sa_controlled_drug_compliance', 'Escalate to compliance officer for investigation',       '["escalated"]',                                 true, false, 40),

  -- sa_pharmacy_capacity
  ('ta_fetch_dispensing_history',        'sa_pharmacy_capacity', 'Fetch historical dispensing demand data',                         '["history_days","avg_daily_orders","max_daily_orders"]', true, false, 10),
  ('ta_forecast_pharmacy_demand',        'sa_pharmacy_capacity', 'AI-powered dispensing demand forecast for next shift/day',       '["predicted_orders","surge_expected","trend"]',  true, false, 20),
  ('ta_check_dispensing_capacity',       'sa_pharmacy_capacity', 'Compare forecast to current dispensing capacity',               '["capacity_ok","active_order_count","surge_threshold"]', true, false, 30),
  ('ta_notify_pharmacy_surge',           'sa_pharmacy_capacity', 'Notify Command Center if pharmacy surge is expected',           '["notified"]',                                   true, false, 40)
ON CONFLICT (id) DO NOTHING;
