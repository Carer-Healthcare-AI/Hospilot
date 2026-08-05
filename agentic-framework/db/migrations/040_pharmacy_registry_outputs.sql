-- ─────────────────────────────────────────────────────────────────────────────
-- G9 fix: pharmacy task_registry outputs → match emitted keys
-- Corrects 10 tasks across sa_pharmacy_queue, sa_pharmacy_capacity,
-- and sa_medication_substitution where declared outputs diverged from code.
-- ─────────────────────────────────────────────────────────────────────────────

-- sa_pharmacy_queue ──────────────────────────────────────────────────────────

-- was: ["bottleneck_stage","bottleneck_count"]
-- code emits: bottleneck_dept, in_progress_count, dept_breakdown
UPDATE hospilot_app.task_registry
SET outputs = '["bottleneck_dept","in_progress_count","dept_breakdown"]'
WHERE id = 'ta_analyze_queue_bottleneck';

-- was: ["reprioritized_count"]
-- code emits: stat_prioritized, stat_orders
UPDATE hospilot_app.task_registry
SET outputs = '["stat_prioritized","stat_orders"]'
WHERE id = 'ta_prioritize_stat_medications';

-- was: ["escalated","breach_count"]
-- code emits: tat_breach_count, escalated, breach_orders
UPDATE hospilot_app.task_registry
SET outputs = '["tat_breach_count","escalated","breach_orders"]'
WHERE id = 'ta_escalate_tat_breach';

-- sa_pharmacy_capacity ───────────────────────────────────────────────────────

-- was: ["has_history","avg_daily_orders"]
-- code emits: history_days, avg_daily_orders, max_daily_orders, history_sample
UPDATE hospilot_app.task_registry
SET outputs = '["history_days","avg_daily_orders","max_daily_orders"]'
WHERE id = 'ta_fetch_dispensing_history';

-- was: ["forecast_orders","surge_expected"]
-- code emits: predicted_orders, surge_expected, trend, ai_forecast
UPDATE hospilot_app.task_registry
SET outputs = '["predicted_orders","surge_expected","trend"]'
WHERE id = 'ta_forecast_pharmacy_demand';

-- was: ["capacity_gap","at_risk"]
-- code emits: capacity_ok, active_order_count, surge_threshold
UPDATE hospilot_app.task_registry
SET outputs = '["capacity_ok","active_order_count","surge_threshold"]'
WHERE id = 'ta_check_dispensing_capacity';

-- sa_medication_substitution ─────────────────────────────────────────────────

-- was: ["unavailable_count","unavailable_drugs"]
-- code emits: unavailable_count, unavailable_meds
UPDATE hospilot_app.task_registry
SET outputs = '["unavailable_count","unavailable_meds"]'
WHERE id = 'ta_check_unavailable_medications';

-- was: ["substitute_available","alternatives_found"]
-- code emits: substitute_available, substitute_count, substitutes
UPDATE hospilot_app.task_registry
SET outputs = '["substitute_available","substitute_count","substitutes"]'
WHERE id = 'ta_search_formulary_alternatives';

-- was: ["approval_sent","approved_count"]
-- code emits: approved_count, pending_approval  (no approval_sent key)
UPDATE hospilot_app.task_registry
SET outputs = '["approved_count","pending_approval"]'
WHERE id = 'ta_request_physician_approval';

-- was: ["orders_updated"]
-- code emits: substitution_updated, substituted_meds
UPDATE hospilot_app.task_registry
SET outputs = '["substitution_updated","substituted_meds"]'
WHERE id = 'ta_update_substitution_order';
