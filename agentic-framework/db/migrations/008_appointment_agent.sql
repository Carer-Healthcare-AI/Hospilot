-- ─────────────────────────────────────────────────────────────────────────────
-- Appointment Agent  —  OPD visit typing + appointment seed
-- Run in Hasura SQL console (Data → SQL) then reload metadata.
--
-- Design (see kafka-a2a-flow notes / discussion):
--   • hospilot.visits gains visit_type + appointment_id.
--   • Existing 57 rows keep visit_type = NULL  → still treated as ER (unchanged).
--   • New mock OPD rows are tagged visit_type = 'OPD'  → EXCLUDED from the ER
--     agent (see hasura.get_untriaged_visits / get_active_er_visits filters)
--     and read ONLY by the Appointment Agent.
--   • Appointment records live in CarerOS public.appointments, linked to the
--     OPD hospilot.visits rows via appointment_id.
--   • Mock OPD rows use synthetic ids absent from CarerOS public.visits, so the
--     30s poller (upsert by id, never deletes) never touches them.
-- Idempotent: the seed only runs when no OPD rows exist yet.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 1. Schema: type + appointment link on the ER mirror ──────────────────────
ALTER TABLE hospilot.visits ADD COLUMN IF NOT EXISTS visit_type     TEXT;
ALTER TABLE hospilot.visits ADD COLUMN IF NOT EXISTS appointment_id UUID;

COMMENT ON COLUMN hospilot.visits.visit_type IS
  'NULL/ER = ER agent queue (unchanged legacy rows). ''OPD'' = Appointment Agent only, excluded from ER.';

-- ── 2. Seed: appointments (CarerOS) + linked OPD visits (hospilot mirror) ─────
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM hospilot.visits WHERE visit_type = 'OPD') THEN

    -- 2a. Main spread: 24 appointments across Scheduled / Completed / Cancelled / No Show.
    --     Scheduled → upcoming (next ~2 days, gives Reminder + No-Show targets);
    --     others → past (gives No-Show history + cancellation slots).
    WITH src AS (
      SELECT patient_id, provider_id, department_id,
             row_number() OVER (ORDER BY id) AS rn
      FROM public.visits
      WHERE visit_type = 'OPD'
        AND patient_id IS NOT NULL
        AND provider_id IS NOT NULL
        AND department_id IS NOT NULL
      LIMIT 24
    ),
    labeled AS (
      SELECT src.*,
        (ARRAY['Scheduled','Completed','Cancelled','No Show'])[1 + (rn % 4)]                          AS appt_status,
        (ARRAY['New Consultation','Follow-up','Lab Review','Specialist Consultation'])[1 + (rn % 4)]  AS appt_type
      FROM src
    ),
    timed AS (
      SELECT labeled.*,
        CASE appt_status
          WHEN 'Scheduled' THEN now() + ((rn % 6) + 1) * interval '8 hour'
          WHEN 'Completed' THEN now() - ((rn % 10) + 1) * interval '3 day'
          WHEN 'Cancelled' THEN now() - ((rn % 5) + 1) * interval '2 day'
          ELSE                  now() - ((rn % 7) + 1) * interval '4 day'
        END AS appt_time
      FROM labeled
    ),
    new_appts AS (
      INSERT INTO public.appointments
        (patient_id, provider_id, department_id, appointment_time, status, type)
      SELECT patient_id, provider_id, department_id, appt_time, appt_status, appt_type
      FROM timed
      RETURNING id, patient_id, appointment_time, status
    )
    INSERT INTO hospilot.visits
      (id, patient_token, department_id, arrived_at, status, chief_complaint, visit_type, appointment_id)
    SELECT gen_random_uuid(), patient_id::text, NULL, appointment_time,
           lower(status), 'OPD appointment', 'OPD', id
    FROM new_appts;

    -- 2b. Chronic no-show: one patient with 3 prior No Shows (drives the
    --     "Chronic no-show management" scenario).
    WITH p AS (
      SELECT patient_id, provider_id, department_id
      FROM public.visits
      WHERE visit_type = 'OPD'
        AND patient_id IS NOT NULL AND provider_id IS NOT NULL AND department_id IS NOT NULL
      LIMIT 1
    ),
    chronic AS (
      INSERT INTO public.appointments
        (patient_id, provider_id, department_id, appointment_time, status, type)
      SELECT p.patient_id, p.provider_id, p.department_id,
             now() - g * interval '6 day', 'No Show', 'Follow-up'
      FROM p, generate_series(1, 3) AS g
      RETURNING id, patient_id, appointment_time
    )
    INSERT INTO hospilot.visits
      (id, patient_token, department_id, arrived_at, status, chief_complaint, visit_type, appointment_id)
    SELECT gen_random_uuid(), patient_id::text, NULL, appointment_time,
           'no show', 'OPD appointment', 'OPD', id
    FROM chronic;

  END IF;
END $$;
