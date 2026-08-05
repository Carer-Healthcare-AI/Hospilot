-- 118_lab_tat_forecast_registry.sql -- registers sa_lab_tat (lab_agent) + task ta_forecast_lab_tat.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/118_lab_tat_forecast_registry.sql

INSERT INTO "hospilot_app".agent_registry (id, label, description, emoji, color, is_active, sort_order)
VALUES ('lab_agent', 'Lab Operations',
   'Manages lab operations: sample tracking, TAT optimization, critical result escalation, analyzer utilization, QC compliance, test recommendations, and capacity forecasting.',
   '🧪', '#06b6d4', true, 130)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES ('sa_lab_tat', 'lab_agent', 'Lab Turnaround Forecast', 'Forward-looking forecast of lab TURNAROUND TIME -- the p90 collection-to-result clock (minutes) for a priority band (STAT/Urgent/Routine/Batch) over a horizon inferred from the request (3h-3d). Include when the goal asks about predicted lab TAT, result turnaround, or SLA-clock risk over a time horizon. Distinct from sa_lab_test_volume (specimen COUNT) and the live TAT-bottleneck work -- this predicts the turnaround CLOCK.', '["Lab Turnaround","TAT Clock","Result SLA"]', false, 40)
ON CONFLICT (id) DO UPDATE SET agent_id=EXCLUDED.agent_id, label=EXCLUDED.label, description=EXCLUDED.description,
  capabilities=EXCLUDED.capabilities, is_prefetch_eligible=EXCLUDED.is_prefetch_eligible, sort_order=EXCLUDED.sort_order, updated_at=now();

INSERT INTO "hospilot_app".task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES ('ta_forecast_lab_tat', 'sa_lab_tat', 'Forecast the p90 collection-to-result turnaround time (minutes) for a priority band over a horizon derived from the goal, with a recommended action, from current backlog, analyzers operational and technologist staffing', '["forecast_available","predicted_turnaround_minutes","test_priority","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry SET label='Forecast the p90 collection-to-result turnaround time (minutes) for a priority band over a horizon derived from the goal, with a recommended action, from current backlog, analyzers operational and technologist staffing', updated_at=now() WHERE id='ta_forecast_lab_tat';
