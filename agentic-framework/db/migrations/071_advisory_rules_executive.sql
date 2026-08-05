-- 071_advisory_rules_executive.sql
--
-- Executive advisory rules -- meta-rules over the whole house. Evaluators:
-- workflows/graph/advisory_evaluators.py (Executive section). Data: the same
-- projections/DB reads the domain rules use, plus the advisories fire history
-- (hasura.list_advisories_since; Executive-topic fires excluded so the
-- meta-rules never feed on their own output). All clock-only.
--
-- Proxies imposed by the data (documented in the evaluators):
--   * no HIS stress index exists -> weighted composite of domain pressure
--     signals (weights/component_norms operator-editable in params)
--   * no KPI store exists -> "KPI deteriorating" = per-topic advisory fire
--     rate vs its trailing baseline daily average
--   * capacity forecast is the deterministic ward-level sibling of
--     bed_occupancy_forecast_critical (which stays house-level + ML)
--   * "rebalance automatically" is suggested_action text only -- the engine
--     is notify-only; imbalance is measured on beds (staff not integrated)
--
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/071_advisory_rules_executive.sql
-- (no --track-only needed: only new TABLES need tracking, not rows)
-- Keep in sync with db/init/tenant_template.sql. Idempotent: safe to re-run --
-- ON CONFLICT DO NOTHING never clobbers operator-edited thresholds.

INSERT INTO hospilot_app.advisory_rules
  (rule_key, topic, label, condition_description, suggested_action,
   severity, params, trigger_entities, check_interval_seconds, cooldown_seconds)
VALUES
  ('exec_stress_index', 'Executive', 'Hospital stress index high',
   'Hospital Stress Index high',
   'Launch hospital-wide optimization workflow',
   'critical', '{"stress_index_threshold": 70, "weights": {"bed_occupancy": 0.35, "er_boarding": 0.25, "ot_backlog": 0.15, "discharge_delays": 0.15, "lab_tat": 0.10}, "component_norms": {"bed_occupancy": 90, "er_boarding": 5, "ot_backlog": 2, "discharge_delays": 5, "lab_tat": 5}}',
   '[]', 1800, 21600),

  ('exec_sla_breaches', 'Executive', 'Multiple SLA breaches',
   'Multiple SLA breaches',
   'Escalate to command center dashboard',
   'warning', '{"min_breaches": 3, "window_hours": 4, "sla_rule_keys": ["bed_turnaround_sla", "lab_tat_sla", "lab_collection_delayed", "ot_first_case_delayed", "discharge_delayed"]}',
   '[]', 900, 14400),

  ('exec_capacity_forecast', 'Executive', 'Capacity forecast critical',
   'Capacity forecast critical',
   'Recommend surge capacity plan',
   'critical', '{"predicted_occupancy_pct": 90, "horizon_hours": 24, "min_wards_critical": 2, "min_ward_beds": 5}',
   '[]', 3600, 21600),

  ('exec_kpi_deteriorating', 'Executive', 'Operational KPI deteriorating',
   'Operational KPI deteriorating',
   'Generate executive action plan',
   'warning', '{"min_pct_deterioration": 25, "window_hours": 24, "baseline_days": 7, "min_baseline_fires": 5}',
   '[]', 3600, 86400),

  ('exec_utilization_imbalance', 'Executive', 'Resource utilization imbalance',
   'Resource utilization imbalance',
   'Rebalance beds, staff, and operational resources automatically',
   'warning', '{"max_occupancy_spread_pct": 30, "min_ward_beds": 5, "min_wards": 2}',
   '[]', 3600, 21600)
ON CONFLICT (rule_key) DO NOTHING;
