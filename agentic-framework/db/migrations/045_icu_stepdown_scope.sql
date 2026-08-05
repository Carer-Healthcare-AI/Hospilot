-- ─────────────────────────────────────────────────────────────────────────────
-- Gap G37 — Step-Down Coordinator invoked when step-down isn't the task.
--
-- The Step-Down Coordinator (sa_icu_stepdown) was selected for a FULL HOSPITAL
-- DISCHARGE (query #41 "14-day ICU discharge, complex insurance claim") and for a
-- DOCUMENTATION / NOTES review (query #45 "3 PM ICU shift change, 2 patients
-- missing notes") -- neither is an internal ICU->ward step-down transfer. Sub-agent
-- selection reads ONLY subagent_registry.description, and the seed text ("Confirms
-- clinical criteria for step-down and arranges transfer to a lower-acuity bed")
-- reads broadly enough that any ICU-discharge/transfer-sounding goal pulls it in.
--
-- Fix: scope the description to an INTERNAL step-down only, and explicitly exclude
-- full hospital discharge (-> discharge_agent) and documentation/notes reviews.
-- This is also the root cause of G41: when step-down wrongly runs for a discharge
-- it emits phantom step_down_candidates that the bed agent then reserves for.
-- Idempotent UPDATE. Python fallback parity: SUB_AGENTS['icu_agent'] in planner.py.
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE hospilot_app.subagent_registry
   SET description = 'Confirms clinical criteria for STEP-DOWN — an INTERNAL transfer of an ICU patient who is STAYING in the hospital to a lower-acuity bed (ward / HDU / progressive care) — and arranges that transfer. Include ONLY when the task is an internal ICU-to-ward step-down. Do NOT use for a FULL HOSPITAL DISCHARGE (patient leaving the hospital — that is discharge_agent), nor for a documentation / notes / records review (which is not a transfer at all).',
       updated_at  = now()
 WHERE id = 'sa_icu_stepdown';
