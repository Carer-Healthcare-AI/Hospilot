-- ─────────────────────────────────────────────────────────────────────────────
-- Gap G36 — Notification Agent redundant with the Appointments Reminder Agent.
--
-- For query #35 ("Monday OPD: 18 no-shows / 40") the planner pulled in
-- notification_agent to "remind" patients, but notification_agent only broadcasts
-- IN-APP alerts to STAFF (sa_notifier: ta_gather_alerts + ta_broadcast_alerts) --
-- it cannot reach patients. The real patient-reminder / no-show-outreach logic
-- lives in appointment_agent's Reminder Agent (sa_appt_reminder).
--
-- Root cause: the runtime (DB) description was the permissive 003 seed ("Sends
-- clinical alerts to the right staff at the right time"), looser than even the
-- Python fallback, so the LLM read "notify/remind" and selected it. Fix: sharpen
-- the description to STAFF-only in-app alerts and explicitly hand patient reminders
-- to appointment_agent. Idempotent UPDATE.
-- Python fallback parity: AVAILABLE_AGENTS in workflows/planner.py.
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE hospilot_app.agent_registry
   SET description = 'owns sending in-app alerts to STAFF (clinicians, on-call / code teams, supervisors) — include ONLY when the goal explicitly asks to alert / notify / page staff. NOT patient-facing: patient reminders, recalls and no-show outreach (appointments, follow-ups) are handled by appointment_agent''s Reminder Agent, not here. Do not add notification_agent to reach patients.',
       updated_at = now()
 WHERE id = 'notification_agent';
