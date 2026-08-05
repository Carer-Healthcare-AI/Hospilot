-- ── OT Agent full build — 2 new subagents, 13 tasks (1 census update + 6 turnaround + 7 scheduling - 1 old)
-- Run in Hasura SQL console (Data → SQL)

-- ─────────────────────────────────────────────────────────────────────────────
-- SUBAGENTS
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO hospilot.subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  (
    'sa_ot_turnaround', 'ot_agent',
    'OT Turnaround Agent',
    'Reduces OT idle time — coordinates theatre cleaning, validates instrument readiness, tracks turnaround, predicts delays, coordinates staff, and scores OT efficiency',
    '["OT Cleaning Coordination","Instrument Readiness Validation","OT Turnaround Tracking","Delay Prediction","Staff Coordination","OT Efficiency Optimisation"]',
    false, 20
  ),
  (
    'sa_ot_scheduling', 'ot_agent',
    'OT Scheduling Agent',
    'Optimises OT planning — detects conflicts, handles emergencies, checks resources, optimises slots, balances load, and dynamically prioritises the surgical list',
    '["Surgery Slot Optimisation","Surgeon Coordination","Emergency OT Handling","Conflict Detection","Resource-Aware Scheduling","Dynamic OT Prioritisation","OT Load Balancing"]',
    false, 30
  )
ON CONFLICT (id) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- TASKS
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO hospilot.task_registry
  (id, subagent_id, label, outputs, sort_order)
VALUES
  -- census (update existing subagent with new task)
  ('ta_get_ot_census',        'sa_ot_census',       'Fetch OT schedule, rooms, room status, and today''s equipment from cache — always include',                                                       '["schedule","rooms","room_status","upcoming_surgeries","upcoming_count"]',            10),
  -- turnaround
  ('ta_ot_check_cleaning',    'sa_ot_turnaround',   'OT cleaning coordination — check which theatres need cleaning before next case — always include',                                                 '["cleaning_count","rooms_to_clean"]',                                                10),
  ('ta_ot_check_instruments', 'sa_ot_turnaround',   'Instrument readiness validation — validate equipment availability for upcoming surgeries — condition: ta_get_ot_census.upcoming_count > 0',     '["gap_count","ready_count","gaps"]',                                                  20),
  ('ta_ot_track_turnaround',  'sa_ot_turnaround',   'OT turnaround tracking — track theatre utilisation and active surgeries — always include',                                                        '["rooms_active","active_count","utilisation_pct"]',                                   30),
  ('ta_ot_predict_delays',    'sa_ot_turnaround',   'Delay prediction — predict on-time start risks per theatre — always include',                                                                     '["delay_risks","high_risk_count"]',                                                   40),
  ('ta_ot_coordinate_staff',  'sa_ot_turnaround',   'Staff coordination — assign staff actions to address delay risks and gaps — condition: ta_ot_predict_delays.high_risk_count > 0',               '["staff_actions"]',                                                                   50),
  ('ta_ot_score_efficiency',  'sa_ot_turnaround',   'OT efficiency optimisation — calculate overall OT efficiency score — always include',                                                             '["efficiency_score"]',                                                                60),
  -- scheduling
  ('ta_ot_detect_conflicts',   'sa_ot_scheduling',  'Conflict detection — detect room double-bookings and surgeon overlaps — always include',                                                          '["conflict_count","has_conflicts","room_conflicts","surgeon_conflicts"]',             10),
  ('ta_ot_find_emergencies',   'sa_ot_scheduling',  'Emergency OT handling — identify emergency and urgent cases in the schedule — always include',                                                   '["emergency_count","emergency_cases"]',                                               20),
  ('ta_ot_check_resources',    'sa_ot_scheduling',  'Resource-aware scheduling — assess room availability and case load per theatre — always include',                                                '["available_rooms","utilisation_pct","under_resourced"]',                            30),
  ('ta_ot_handle_emergencies', 'sa_ot_scheduling',  'Emergency OT handling — plan immediate actions for emergency cases — condition: ta_ot_find_emergencies.emergency_count > 0',                    '["emergency_actions"]',                                                               40),
  ('ta_ot_optimise_slots',     'sa_ot_scheduling',  'Surgery slot optimisation — recommend slot swaps to resolve conflicts — condition: ta_ot_detect_conflicts.has_conflicts == true',               '["slot_optimizations"]',                                                              50),
  ('ta_ot_balance_load',       'sa_ot_scheduling',  'OT load balancing — balance case load across available theatres — always include',                                                               '["load_balance","summary"]',                                                          60),
  ('ta_ot_prioritise_cases',   'sa_ot_scheduling',  'Dynamic OT prioritisation — reorder cases based on conflicts and emergencies — condition: ta_ot_detect_conflicts.conflict_count > 0',           '["priority_adjustments"]',                                                            70)
ON CONFLICT (id) DO NOTHING;
