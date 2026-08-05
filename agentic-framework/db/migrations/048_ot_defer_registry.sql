-- ─────────────────────────────────────────────────────────────────────────────
-- OT reprioritisation (executable) — defer delay-flagged electives.
--
-- ta_ot_defer_electives reads analyze_ot_capacity's per-case dispositions and, for each
-- elective flagged 'delay' (yielding to a non-elective/emergency or a conflict), stages a
-- move to a later open theatre slot. Rides the SAME ot_reschedules move channel as
-- ta_ot_reschedule_surgery (commit -> Fabric surgery_reschedule -> ot_surgeries UPDATE), so
-- no new commit/Fabric plumbing. Makes the "compare incoming OT vs existing and defer the
-- less-severe cases" recommendation actually execute. Idempotent.
-- Python parity: SUB_AGENTS['ot_agent'] sa_ot_analysis in workflows/planner.py.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO hospilot_app.task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  ('ta_ot_defer_electives', 'sa_ot_analysis',
   'Reprioritisation (executable) — move the electives flagged delay to a later open theatre slot to free capacity for higher-acuity cases; stages the moves for commit — include when electives must yield to a non-elective/emergency or a conflict',
   '["deferred","proposals","status"]', true, false, 30)
ON CONFLICT (id) DO UPDATE SET
  subagent_id = EXCLUDED.subagent_id, label = EXCLUDED.label, outputs = EXCLUDED.outputs,
  is_active = true, sort_order = EXCLUDED.sort_order, updated_at = now();
