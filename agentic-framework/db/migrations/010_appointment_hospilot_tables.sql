-- ─────────────────────────────────────────────────────────────────────────────
-- Appointment Agent — hospilot-owned data tables (remove public dependency).
-- Agents must read from the hospilot schema, not public. These are FLAT
-- denormalised copies (patient name/contact, specialty, department inlined) so
-- no Hasura relationships are needed and reminders have phone/email.
--   • ids are preserved from public so hospilot.visits.appointment_id links resolve.
--   • the agent OWNS hospilot.appointments (reads + writes bookings here).
-- Idempotent: ON CONFLICT (id) DO NOTHING.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.appointments (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  patient_id       UUID,
  provider_id      UUID,
  department_id    UUID,
  appointment_time TIMESTAMP,
  status           VARCHAR(50),
  type             VARCHAR(100),
  patient_name     TEXT,
  phone            TEXT,
  email            TEXT,
  specialization   TEXT,
  department_name  TEXT,
  synced_at        TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.doctor_slots (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider_id    UUID,
  slot_date      DATE,
  slot_start     TIME,
  slot_end       TIME,
  slot_type      VARCHAR(50),
  status         VARCHAR(50),
  max_patients   INTEGER,
  booked_count   INTEGER,
  specialization TEXT,
  synced_at      TIMESTAMPTZ DEFAULT now()
);

-- Seed appointments (denormalised) from public, preserving ids
INSERT INTO hospilot.appointments
  (id, patient_id, provider_id, department_id, appointment_time, status, type,
   patient_name, phone, email, specialization, department_name)
SELECT a.id, a.patient_id, a.provider_id, a.department_id, a.appointment_time, a.status, a.type,
       NULLIF(TRIM(COALESCE(p.first_name,'') || ' ' || COALESCE(p.last_name,'')), ''),
       p.phone, p.email, pr.specialization, d.name
FROM public.appointments a
LEFT JOIN public.patients   p  ON p.id  = a.patient_id
LEFT JOIN public.providers  pr ON pr.id = a.provider_id
LEFT JOIN public.departments d ON d.id  = a.department_id
ON CONFLICT (id) DO NOTHING;

-- Seed doctor slots (denormalised specialty) from public, preserving ids
INSERT INTO hospilot.doctor_slots
  (id, provider_id, slot_date, slot_start, slot_end, slot_type, status,
   max_patients, booked_count, specialization)
SELECT s.id, s.provider_id, s.slot_date, s.slot_start, s.slot_end, s.slot_type, s.status,
       s.max_patients, s.booked_count, pr.specialization
FROM public.doctor_slots s
LEFT JOIN public.providers pr ON pr.id = s.provider_id
ON CONFLICT (id) DO NOTHING;
