-- ─────────────────────────────────────────────────────────────────────────────
-- Lab Agent — registry rows (source of truth for the DB-driven planner)
-- Inserts into hospilot_app.agent_registry / subagent_registry / task_registry
-- so planner._fetch_registry() picks it up.
-- Idempotent via ON CONFLICT (id) DO NOTHING.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO hospilot_app.agent_registry (id, label, description, emoji, color, is_active, sort_order) VALUES
  ('lab_agent', 'Lab Operations',
   'Manages lab operations: sample tracking, TAT optimization, critical result escalation, analyzer utilization, QC compliance, test recommendations, and capacity forecasting.',
   '🧪', '#06b6d4', true, 130)
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot_app.subagent_registry (id, agent_id, label, description, is_active, is_prefetch_eligible, sort_order) VALUES
  ('sa_sample_prioritization',    'lab_agent', 'Sample Prioritization Agent',        'Prioritizes STAT/ICU/ER samples and escalates TAT-risk cases',                     true, true,  10),
  ('sa_sample_tracking',          'lab_agent', 'Sample Tracking Agent',              'Tracks collection, transport, and lab receipt status; triggers search for missing samples', true, true, 20),
  ('sa_tat_optimization',         'lab_agent', 'TAT Optimization Agent',             'Monitors turnaround time, identifies bottlenecks, escalates SLA breaches',         true, false, 30),
  ('sa_analyzer_utilization',     'lab_agent', 'Analyzer Utilization Agent',         'Monitors analyzer load, triggers rebalancing and maintenance alerts',              true, false, 40),
  ('sa_analyzer_routing',         'lab_agent', 'Analyzer Routing Agent',             'Routes samples to alternate analyzers when primary is overloaded',                 true, false, 50),
  ('sa_quality_control',          'lab_agent', 'Quality Control Agent',              'Validates QC pass/fail per shift, triggers recalibration, raises compliance alerts', true, false, 60),
  ('sa_test_validation',          'lab_agent', 'Test Validation Agent',              'Auto-validates results against rules, delta checks, critical value flags',          true, false, 70),
  ('sa_critical_result_escalation','lab_agent','Critical Result Escalation Agent',   'Detects critical lab values and escalates to physician / ICU-ER team',              true, false, 80),
  ('sa_test_recommendation',      'lab_agent', 'Test Recommendation Agent',          'Detects abnormal results, applies reflex rules, recommends add-on tests',           true, false, 90),
  ('sa_capacity_prediction',      'lab_agent', 'Lab Capacity Prediction Agent',      'Forecasts workload demand and notifies command center of surge risk',               true, false, 100)
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot_app.task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  -- sa_sample_prioritization
  ('ta_check_stat_status',        'sa_sample_prioritization', 'Check if any STAT samples are pending',                               '["stat_count","stat_samples"]',           true, false, 10),
  ('ta_apply_icu_er_priority',    'sa_sample_prioritization', 'Apply highest priority for ICU/ER samples',                           '["prioritized_count"]',                   true, false, 20),
  ('ta_check_analyzer_available', 'sa_sample_prioritization', 'Check if target analyzer is available',                               '["available_count"]',                     true, false, 30),
  ('ta_escalate_tat_risk',        'sa_sample_prioritization', 'Escalate to supervisor if TAT target is at risk',                     '["escalated"]',                           true, false, 40),

  -- sa_sample_tracking
  ('ta_check_sample_collection',  'sa_sample_tracking', 'Check which samples are collected vs pending',                              '["collected_count","pending_count"]',      true, false, 10),
  ('ta_check_sample_transport',   'sa_sample_tracking', 'Check transport status for collected samples',                              '["in_transit","delayed_count"]',           true, false, 20),
  ('ta_verify_sample_receipt',    'sa_sample_tracking', 'Verify sample received at lab',                                             '["received_count","missing_count"]',       true, false, 30),
  ('ta_trigger_sample_search',    'sa_sample_tracking', 'Trigger search for misplaced samples',                                      '["search_triggered"]',                    true, false, 40),

  -- sa_tat_optimization
  ('ta_check_tat_threshold',      'sa_tat_optimization', 'Check if current TAT exceeds SLA threshold',                               '["overdue_count","tat_exceeded"]',         true, false, 10),
  ('ta_analyze_tat_bottleneck',   'sa_tat_optimization', 'Identify the processing bottleneck stage',                                 '["bottleneck_stage","bottleneck_count"]',  true, false, 20),
  ('ta_prioritize_stat_queue',    'sa_tat_optimization', 'Reprioritize queue for pending STAT samples',                              '["reprioritized_count"]',                  true, false, 30),
  ('ta_escalate_tat_supervisor',  'sa_tat_optimization', 'Escalate to Lab Supervisor when TAT is not restored',                      '["escalated"]',                           true, false, 40),

  -- sa_analyzer_utilization
  ('ta_check_analyzer_utilization',  'sa_analyzer_utilization', 'Check if any analyzer load exceeds 90%',                           '["overloaded_count","max_utilization"]',   true, false, 10),
  ('ta_identify_alternate_analyzer', 'sa_analyzer_utilization', 'Identify available backup analyzer',                               '["alternate_available","alternate_id"]',   true, false, 20),
  ('ta_rebalance_analyzer_workload', 'sa_analyzer_utilization', 'Rebalance workload to backup analyzer',                            '["rebalanced"]',                           true, false, 30),
  ('ta_trigger_maintenance_alert',   'sa_analyzer_utilization', 'Alert maintenance team for predicted downtime',                    '["alerted"]',                              true, false, 40),

  -- sa_analyzer_routing
  ('ta_check_analyzer_overload',     'sa_analyzer_routing', 'Check if primary analyzer is overloaded',                              '["overloaded","load_pct"]',                true, false, 10),
  ('ta_validate_alternate_analyzer', 'sa_analyzer_routing', 'Validate backup analyzer is certified for the test',                   '["validated","alternate_id"]',             true, false, 20),
  ('ta_execute_sample_routing',      'sa_analyzer_routing', 'Route samples to alternate analyzer',                                  '["routed_count"]',                         true, false, 30),
  ('ta_restore_routing_capacity',    'sa_analyzer_routing', 'Close routing workflow when capacity is normalized',                   '["restored"]',                             true, false, 40),

  -- sa_quality_control
  ('ta_check_qc_status',      'sa_quality_control', 'Check QC pass/fail for active analyzers this shift',                          '["failed_count","qc_failed"]',             true, false, 10),
  ('ta_trigger_recalibration','sa_quality_control', 'Stop result release and trigger recalibration',                                '["recalibration_triggered"]',              true, false, 20),
  ('ta_repeat_qc_check',      'sa_quality_control', 'Rerun QC after calibration',                                                  '["passed","repeat_passed"]',               true, false, 30),
  ('ta_compliance_alert',     'sa_quality_control', 'Generate compliance alert if accreditation is impacted',                      '["alerted"]',                              true, false, 40),

  -- sa_test_validation
  ('ta_validate_result_rules',    'sa_test_validation', 'Check result against auto-validation rules',                               '["auto_released","flagged_count"]',        true, false, 10),
  ('ta_check_delta_flag',         'sa_test_validation', 'Run delta check against prior result',                                     '["delta_failed_count"]',                   true, false, 20),
  ('ta_check_critical_value_flag','sa_test_validation', 'Check for critical value requiring escalation',                            '["critical_count"]',                       true, false, 30),
  ('ta_release_validated_report', 'sa_test_validation', 'Release auto-validated reports',                                           '["released_count"]',                       true, false, 40),

  -- sa_critical_result_escalation
  ('ta_detect_critical_results',   'sa_critical_result_escalation', 'Detect critical lab values requiring immediate action',        '["critical_count","critical_results"]',    true, false, 10),
  ('ta_notify_physician_critical', 'sa_critical_result_escalation', 'Alert physician for critical result',                         '["notified_count"]',                       true, false, 20),
  ('ta_escalate_icu_er_critical',  'sa_critical_result_escalation', 'Trigger urgent escalation for ICU/ER patients',               '["escalated_count"]',                      true, false, 30),
  ('ta_log_critical_action',       'sa_critical_result_escalation', 'Log physician acknowledgment and close workflow',             '["logged"]',                               true, false, 40),

  -- sa_test_recommendation
  ('ta_detect_abnormal_result',    'sa_test_recommendation', 'Detect abnormal results triggering reflex rules',                    '["abnormal_count","abnormal_results"]',     true, false, 10),
  ('ta_evaluate_reflex_rules',     'sa_test_recommendation', 'Apply reflex/add-on rules to abnormal results',                     '["recommended_count"]',                    true, false, 20),
  ('ta_recommend_additional_test', 'sa_test_recommendation', 'Send recommendation to physician',                                  '["sent_count"]',                           true, false, 30),
  ('ta_create_reflex_order',       'sa_test_recommendation', 'Auto-create order per protocol if no approval needed',              '["orders_created"]',                       true, false, 40),

  -- sa_capacity_prediction
  ('ta_get_historical_demand',    'sa_capacity_prediction', 'Fetch historical workload data',                                       '["has_history","avg_daily_orders"]',        true, false, 10),
  ('ta_run_workload_forecast',    'sa_capacity_prediction', 'AI-powered demand forecast for next shift/day',                       '["forecast_orders","surge_expected"]',     true, false, 20),
  ('ta_check_capacity_threshold', 'sa_capacity_prediction', 'Compare forecast to analyzer capacity',                              '["capacity_gap","at_risk"]',               true, false, 30),
  ('ta_surge_notify_command',     'sa_capacity_prediction', 'Notify Command Center if surge is expected',                         '["notified"]',                             true, false, 40)
ON CONFLICT (id) DO NOTHING;
