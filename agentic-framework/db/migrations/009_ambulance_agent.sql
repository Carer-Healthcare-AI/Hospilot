-- ── Ambulance Agent — agent registry, 2 subagents, 4 tasks
-- Run in Hasura SQL console (Data → SQL)

-- ─────────────────────────────────────────────────────────────────────────────
-- AGENT
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO hospilot.agent_registry
  (id, label, description, emoji, color, sort_order, is_active)
VALUES
  (
    'ambulance_agent',
    'Ambulance Agent',
    'Assigns the best available ambulance unit, surfaces ETA and crew details, and flags emergency escalation for critical cases',
    '🚑',
    'red',
    200,
    true
  )
ON CONFLICT (id) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- SUBAGENTS
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO hospilot.subagent_registry
  (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES
  (
    'sa_ambulance_census', 'ambulance_agent',
    'Fleet Census',
    'Fetches the full ambulance fleet from the database and caches it in Redis for fast dispatch lookup',
    '["Fleet Status Query","Redis Caching"]',
    false, 10
  ),
  (
    'sa_ambulance_dispatch', 'ambulance_agent',
    'Dispatch Coordinator',
    'Assigns the best available unit by type and fuel level, surfaces ETA and crew from the DB record, and flags escalation for critical emergency types',
    '["Ambulance Assignment","Driver Coordination","ETA Surfacing","Emergency Escalation"]',
    false, 20
  )
ON CONFLICT (id) DO NOTHING;

-- ─────────────────────────────────────────────────────────────────────────────
-- TASKS
-- ─────────────────────────────────────────────────────────────────────────────

INSERT INTO hospilot.task_registry
  (id, subagent_id, label, outputs, sort_order)
VALUES
  (
    'ta_get_available_ambulances', 'sa_ambulance_census',
    'Fetch ambulance fleet from DB and cache in Redis — always include',
    '["ambulances"]',
    10
  ),
  (
    'ta_assign_ambulance', 'sa_ambulance_dispatch',
    'Assign best available unit, surface ETA and crew, flag escalation with Claude',
    '["assigned_vehicle_no","eta_mins","escalate"]',
    10
  ),
  (
    'ta_create_ambulance_approval', 'sa_ambulance_dispatch',
    'Create dispatch approval task — condition: ta_assign_ambulance.assigned_vehicle_no != null',
    '["approval_id"]',
    20
  ),
  (
    'ta_confirm_ambulance_dispatch', 'sa_ambulance_dispatch',
    'Confirm dispatch and write audit log — condition: ta_create_ambulance_approval.approval_id != null',
    '["confirmed"]',
    30
  )
ON CONFLICT (id) DO NOTHING;
