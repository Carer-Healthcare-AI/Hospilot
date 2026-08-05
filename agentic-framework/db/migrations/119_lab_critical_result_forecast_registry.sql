-- 119_lab_critical_result_forecast_registry.sql -- registers sa_lab_critical_result (lab_agent) + task.
-- Built with constants (rate drivers unsourced). Apply via: python scripts/migrate_all_tenants.py db/migrations/119_lab_critical_result_forecast_registry.sql

INSERT INTO "hospilot_app".agent_registry (id, label, description, emoji, color, is_active, sort_order)
VALUES ('lab_agent', 'Lab Operations',
   'Manages lab operations: sample tracking, TAT optimization, critical result escalation, analyzer utilization, QC compliance, test recommendations, and capacity forecasting.',
   '🧪', '#06b6d4', true, 130)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES ('sa_lab_critical_result', 'lab_agent', 'Critical Result Forecast',
   'Forward-looking forecast of CRITICAL LAB RESULTS needing an urgent clinician callback over a horizon inferred from the request (3h-3d). Include when the goal asks about expected critical results, callback workload, or critical-notification staffing over a time horizon. Distinct from sa_lab_tat (turnaround clock) and the live critical-result escalation work. NOTE: rate-driving inputs (prior critical rate, panel mix, reachability) are not sourced -- rests on a base rate.',
   '["Critical Results","Callback Workload","Critical Notification"]', false, 50)
ON CONFLICT (id) DO UPDATE SET agent_id=EXCLUDED.agent_id, label=EXCLUDED.label, description=EXCLUDED.description,
  capabilities=EXCLUDED.capabilities, is_prefetch_eligible=EXCLUDED.is_prefetch_eligible, sort_order=EXCLUDED.sort_order, updated_at=now();

INSERT INTO "hospilot_app".task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES ('ta_forecast_lab_critical_result', 'sa_lab_critical_result',
   'Forecast critical lab results per hour needing an urgent clinician callback over a horizon derived from the goal, with a recommended action, from the expected test rate and callback staffing',
   '["forecast_available","predicted_critical_results","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry SET label='Forecast critical lab results per hour needing an urgent clinician callback over a horizon derived from the goal, with a recommended action, from the expected test rate and callback staffing', updated_at=now() WHERE id='ta_forecast_lab_critical_result';
