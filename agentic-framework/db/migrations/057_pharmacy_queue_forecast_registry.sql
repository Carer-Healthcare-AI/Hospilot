-- ============================================================
-- 057_pharmacy_queue_forecast_registry.sql
-- Registers ta_forecast_pharmacy_queue under the existing
-- sa_pharmacy_queue (pharmacy_agent) -- next-hour dispensing-counter
-- queue-length forecast via Hospilot /pharmacy/queue.
-- Wired in both paths (pharmacy_agent_workflow + run_pharmacy_body).
-- sa_pharmacy_queue already exists (016, hospilot_app) so no FK guard
-- needed; kept idempotent + self-healing label.
-- ============================================================

INSERT INTO "hospilot_app".task_registry
  (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES
  ('ta_forecast_pharmacy_queue', 'sa_pharmacy_queue',
   'Forecast the dispensing-counter queue length for the next hour',
   '["forecast_available","predicted_queue_length","queue_level"]', true, false, 50)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry
   SET label      = 'Forecast the dispensing-counter queue length for the next hour',
       updated_at = now()
 WHERE id = 'ta_forecast_pharmacy_queue';
