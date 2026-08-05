-- 117_ambulance_availability_forecast_registry.sql -- registers sa_ambulance_availability (ambulance_agent) + task ta_forecast_ambulance_availability.
-- Apply via: python scripts/migrate_all_tenants.py db/migrations/117_ambulance_availability_forecast_registry.sql

INSERT INTO "hospilot_app".agent_registry (id, label, description, emoji, color, is_active, sort_order)
VALUES ('ambulance_agent', 'Ambulance Agent',
   'Assigns the best available ambulance unit, surfaces ETA and crew details, and flags emergency escalation for critical cases',
   '🚑', '#ef4444', true, 200)
ON CONFLICT (id) DO NOTHING;

INSERT INTO "hospilot_app".subagent_registry (id, agent_id, label, description, capabilities, is_prefetch_eligible, sort_order)
VALUES ('sa_ambulance_availability', 'ambulance_agent', 'Availability Forecast', 'Forward-looking forecast of ambulance AVAILABILITY -- the minimum number of ambulances that will be free for dispatch at the worst hour over a horizon inferred from the request (3h-3d). Include when the goal asks about dispatch reserve/headroom, guaranteed-available unit count, or how thin the free fleet will get. Distinct from sa_ambulance_fleet_utilization (the % view of the same model) and sa_ambulance_response (response minutes) -- this is the COUNT of free units.', '["Ambulance Availability","Dispatch Reserve","Fleet Headroom"]', false, 30)
ON CONFLICT (id) DO UPDATE SET agent_id=EXCLUDED.agent_id, label=EXCLUDED.label, description=EXCLUDED.description,
  capabilities=EXCLUDED.capabilities, is_prefetch_eligible=EXCLUDED.is_prefetch_eligible, sort_order=EXCLUDED.sort_order, updated_at=now();

INSERT INTO "hospilot_app".task_registry (id, subagent_id, label, outputs, is_active, is_dynamic, sort_order)
VALUES ('ta_forecast_ambulance_availability', 'sa_ambulance_availability', 'Forecast the minimum ambulances free for dispatch at the worst hour over a horizon derived from the goal, with a recommended action, from total/on-mission/available units in the live fleet', '["forecast_available","predicted_minimum_ambulances_available","recommended_action"]', true, false, 10)
ON CONFLICT (id) DO NOTHING;

UPDATE "hospilot_app".task_registry SET label='Forecast the minimum ambulances free for dispatch at the worst hour over a horizon derived from the goal, with a recommended action, from total/on-mission/available units in the live fleet', updated_at=now() WHERE id='ta_forecast_ambulance_availability';
