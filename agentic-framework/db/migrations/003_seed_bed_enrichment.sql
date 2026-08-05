-- Seed reasonable enrichment defaults for hospilot.beds
-- Run AFTER 001_agent_tables.sql and after the poller has synced beds from CarerOS.
-- Adjust ward name patterns to match your CarerOS data.

-- ICU beds — ventilated, private, closest to nurse station
UPDATE hospilot.beds
SET
    ventilation  = 'full_ventilator',
    room_sharing = 'private',
    proximity    = 1,
    noise_level  = 'moderate',
    features     = '{telemetry}'
WHERE ward ILIKE '%ICU%' OR ward ILIKE '%intensive%';

-- HDU / High Dependency
UPDATE hospilot.beds
SET
    ventilation  = 'oxygen',
    room_sharing = 'private',
    proximity    = 1,
    noise_level  = 'moderate',
    features     = '{telemetry}'
WHERE ward ILIKE '%HDU%' OR ward ILIKE '%high depend%';

-- Private rooms (general ward)
UPDATE hospilot.beds
SET
    ventilation  = 'none',
    room_sharing = 'private',
    proximity    = 3,
    noise_level  = 'quiet',
    natural_light = true
WHERE room_type ILIKE '%private%' AND ventilation = 'none';

-- Shared bays / general ward
UPDATE hospilot.beds
SET
    ventilation  = 'none',
    room_sharing = 'shared_4',
    proximity    = 3,
    noise_level  = 'moderate'
WHERE room_type ILIKE '%shared%' OR room_type ILIKE '%bay%' OR room_type ILIKE '%general%';

-- Isolation rooms
UPDATE hospilot.beds
SET
    room_sharing = 'private',
    features     = features || '{isolation}',
    noise_level  = 'quiet'
WHERE room_type ILIKE '%isolation%' OR room_type ILIKE '%negative%';
