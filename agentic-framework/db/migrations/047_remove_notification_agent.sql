-- Migration 047: remove the notification_agent from the registry (oss-prep).
--
-- The notification agent was a low-value, rarely-selected agent: the planner only
-- picked it when a goal explicitly asked to "alert / notify / page" someone. Alerts
-- themselves are a shared, cross-agent WebSocket channel (every clinical agent emits
-- its own `type: "alert"` events), so removing this agent loses no alerting behaviour.
--
-- The agent/sub-agent/task definitions remain commented out in
-- 003_agent_registry_seed.sql for reference if we ever need to restore it.
--
-- FK cascade: deleting the agent_registry row removes its subagent_registry row
-- (sa_notifier) and their task_registry rows (ta_gather_alerts, ta_broadcast_alerts).

BEGIN;

DELETE FROM hospilot_app.agent_registry
 WHERE id = 'notification_agent';

COMMIT;
