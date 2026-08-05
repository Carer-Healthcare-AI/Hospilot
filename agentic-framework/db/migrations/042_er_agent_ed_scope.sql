-- ─────────────────────────────────────────────────────────────────────────────
-- Gap G50 — ER Coordination pathway used for non-ED events.
--
-- Queries #30 ("79yo fell in Ward 5, hip injury") and #43 ("5 elective surgical
-- patients waiting >6h for beds") both routed through er_agent, so ER triage /
-- CTAS / boarding logic ran for patients who NEVER came through the Emergency
-- Department — an already-admitted inpatient's ward fall, and elective/ward
-- patients waiting for an inpatient bed. The stage-1 planner reasons only over
-- agent_registry.description, and er_agent's seed description ("Monitors
-- emergency patients ... routes patients to the right care setting") is generic
-- enough that any acute/urgent-sounding event pulls it in.
--
-- Fix: scope the er_agent description to NEW ED arrivals only, and explicitly
-- exclude in-hospital events involving patients who never entered the ED
-- (inpatient falls/deterioration -> clinical escalation; elective/ward bed
-- waiters -> bed_agent). Idempotent UPDATE.
-- Python fallback parity: AVAILABLE_AGENTS in workflows/planner.py.
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE hospilot_app.agent_registry
   SET description = 'owns EMERGENCY DEPARTMENT flow for patients ARRIVING at the ED from outside the hospital: triage/CTAS scoring, acuity response, fast-track, admission selection, and ER boarding (patients admitted FROM the ED still awaiting an inpatient bed). Scope is NEW ED arrivals only. Do NOT use for in-hospital events involving patients who never came through the ED: an already-admitted inpatient''s fall or deterioration on a ward is a clinical-escalation event, not ER triage; and elective/scheduled or ward patients waiting for an inpatient bed belong to bed_agent — ER triage/CTAS/boarding logic does not apply to them.',
       updated_at = now()
 WHERE id = 'er_agent';
