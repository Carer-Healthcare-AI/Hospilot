-- Migration 045: gate ta_confirm_staff_recommendation on approval existing (G19).
-- The task planner was ordering confirm before approve because the label had no
-- dependency hint. Adding the condition makes the planner emit the correct edge
-- direction and gives should_run_task a typed guard as a safety net.

BEGIN;

UPDATE hospilot_app.task_registry
   SET label      = 'Confirm and dispatch float nurses — condition: ta_create_staff_approval.created == true',
       updated_at = now()
 WHERE id = 'ta_confirm_staff_recommendation';

COMMIT;
