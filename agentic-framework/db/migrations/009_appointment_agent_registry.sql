-- ─────────────────────────────────────────────────────────────────────────────
-- Appointment Agent — registry rows (source of truth for the DB-driven planner)
-- Inserts into hospilot_app.agent_registry / subagent_registry / task_registry so
-- planner._fetch_registry() and the generic RegistryAgentWorkflow pick it up.
-- Built-in tasks: is_dynamic=false, function_code NULL (run via run_builtin_task).
-- Idempotent via ON CONFLICT (id) DO NOTHING.
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO hospilot_app.agent_registry (id, label, description, emoji, color, is_active, sort_order) VALUES
  ('appointment_agent', 'Appointments',
   'Schedules OPD appointments, sends reminders, and prevents no-shows.',
   '📅', '#14b8a6', true, 120)
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot_app.subagent_registry (id, agent_id, label, description, is_active, is_prefetch_eligible, sort_order) VALUES
  ('sa_appt_scheduling', 'appointment_agent', 'Scheduling Agent',          'Finds bookable OPD slots, matches them to the requested specialty, books the earliest suitable slot, and reconciles cancellations against open capacity. Handles slot-finding and booking for scheduling and waitlist/cancellation queries.', true, true,  10),
  ('sa_appt_reminder',   'appointment_agent', 'Reminder Agent',            'Multi-channel reminders, pre-visit prep, follow-up, escalation',                       true, false, 20),
  ('sa_appt_noshow',     'appointment_agent', 'No-Show Prevention Agent',  'Predicts no-show risk and engages high-risk patients; prepares replacement candidates from the cancellation pool to backfill predicted no-show slots (counts candidates; does not auto-assign to slots).',        true, false, 30)
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot_app.task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order) VALUES
  ('ta_appt_find_available_slots',      'sa_appt_scheduling', 'Find bookable OPD doctor slots',                          '["available_slot_count","earliest_slot","slots_by_specialty"]', true, false, 10),
  ('ta_appt_match_specialty',           'sa_appt_scheduling', 'Match requested specialty to providers',                  '["specialty_matched","provider_count"]',                       true, false, 20),
  ('ta_appt_match_waitlist',            'sa_appt_scheduling', 'Match waitlisted patients to open slots: order by priority, pair each to the earliest suitable same-specialty slot (feeds booking)', '["waitlist_count","matched_count","unmatched_count","matches"]', true, false, 25),
  ('ta_appt_reserve_slot',              'sa_appt_scheduling', 'Book the matched patient<->slot pairs (or the earliest suitable slot)', '["slot_reserved","matched_count","appointment_id"]',           true, false, 30),
  ('ta_appt_prioritize_urgent',         'sa_appt_scheduling', 'Find same-day / priority slot for urgent patient',        '["same_day_available","urgent_slot_found"]',                   true, false, 40),
  ('ta_appt_coordinate_multispecialty', 'sa_appt_scheduling', 'Find a common window across multiple specialties',        '["common_window_found","specialties_available"]',              true, false, 50),
  ('ta_appt_fill_cancellation',         'sa_appt_scheduling', 'Count cancelled appointments and open slots; report how many cancellations are fillable from the cancellation pool (no patient-slot assignment)', '["cancelled_count","slots_fillable"]',                         true, false, 60),
  ('ta_appt_classify_movable',          'sa_appt_scheduling', 'Classify upcoming appointments as urgent (keep) vs non-urgent / movable', '["assessed","movable_count","urgent_count","movable"]',        true, false, 70),
  ('ta_appt_reschedule',                'sa_appt_scheduling', 'Reschedule non-urgent appointments away from peak understaffed hours to off-peak slots', '["rescheduled","proposed_count","avoid_hours","avoid_source"]', true, false, 80),
  ('ta_appt_find_service_slots',        'sa_appt_scheduling', 'Find open non-OPD slots (sample collection / phlebotomy, pharmacy pickup)', '["slot_type","available_slot_count","slots_by_type","earliest_slot"]', true, false, 85),
  ('ta_appt_book_service_slot',         'sa_appt_scheduling', 'Book patients into sample-collection or pharmacy-pickup windows and notify them', '["slot_reserved","booked_count","slot_type","matched_count","windows"]', true, false, 90),

  ('ta_appt_get_due_reminders',   'sa_appt_reminder', 'List appointments due for a reminder (next 48h)',     '["due_count","appointments_due"]',              true, false, 10),
  ('ta_appt_resolve_channel',     'sa_appt_reminder', 'Resolve SMS/email channel per patient',               '["channel_resolved","sms_count","email_count"]', true, false, 20),
  ('ta_appt_send_reminders',      'sa_appt_reminder', 'Send appointment reminders',                          '["reminders_sent","delivery_failed"]',          true, false, 30),
  ('ta_appt_prep_instructions',   'sa_appt_reminder', 'Send pre-visit prep (fasting/lab) instructions',      '["prep_sent","patients_with_prep"]',            true, false, 40),
  ('ta_appt_followup_reminders',  'sa_appt_reminder', 'Send follow-up reminders',                            '["followup_count","followup_sent"]',            true, false, 50),
  ('ta_appt_escalate_reminder',   'sa_appt_reminder', 'Escalate unconfirmed high-risk patients',             '["escalated_count"]',                           true, false, 60),

  ('ta_appt_predict_noshow',       'sa_appt_noshow', 'Predict no-show risk for upcoming appointments',  '["assessed","high_risk_count","predictions"]', true, false, 10),
  ('ta_appt_flag_high_risk',       'sa_appt_noshow', 'Flag high-risk appointments and intervene',       '["flagged"]',                                  true, false, 20),
  ('ta_appt_proactive_engagement', 'sa_appt_noshow', 'Proactively engage high/medium-risk patients',    '["engaged"]',                                  true, false, 30),
  ('ta_appt_waitlist_replacement', 'sa_appt_noshow', 'Count replacement candidates from the cancellation pool for predicted no-show slots (after no-show prediction; no auto-assignment)','["replacements_prepared"]',                    true, false, 40),
  ('ta_appt_chronic_noshow',       'sa_appt_noshow', 'Identify chronic no-show patients (>=3)',          '["chronic_count","chronic_patients"]',         true, false, 50)
ON CONFLICT (id) DO NOTHING;

