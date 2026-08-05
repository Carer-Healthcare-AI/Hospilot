-- Migration 044: gate ta_check_dirty_icu_beds to ICU-bed goals only (G17).
-- The task was firing as a fallback for any bed type (general, HDU, surgical,
-- paediatric) whenever icu_count == 0, regardless of what the patient needed.

BEGIN;

UPDATE hospilot_app.task_registry
   SET label      = 'Fallback when ICU has no clean beds — ONLY when patient requires an ICU bed; condition: ta_query_beds.icu_count == 0',
       updated_at = now()
 WHERE id = 'ta_check_dirty_icu_beds';

COMMIT;
