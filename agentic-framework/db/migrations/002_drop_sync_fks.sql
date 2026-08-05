-- Mirror tables don't need FK enforcement — referential integrity lives in CarerOS.
-- Nursing tasks and discharge summaries can reference admissions not yet in our mirror
-- (e.g. discharged admissions filtered from the admissions sync).

ALTER TABLE hospilot.nursing_tasks
    DROP CONSTRAINT IF EXISTS nursing_tasks_admission_id_fkey CASCADE;

ALTER TABLE hospilot.discharge_summaries
    DROP CONSTRAINT IF EXISTS discharge_summaries_admission_id_fkey CASCADE;

ALTER TABLE hospilot.vitals
    DROP CONSTRAINT IF EXISTS vitals_admission_id_fkey CASCADE;
