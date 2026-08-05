-- ── Agent Registry Seed ───────────────────────────────────────────────────────
-- Extracted from services/planner.py (IDs + task catalog) and
-- src/data/agents.ts (emoji, color, description, capabilities).
-- Run AFTER 003_agent_registry.sql (both with Database = default).

-- ── Agents ────────────────────────────────────────────────────────────────────

INSERT INTO hospilot.agent_registry (id, label, description, emoji, color, sort_order) VALUES
  ('bed_agent',            'Bed Management',    'Finds available beds, recommends the best match for each patient, and manages bed reservations',            '🛏️',  '#3b82f6', 10),
  ('icu_agent',            'ICU Operations',    'Monitors ICU capacity, tracks ventilated patients, and identifies patients ready for step-down',            '🫀',  '#dc2626', 20),
  ('er_agent',             'ER Coordination',   'Monitors emergency patients, assigns urgency scores, and routes patients to the right care setting',        '🚑',  '#ef4444', 30),
  ('staff_agent',          'Staffing',          'Monitors staffing levels across all wards and deploys additional nurses where needed',                      '👥',  '#f59e0b', 40),
  ('discharge_agent',      'Discharge Planning','Identifies patients ready for discharge, resolves barriers, and generates discharge documentation',         '📤',  '#10b981', 50),
  ('pharmacy_agent',       'Pharmacy',          'Drug inventory monitoring, low-stock alerting, and medication reconciliation at discharge',                 '💊',  '#06b6d4', 60),
  ('ot_agent',             'OT Scheduling',     'Reviews today''s surgical schedule against available post-op beds and flags any conflicts',                 '⚕️',  '#7c3aed', 70),
  -- notification_agent removed (oss-prep): low-value, rarely selected; alerts are cross-agent. Uncomment to restore.
  -- ('notification_agent',   'Notifications',     'Sends clinical alerts to the right staff at the right time',                                               '🔔',  '#6b7280', 80),
  ('bed_prediction_agent', 'Bed Prediction',    'Analyses current bed usage and predicts capacity pressures over the next 4–24 hours',                      '📊',  '#0284c7', 90),
  ('revenue_agent',        'Revenue',           'Monitors outstanding invoices, daily collections, and insurance claims to flag financial risks',           '💰',  '#f97316', 100),
  ('billing_agent',        'Billing & Insurance','Claim validation, denial risk, insurance eligibility, compliance checks, and payment recovery',           '📋',  '#84cc16', 110)
ON CONFLICT (id) DO UPDATE SET
  label = EXCLUDED.label, description = EXCLUDED.description,
  emoji = EXCLUDED.emoji, color = EXCLUDED.color, updated_at = now();


-- ── Sub-agents ────────────────────────────────────────────────────────────────

INSERT INTO hospilot.subagent_registry (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order) VALUES

  -- bed_agent
  ('sa_bed_availability',       'bed_agent', 'Bed Availability',     'Identifies all available beds that are clean, unblocked, and ready for a patient',                                       '["Vacancy Filter","Bed Type","Maintenance Exclusion"]',      true,  10),
  ('sa_dirty_bed_recovery',     'bed_agent', 'Dirty Bed Recovery',   'Dispatches emergency housekeeping for dirty beds detected by availability check',                                       '["Vacated Bed Detection","Housekeeping Dispatch","Room Turnover"]', false, 20),
  ('sa_bed_ranking',            'bed_agent', 'Bed Assignment',       'Uses clinical AI to recommend the best available bed based on the patient''s needs',                                    '["AI Ranking","Patient Acuity","Clinical Reasoning"]',       false, 30),
  ('sa_bed_reservation',        'bed_agent', 'Bed Reservation',      'Reserves the selected bed and notifies the receiving ward after clinical approval',                                     '["Approval Gate","Ward Notification","Double-booking Prevention"]', false, 40),
  ('sa_bed_prediction',         'bed_agent', 'Bed Prediction',       'Analyses current bed usage and predicts capacity pressures over the next 4–24 hours',                                  '["Bed Census","Discharge Horizon","ER Pressure","Overflow Risk"]', false, 50),
  ('sa_discharge_coordination', 'bed_agent', 'Discharge Coordination','Signals the discharge workflow to accelerate a patient discharge',                                                    '[]',                                                         false, 60),
  ('sa_escalation',             'bed_agent', 'Escalation',           'Escalates when no compliant bed can be found after a full search',                                                     '[]',                                                         false, 70),

  -- icu_agent
  ('sa_icu_census',    'icu_agent', 'ICU Census',           'Reviews current ICU occupancy and flags stable patients who may be ready to move to a lower-acuity ward', '["ICU Occupancy","Ventilator Count","Step-down Candidates"]', true,  10),
  ('sa_icu_transfer',  'icu_agent', 'ICU Transfer',         'Ranks incoming ICU admission requests and reserves beds for the most critical patients',                  '["Admission Ranking","Ventilator Priority","Overflow Trigger"]', false, 20),
  ('sa_icu_stepdown',  'icu_agent', 'Step-Down Coordinator','Confirms clinical criteria for step-down and arranges transfer to a lower-acuity bed',                   '["Step-down Criteria","Progressive Care Bed"]',              false, 30),

  -- er_agent
  ('sa_er_triage',   'er_agent', 'Triage Monitor',  'Reviews all active ER patients and identifies those who need reassessment',                         '["CTAS Score","Re-triage Flag","Wait Time"]',      true,  10),
  ('sa_er_boarding', 'er_agent', 'Boarding Monitor','Checks ER boarders and escalates SLA breaches',                                                     '["Boarding SLA","Escalation"]',                   true,  20),

  -- staff_agent
  ('sa_ratio_monitor', 'staff_agent', 'Ratio Monitor',        'Reviews nurse-to-patient ratios across all wards and flags understaffed areas', '["Nurse-Patient Ratio","Safe Staffing","Ward Census"]', true,  10),
  ('sa_float_pool',    'staff_agent', 'Float Pool Dispatcher','Identifies available float nurses and recommends where to deploy them',         '["Float Nurses","Skill Matching","Reallocation"]',      false, 20),

  -- discharge_agent
  ('sa_discharge_ready',    'discharge_agent', 'Readiness Assessor',  'Reviews each admitted patient to determine if they are clinically ready for discharge', '["Discharge Checklist","Clinical Readiness","Approval Gate"]', true,  10),
  ('sa_discharge_barriers', 'discharge_agent', 'Barriers Identifier', 'Identifies what is holding up each discharge and escalates where needed',             '["Blocker Detection","Case Manager Escalation"]',             true,  20),

  -- pharmacy_agent
  ('sa_stock_monitor', 'pharmacy_agent', 'Stock Monitor', 'Reviews current medication stock levels and flags drugs running low', '["Drug Inventory","Low Stock Flag","Reorder Alert"]', true, 10),

  -- ot_agent
  ('sa_ot_census',    'ot_agent', 'OT Census',            'Reviews today''s surgical list and checks how many post-operative beds are available', '["Surgical Case List","Post-op Beds"]',                         true,  10),
  ('sa_ot_analysis',  'ot_agent', 'OT Capacity Analyser', 'Assesses whether there is sufficient post-op capacity for each planned surgery',      '["Capacity Risk","Conflict Detection","AI Assessment"]',        false, 20),

  -- notification_agent removed (oss-prep). Uncomment to restore.
  -- ('sa_notifier', 'notification_agent', 'Notifier', 'Delivers alerts to relevant clinical staff for critical vitals, overdue tasks, and long ER waits', '["Critical Vitals","Overdue Tasks","ER Wait Alerts"]', false, 10),

  -- bed_prediction_agent
  ('sa_bed_pred_census',   'bed_prediction_agent', 'Capacity Census',   'Counts beds, ICU occupancy, discharge horizon, and ER pressure',                   '["Bed Census","ICU Occupancy","ER Pressure"]',          true,  10),
  ('sa_bed_pred_forecast', 'bed_prediction_agent', 'Capacity Forecast', 'Generates plain-language capacity forecast and risk classification with Claude',   '["Risk Level","Overflow Risk","Surge Model"]',          false, 20),

  -- revenue_agent
  ('sa_rev_optimization',    'revenue_agent', 'Revenue Optimization', 'Identifies revenue leakage and optimizes utilization across hospital resources',           '["Revenue Leakage","Package Utilization","Resource Efficiency"]', true,  10),
  ('sa_rev_denial_prevention','revenue_agent', 'Denial Prevention',   'Predicts insurance claim denial risk and validates claims before payer submission',        '["Denial Risk","Pre-submission Check","Payer Rules"]',            false, 20),
  ('sa_rev_patient_billing', 'revenue_agent', 'Patient Billing Lookup','Fetches all invoices and claims for a specific patient',                                  '["Invoice Lookup","Claim Status"]',                              false, 30),

  -- billing_agent
  ('sa_claim_validation',    'billing_agent', 'Claim Validation',    'Detects duplicate claims, missing invoice linkages, and amount mismatches',               '["Discrepancy Check","TPA Eligibility","Denial Risk"]', true,  10),
  ('sa_denial_prevention',   'billing_agent', 'Denial Prevention',   'Applies pre-submission review and stricter validation for high-denial-risk claims',        '["Pre-submission Review","TPA Validation","Escalation"]', false, 20),
  ('sa_billing_optimization','billing_agent', 'Billing Optimization','Tracks overdue invoices, detects revenue leakage, and generates billing recommendations',  '["Overdue Tracking","Leakage Detection","AI Recommendations"]', false, 30)

ON CONFLICT (id) DO UPDATE SET
  label = EXCLUDED.label, description = EXCLUDED.description,
  capabilities = EXCLUDED.capabilities,
  is_prefetch_eligible = EXCLUDED.is_prefetch_eligible,
  updated_at = now();


-- ── Tasks ─────────────────────────────────────────────────────────────────────

INSERT INTO hospilot.task_registry (id, subagent_id, label, outputs, sort_order) VALUES

  -- sa_bed_availability
  ('ta_query_beds',                   'sa_bed_availability', 'Query all available beds — always include; returns counts by type (icu_count, hdu_count, general_count, ventilator_count, isolation_count)',  '["candidate_count","icu_count","hdu_count","general_count","ventilator_count","isolation_count","candidates"]', 10),
  ('ta_check_dirty_icu_beds',          'sa_bed_availability', 'Fallback when ICU has no clean beds — condition: ta_query_beds.icu_count == 0',                                                                '["dirty_count","dirty_beds"]',                    20),
  ('ta_check_dirty_soon_to_release',   'sa_bed_availability', 'Fallback when no beds found at all — condition: ta_query_beds.candidate_count == 0',                                                          '["beds"]',                                        30),
  ('ta_check_overflow_candidates',     'sa_bed_availability', 'Fallback when no beds found at all — condition: ta_query_beds.candidate_count == 0',                                                          '["candidates"]',                                  40),
  ('ta_check_temporary_overflow_beds', 'sa_bed_availability', 'Emergency fallback when no beds found at all — condition: ta_query_beds.candidate_count == 0',                                                '["candidates"]',                                  50),

  -- sa_dirty_bed_recovery
  ('ta_clean_vacated_beds',               'sa_dirty_bed_recovery', 'Dispatch standard housekeeping for recently discharged beds — always include for bed_cleaning task type',                          '["dispatched"]',                     10),
  ('ta_create_emergency_cleaning_task',   'sa_dirty_bed_recovery', 'Create priority cleaning job for urgently needed bed — condition: ta_check_dirty_icu_beds.dirty_count > 0',                         '["bed_ids","created"]',              20),
  ('ta_dispatch_housekeeping_fast_track', 'sa_dirty_bed_recovery', 'Dispatch fast-track housekeeping with response target < 10 minutes — condition: ta_check_dirty_icu_beds.dirty_count > 0',           '["dispatched","within_sla"]',        30),
  ('ta_escalate_to_floor_supervisor',     'sa_dirty_bed_recovery', 'Escalate to floor supervisor when housekeeping cannot respond in time — condition: ta_dispatch_housekeeping_fast_track.within_sla == 0', '["escalated"]',                  40),
  ('ta_validate_sanitization',            'sa_dirty_bed_recovery', 'Validate sanitization completed after housekeeping dispatch — condition: ta_dispatch_housekeeping_fast_track.within_sla > 0',       '["passed"]',                         50),
  ('ta_mark_bed_ready',                   'sa_dirty_bed_recovery', 'Mark bed clean and available after sanitization passes — condition: ta_validate_sanitization.passed > 0',                           '["bed_id"]',                         60),
  ('ta_check_room_readiness',             'sa_dirty_bed_recovery', 'Check overall room cleanliness and readiness status',                                                                               '["ready","issues"]',                 70),
  ('ta_validate_oxygen_readiness',        'sa_dirty_bed_recovery', 'Validate O2 pipeline is functional for a bed',                                                                                     '["functional"]',                     80),
  ('ta_check_monitor_readiness',          'sa_dirty_bed_recovery', 'Check that bedside monitor is connected and functional',                                                                            '["functional"]',                     90),
  ('ta_notify_biomedical_team',           'sa_dirty_bed_recovery', 'Alert biomedical engineering about an equipment fault',                                                                             '["notified"]',                       100),
  ('ta_sync_ready_status',               'sa_dirty_bed_recovery', 'Sync bed-ready status to the Bed Assignment Agent',                                                                                '["synced"]',                         110),
  ('ta_create_equipment_task',            'sa_dirty_bed_recovery', 'Create an equipment setup or repair task',                                                                                         '["task_id"]',                        120),

  -- sa_bed_ranking
  ('ta_rank_beds',                    'sa_bed_ranking', 'Rank candidate beds for this patient — always include when reserving',                                                                            '["ranked_beds","recommendation"]',   10),
  ('ta_filter_ventilator_beds',        'sa_bed_ranking', 'ONLY when patient requires mechanical ventilation — condition: ta_query_beds.ventilator_count > 0',                                             '["candidates"]',                     20),
  ('ta_filter_isolation_beds',         'sa_bed_ranking', 'ONLY when patient requires infection isolation — condition: ta_query_beds.isolation_count > 0',                                                 '["candidates"]',                     30),
  ('ta_apply_gender_filter',           'sa_bed_ranking', 'ONLY when gender-bay policy explicitly applies to this admission — condition: ta_query_beds.candidate_count > 0',                              '["candidates"]',                     40),
  ('ta_apply_isolation_room_filter',   'sa_bed_ranking', 'ONLY when patient requires negative-pressure isolation (TB, airborne precautions) — condition: ta_query_beds.isolation_count > 0',             '["candidates"]',                     50),
  ('ta_trigger_alternate_ward_search', 'sa_bed_ranking', 'ONLY when primary ward candidates are exhausted after filtering — condition: ta_query_beds.candidate_count > 0',                              '["candidates"]',                     60),
  ('ta_recommend_transfer_allocation', 'sa_bed_ranking', 'ONLY when no bed available in-hospital — condition: ta_query_beds.candidate_count == 0',                                                      '["recommendation"]',                 70),
  ('ta_recommend_icu_to_ward_transfer','sa_bed_ranking', 'ONLY when ICU bed is needed but ICU is full — condition: ta_query_beds.icu_count == 0',                                                        '["recommendation"]',                 80),
  ('ta_allocate_overflow_bed',         'sa_bed_ranking', 'ONLY when no standard bed available, last resort — condition: ta_query_beds.candidate_count == 0',                                            '["bed_id"]',                         90),

  -- sa_bed_reservation
  ('ta_create_approval',      'sa_bed_reservation', 'Lock bed in Redis and create approval task in Hasura — always include when reserving',            '["approval_id","bed_id"]',   10),
  ('ta_confirm_reservation',  'sa_bed_reservation', 'Confirm reservation post-approval and write audit log — always include when reserving',           '["bed_id","status"]',        20),
  ('ta_sync_bed_status',      'sa_bed_reservation', 'Sync bed status across Redis and HIS after reservation — always include when reserving',          '["synced"]',                 30),
  ('ta_hold_bed_temporarily', 'sa_bed_reservation', 'Soft-hold a bed for a patient explicitly en route but not yet arrived — ONLY when goal mentions patient en route', '["held"]', 40),

  -- sa_bed_prediction (under bed_agent)
  ('ta_predict_icu_saturation',        'sa_bed_prediction', 'Derive ICU saturation risk score from Redis bed census',                         '["saturation_pct","risk"]',          10),
  ('ta_generate_capacity_alert',       'sa_bed_prediction', 'Generate an alert when predicted occupancy exceeds threshold',                   '["alert_sent"]',                     20),
  ('ta_trigger_surge_forecast',        'sa_bed_prediction', 'Trigger a surge forecast when ER inflow is rising',                              '["forecast"]',                       30),
  ('ta_recommend_overflow_strategy',   'sa_bed_prediction', 'Use Claude to reason over census and produce redistribution plan',               '["strategy"]',                       40),
  ('ta_predict_discharge_probability', 'sa_bed_prediction', 'Predict probability each ICU patient will discharge soon',                       '["predictions"]',                    50),
  ('ta_notify_discharge_team',         'sa_bed_prediction', 'Notify discharge team of high-probability discharge patients',                   '["notified"]',                       60),
  ('ta_trigger_clearance_workflow',    'sa_bed_prediction', 'Trigger billing and pharmacy clearance for a discharge',                         '["triggered"]',                      70),
  ('ta_predict_discharge_horizon',     'sa_bed_prediction', 'Forecast time-to-next-discharge for bed capacity planning',                      '["horizon_minutes"]',                80),
  ('ta_run_surge_model',               'sa_bed_prediction', 'Run ER admission surge demand model',                                            '["demand_forecast"]',                90),
  ('ta_alert_operations_team',         'sa_bed_prediction', 'Alert the operations team of a surge prediction',                                '["notified"]',                       100),
  ('ta_notify_staffing_agent',         'sa_bed_prediction', 'Notify staffing agent of predicted surge demand',                                '["notified"]',                       110),
  ('ta_recommend_overflow_zone',       'sa_bed_prediction', 'Recommend a temporary expansion or overflow zone',                               '["recommendation"]',                 120),

  -- sa_discharge_coordination
  ('ta_trigger_discharge_coordination','sa_discharge_coordination', 'Signal the discharge workflow to accelerate a discharge', '["triggered"]', 10),

  -- sa_escalation
  ('ta_escalate_to_command_center',    'sa_escalation', 'Broadcast a full escalation alert via WebSocket',              '["escalated"]', 10),
  ('ta_escalate_allocation_conflict',  'sa_escalation', 'Escalate when no compliant bed can be found after full search', '["escalated"]', 20),

  -- sa_icu_census
  ('ta_get_icu_census', 'sa_icu_census', 'Query ICU occupancy and identify step-down candidates from Redis', '["icu_available","occupancy_pct","available_beds","ventilator_beds","step_down_candidates","escalation_candidates"]', 10),

  -- sa_icu_transfer
  ('ta_rank_icu_requests',           'sa_icu_transfer', 'Rank incoming ICU admission requests by clinical acuity — always run when ICU requests are active',                                                    '["ranked_requests","ventilator_dependent_count","deterioration_risk_count"]', 10),
  ('ta_prioritize_ventilator_bed',   'sa_icu_transfer', 'Prioritize ventilator ICU bed for ventilator-dependent patients — condition: ta_rank_icu_requests.ventilator_dependent_count > 0',                    '["ventilator_priority_count","ranked_requests"]',                           20),
  ('ta_reserve_icu_admission',       'sa_icu_transfer', 'Reserve ICU admission for top-ranked patient when beds available — condition: ta_get_icu_census.icu_available > 0',                                    '["approval_id","patient_token"]',                                           30),
  ('ta_trigger_overflow_evaluation', 'sa_icu_transfer', 'Trigger overflow evaluation when ICU full — condition: ta_get_icu_census.icu_available == 0',                                                          '["overflow_triggered","patients_pending"]',                                 40),
  ('ta_escalate_deterioration',      'sa_icu_transfer', 'Escalate priority for patients with high deterioration risk — condition: ta_rank_icu_requests.deterioration_risk_count > 0',                          '["escalated"]',                                                             50),

  -- sa_icu_stepdown
  ('ta_analyze_icu_status',  'sa_icu_stepdown', 'Analyse step-down eligibility with Claude',       '["step_down","escalations","step_down_candidates","escalation_candidates"]', 10),
  ('ta_create_icu_approval', 'sa_icu_stepdown', 'Create ICU transfer approval task in Hasura',     '["approval_id"]',  20),
  ('ta_confirm_icu_actions', 'sa_icu_stepdown', 'Execute confirmed step-down transfers',           '["confirmed"]',    30),

  -- sa_er_triage
  ('ta_get_er_visits',          'sa_er_triage', 'Query ER queue from Redis',                                                                                    '["active","visits"]',                                                                                    10),
  ('ta_triage_patients',        'sa_er_triage', 'Score and triage ER patients with Claude — outputs criticality flags for downstream conditional tasks',         '["triaged","ctas1","ctas2","critical","spo2_critical_count","protocol_flags_count","specialist_needed_count"]', 20),
  ('ta_save_triage_scores',     'sa_er_triage', 'Persist triage scores to Redis',                                                                               '["saved"]',                                                                                             30),
  ('ta_detect_cardiac_arrest',  'sa_er_triage', 'Trigger code-blue workflow when cardiac arrest is suspected — condition on ctas1 > 0',                         '["cardiac_arrest_suspected","code_blue_triggered"]',                                                    40),
  ('ta_check_spo2_critical',    'sa_er_triage', 'Escalate stabilization protocol when SpO2 is critically low — condition on spo2_critical_count > 0',           '["spo2_critical","escalated"]',                                                                         50),
  ('ta_detect_clinical_protocol','sa_er_triage','Activate sepsis/stroke/trauma protocol pathway — condition on protocol_flags_count > 0',                       '["protocol_count","protocol_activated","protocols"]',                                                   60),
  ('ta_notify_specialist',      'sa_er_triage', 'Notify relevant specialist team based on detected condition — condition on specialist_needed_count > 0',        '["notified","specialists_notified"]',                                                                   70),
  ('ta_route_fasttrack',        'sa_er_triage', 'Route low-acuity patients to fast-track',                                                                      '["routed"]',                                                                                            80),
  ('ta_select_critical',        'sa_er_triage', 'Select critical patients for admission',                                                                       '["selected","critical_patients"]',                                                                      90),

  -- sa_er_boarding
  ('ta_check_er_boarders', 'sa_er_boarding', 'Check ER boarders and escalate SLA breaches', '["boarders","escalated"]', 10),

  -- sa_ratio_monitor
  ('ta_get_ward_workload',      'sa_ratio_monitor', 'Aggregate patients and incomplete/overdue task load per ward (from admissions + clinical tasks)', '["workload"]',                                       10),
  ('ta_get_hourly_workload',    'sa_ratio_monitor', 'Bucket ward task-load by hour of day; flag peak / understaffed hours',                            '["by_hour","peak_hours","understaffed_hours","total_tasks"]', 15),
  ('ta_get_area_staffing',      'sa_ratio_monitor', 'Assess staffing for a specific area (front desk, OPD, phlebotomy, OT, recovery/PACU, lab, inpatient nursing); flag understaffed areas', '["shift","areas_assessed","areas","understaffed_areas"]', 17),
  ('ta_check_documentation_gaps','sa_ratio_monitor', 'Detect staffing documentation gaps (missing / overdue care notes, charting, unsigned records) by ward', '["documentation_tasks_pending","documentation_tasks_overdue","by_ward","flagged_wards","has_gaps"]', 18),
  ('ta_analyze_staff_workload', 'sa_ratio_monitor', 'Analyse ward workload; flag high-pressure wards and recommend same-type staff moves (Claude)',    '["recommendations","high_pressure_wards","summary"]', 20),

  -- sa_float_pool
  ('ta_create_staff_approval',        'sa_float_pool', 'Create float pool deployment approval',   '["created"]',                  10),
  ('ta_confirm_staff_recommendation', 'sa_float_pool', 'Confirm and dispatch float nurses',       '["status","recommendations"]', 20),

  -- sa_discharge_ready
  ('ta_get_discharge_candidates',  'sa_discharge_ready', 'Fetch active admissions and discharge checklists',                                                             '["candidates","count"]',                              10),
  ('ta_batch_assess_discharges',   'sa_discharge_ready', 'Assess discharge readiness for each patient with Claude',                                                     '["assessed","ready","blocked"]',                      20),
  ('ta_check_notes_completeness',  'sa_discharge_ready', 'Check all clinical notes are present for discharge-ready patients — condition: ta_batch_assess_discharges.ready > 0',  '["notes_incomplete","incomplete_admissions"]',  30),
  ('ta_request_missing_docs',      'sa_discharge_ready', 'Request missing documentation — condition: ta_check_notes_completeness.notes_incomplete > 0',                         '["requested"]',                               40),
  ('ta_check_pending_results',     'sa_discharge_ready', 'Check for pending lab/imaging results before finalizing summary — condition: ta_batch_assess_discharges.ready > 0',  '["results_pending","admissions_with_pending"]', 50),
  ('ta_generate_summaries',        'sa_discharge_ready', 'Generate AI discharge summaries — condition: ta_check_pending_results.results_pending == 0',                          '["summaries_generated"]',                     60),

  -- sa_discharge_barriers
  ('ta_create_discharge_approval', 'sa_discharge_barriers', 'Create discharge approval task in Hasura', '["approval_id"]', 10),
  ('ta_confirm_discharge_updates', 'sa_discharge_barriers', 'Execute confirmed discharge updates',      '["confirmed"]',   20),

  -- sa_stock_monitor
  ('ta_get_discharge_patients',          'sa_stock_monitor', 'Fetch discharge-ready patients for med reconciliation', '["patients"]',                   10),
  ('ta_check_medication_reconciliation', 'sa_stock_monitor', 'Check medication reconciliation gaps with Claude',      '["gaps","stock_hours_remaining"]', 20),
  ('ta_save_pharmacy_report',            'sa_stock_monitor', 'Persist pharmacy report to Hasura',                    '["saved"]',                      30),

  -- sa_ot_census
  ('ta_get_ot_cases', 'sa_ot_census', 'Fetch scheduled OT cases and available post-op beds from CarerOS', '["cases","post_op_beds"]', 10),

  -- sa_ot_analysis
  ('ta_analyze_ot_capacity', 'sa_ot_analysis', 'Identify conflicts and recommend proceed/delay/escalate per case', '["conflicts","recommendations"]', 10),

  -- sa_notifier (notification_agent) removed (oss-prep). Uncomment to restore.
  -- ('ta_gather_alerts',    'sa_notifier', 'Collect pending clinical alerts',              '["alerts"]', 10),
  -- ('ta_broadcast_alerts', 'sa_notifier', 'Send in-app notifications to relevant staff', '["sent"]',   20),

  -- sa_bed_pred_census
  ('ta_get_capacity_snapshot', 'sa_bed_pred_census', 'Count beds, ICU occupancy, discharge horizon, and ER pressure', '["total_beds","available","icu_occupancy_pct","overflow_risk"]', 10),

  -- sa_bed_pred_forecast
  ('ta_run_capacity_forecast', 'sa_bed_pred_forecast', 'Generate plain-language capacity forecast and risk classification with Claude', '["forecast","risk_level"]', 10),

  -- sa_rev_optimization
  ('ta_identify_revenue_leakage',     'sa_rev_optimization', 'Identify revenue leakage across departments and workflows — always include',                                                           '["leakage_detected","leakage_amount","unbilled_count"]',              10),
  ('ta_optimize_package_utilization', 'sa_rev_optimization', 'Optimize treatment and insurance package utilization — ONLY when goal involves package billing or package optimization',              '["packages_reviewed","savings_identified","recommendations"]',        20),
  ('ta_analyze_resource_utilization', 'sa_rev_optimization', 'Analyze utilization efficiency across hospital resources — always include',                                                           '["utilization_score","idle_equipment_count","bottlenecks"]',          30),
  ('ta_analyze_dept_profitability',   'sa_rev_optimization', 'Analyze profitability across departments and specialties — ONLY when goal involves department performance or profitability analysis', '["dept_count","below_target_count","recommendations"]',              40),

  -- sa_rev_denial_prevention
  ('ta_predict_denial_risk_rev',        'sa_rev_denial_prevention', 'Predict insurance claim denial risk before submission — always include',                                                              '["high_risk_count","medium_risk_count","denial_probability"]',       10),
  ('ta_presubmission_validation_rev',   'sa_rev_denial_prevention', 'Validate claims before payer submission — condition: ta_predict_denial_risk_rev.high_risk_count > 0',                             '["validation_passed","issues_found","missing_fields_count"]',         20),
  ('ta_payer_rule_compliance_rev',      'sa_rev_denial_prevention', 'Validate claims against payer-specific policies — always include',                                                                '["compliance_issues","non_covered_count","auth_missing_count"]',     30),
  ('ta_detect_missing_docs_rev',        'sa_rev_denial_prevention', 'Detect incomplete documentation before claim submission — always include',                                                        '["missing_docs_count","missing_summaries","missing_signatures"]',    40),
  ('ta_escalation_recommendations_rev', 'sa_rev_denial_prevention', 'Recommend escalation actions for high-risk revenue cases — condition: ta_predict_denial_risk_rev.high_risk_count > 0',           '["escalated","escalation_count"]',                                   50),

  -- sa_rev_patient_billing
  ('ta_get_patient_invoices', 'sa_rev_patient_billing', 'Fetch all invoices and claims for a specific patient', '["invoices","claims"]', 10),

  -- sa_claim_validation
  ('ta_detect_claim_discrepancies',     'sa_claim_validation', 'Detect duplicate claims, missing invoice linkages, and amount mismatches',             '["discrepancy_count","missing_invoice","duplicate_claims"]',    10),
  ('ta_validate_insurance_eligibility', 'sa_claim_validation', 'Check all pending claims for missing TPA linkage and unverified insurance',            '["eligibility_issues","no_tpa_pending_count","unverified_amount"]', 20),
  ('ta_predict_denial_risk',            'sa_claim_validation', 'Claude-based denial risk prediction across pending claims — flags high-risk items',    '["high_risk_count","medium_risk_count","claims_at_risk"]',      30),
  ('ta_check_billing_compliance',       'sa_claim_validation', 'Flag claims and invoices missing required fields or violating coding rules',           '["total_compliance_issues","claim_compliance_issues","invoice_compliance_issues"]', 40),

  -- sa_denial_prevention
  ('ta_trigger_presubmission_review',   'sa_denial_prevention', 'Trigger pre-submission review for high-denial-risk claims — condition: ta_predict_denial_risk.high_risk_count > 0',                          '["reviewed"]',   10),
  ('ta_apply_stricter_validation',      'sa_denial_prevention', 'Apply stricter TPA/insurance validation when payer history is risky — condition: ta_validate_insurance_eligibility.no_tpa_pending_count > 0', '["applied"]',    20),
  ('ta_escalate_claim_review_priority', 'sa_denial_prevention', 'Escalate claim review priority for high financial exposure — condition: ta_validate_insurance_eligibility.unverified_amount > 0',             '["escalated"]',  30),

  -- sa_billing_optimization
  ('ta_track_pending_payments',           'sa_billing_optimization', 'Bucket overdue invoices by SLA and flag high-value accounts for follow-up',                                          '["overdue_count","overdue_amount","high_value_count"]',                  10),
  ('ta_detect_revenue_leakage',           'sa_billing_optimization', 'Find claims without invoices and low-value IPD billing gaps',                                                        '["unlinked_claims_count","estimated_leakage","leakage_detected"]',       20),
  ('ta_generate_billing_recommendations', 'sa_billing_optimization', 'Claude synthesises billing state into prioritised optimisation recommendations',                                      '["recommendation_count","recommendations"]',                            30),
  ('ta_prioritize_payments',              'sa_billing_optimization', 'Rank outstanding invoices by value × aging for targeted collection',                                                 '["prioritized_count","total_recoverable","top_priority"]',               40),
  ('ta_trigger_payment_reminder',         'sa_billing_optimization', 'Send payment reminders for overdue invoices — condition: ta_track_pending_payments.overdue_count > 0',                '["reminders_sent"]',                                                    50),
  ('ta_notify_followup_team',             'sa_billing_optimization', 'Notify follow-up team about claims awaiting payer response — condition: ta_validate_insurance_eligibility.no_tpa_pending_count > 0', '["notified"]', 60)

ON CONFLICT (id) DO UPDATE SET
  label = EXCLUDED.label, outputs = EXCLUDED.outputs, updated_at = now();
