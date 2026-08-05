-- Migration 043: gate ta_allocate_overflow_bed away from vent/monitoring-dependent
-- patients (G15). Overflow beds have no piped O2, ventilator or monitoring --
-- external transfer is the correct last resort for those patients.

BEGIN;

UPDATE hospilot_app.task_registry
   SET label      = 'ONLY when no standard bed available, last resort, AND patient is NOT ventilator-dependent or monitoring-dependent — for vent/monitoring-dependent patients external transfer is the correct last resort, not overflow bed allocation',
       updated_at = now()
 WHERE id = 'ta_allocate_overflow_bed';

COMMIT;
