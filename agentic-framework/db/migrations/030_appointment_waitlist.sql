-- ─────────────────────────────────────────────────────────────────────────────
-- Appointment Agent — patient waitlist data source (planner-query-gaps G1).
--
-- WHY: Q1 ("match patients on the waitlist") had NO waitlist source -- only
-- appointments + doctor_slots existed, and `status` was free-text with only
-- 'cancelled' ever read. This adds a real waitlist table that
-- ta_appt_match_waitlist (G2) reads and pairs to open slots.
--
-- SCHEMA: hospilot -- this is hospital operational data that in the real world is
-- owned/updated by the external HIS, exactly like hospilot.appointments /
-- hospilot.doctor_slots (NOT app-internal like hospilot_app sessions/registry).
-- FLAT / denormalised (name/contact/specialty inline) so the matcher needs no joins.
-- Hasura exposes it as hospilot_waitlist. Idempotent.
--
-- FETCH PATH (see note below): the canonical external-data path is Fabric change
-- API -> Redis. Fabric does not yet expose a /waitlist endpoint, so today the
-- agent reads the hospilot mirror directly via Hasura (hospilot_waitlist) and
-- warms Redis from it -- the same Hasura-fallback pattern hospilot.appointments
-- already uses. Add a Fabric /appointments/waitlist endpoint later to make the
-- sync flow Fabric -> Redis like appointments/slots.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.waitlist (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  patient_id      UUID,
  patient_name    TEXT,
  phone           TEXT,
  email           TEXT,
  specialization  TEXT,                               -- requested specialty (matches doctor_slots.specialization)
  priority        VARCHAR(20)  DEFAULT 'medium',       -- 'high' | 'medium' | 'low'
  requested_date  DATE,                                -- earliest acceptable / preferred date
  status          VARCHAR(50)  DEFAULT 'waitlisted',   -- waitlisted | matched | booked | removed
  reason          TEXT,
  created_at      TIMESTAMPTZ  DEFAULT now(),
  synced_at       TIMESTAMPTZ  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_waitlist_status   ON hospilot.waitlist (status);
CREATE INDEX IF NOT EXISTS idx_waitlist_spec      ON hospilot.waitlist (specialization);
CREATE INDEX IF NOT EXISTS idx_waitlist_priority  ON hospilot.waitlist (priority);

-- Seed a spread of waitlisted patients (incl. Cardiology for Q1) from existing
-- appointment patients so patient_id / name / contact are valid. Deterministic,
-- runs once on a fresh DB. Skips entirely if rows already exist.
INSERT INTO hospilot.waitlist
  (patient_id, patient_name, phone, email, specialization, priority, requested_date, status, reason)
SELECT a.patient_id, a.patient_name, a.phone, a.email,
       CASE a.rn % 3 WHEN 0 THEN 'Cardiology' WHEN 1 THEN 'Orthopedics' ELSE 'General Medicine' END,
       CASE a.rn % 3 WHEN 0 THEN 'high'        WHEN 1 THEN 'medium'      ELSE 'low' END,
       CURRENT_DATE,
       'waitlisted',
       'Seeded waitlist entry pending slot availability'
FROM (
  SELECT patient_id, patient_name, phone, email,
         row_number() OVER (ORDER BY appointment_time) AS rn
  FROM (
    SELECT DISTINCT patient_id, patient_name, phone, email, appointment_time
    FROM hospilot.appointments
    WHERE patient_id IS NOT NULL
  ) d
  LIMIT 24
) a
WHERE NOT EXISTS (SELECT 1 FROM hospilot.waitlist);
