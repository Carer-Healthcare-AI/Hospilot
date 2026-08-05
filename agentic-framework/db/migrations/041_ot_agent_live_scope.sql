-- ─────────────────────────────────────────────────────────────────────────────
-- Gap G52 — OT Scheduling agent (live schedule) used for a retrospective audit.
--
-- Query #44 ("TPA: 23% claims denied for missing pre-auth — audit") is a
-- RETROSPECTIVE claims/denial audit, but the stage-1 planner pulled in ot_agent
-- because its description read as the owner of all surgical / pre-auth / case
-- work. Every OT task reads the LIVE OT schedule from Redis (agents/ot/
-- activities.py) — the wrong data source for a historical audit, which needs
-- historical claim/case records owned by revenue_agent / billing_agent.
--
-- Fix: sharpen the ot_agent description so the planner (which reasons only over
-- agent_registry.description) knows OT is LIVE/forward-only and stays out of any
-- retrospective / historical claim·TPA·pre-auth·case audit. Idempotent UPDATE.
-- Python fallback parity: AVAILABLE_AGENTS in workflows/planner.py.
-- ─────────────────────────────────────────────────────────────────────────────

UPDATE hospilot_app.agent_registry
   SET description = 'owns LIVE operating-theatre scheduling: today''s/upcoming surgical list, theatre capacity & turnaround, emergency case insertion, and post-op bed planning — always read from the live/forward OT schedule. MAY run alongside financial agents when live surgical scheduling is genuinely part of the goal (e.g. an overrunning list plus its cost impact, or confirming pre-auth before booking tomorrow''s cases). The one thing it does NOT do is a RETROSPECTIVE look-back: a "what happened" audit of PAST surgical cases or denied claims (TPA / pre-auth / denial audit over closed encounters) reads historical records owned by revenue_agent or billing_agent — do not add ot_agent for a look-back audit, because its live schedule holds none of that history.',
       updated_at = now()
 WHERE id = 'ot_agent';
