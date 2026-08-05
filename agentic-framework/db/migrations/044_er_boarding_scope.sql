-- ─────────────────────────────────────────────────────────────────────────────
-- Gap G42 — Boarding Monitor out of flow vs the not-yet-triaged ER queue.
--
-- Query #1 ("2 AM night shift, 40% fewer nurses, 11 unattended ER") is an
-- active-queue / not-yet-triaged crisis, but the planner still selects the
-- Boarding Monitor (sa_er_boarding), which tracks a DIFFERENT population --
-- patients ALREADY ADMITTED and physically still in the ED awaiting an inpatient
-- bed. The sub-agent selection step reads ONLY subagent_registry.description, and
-- the prior text ("...independent of the triage flow") nudged the planner to
-- include it for any ER crisis without saying when NOT to.
--
-- Fix: scope the description so Boarding Monitor is selected ONLY when the
-- concern is admitted-patient boarding, and excluded for the not-yet-triaged
-- active queue / general ER wait / understaffing (that is the Triage Monitor's
-- job). Idempotent UPDATE.
-- Python fallback parity: SUB_AGENTS['er_agent'] sa_er_boarding in workflows/planner.py.
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE hospilot_app.subagent_registry
   SET description = 'Checks ER BOARDERS — patients ALREADY ADMITTED who are physically still in the ED waiting for an inpatient bed — and escalates bed-wait SLA breaches. Include ONLY when the concern is admitted patients boarding in the ED (bed-block / boarding SLA). Do NOT include for the not-yet-triaged / unattended active ER queue, general ER wait times, or ER understaffing — that active-queue crisis belongs to the Triage Monitor, not boarding. Boarding tracks a different population (admitted, awaiting a bed) than the arrival/triage queue.',
       updated_at  = now()
 WHERE id = 'sa_er_boarding';
