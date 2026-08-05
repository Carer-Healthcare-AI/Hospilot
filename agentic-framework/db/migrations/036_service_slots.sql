-- ─────────────────────────────────────────────────────────────────────────────
-- Appointment Agent — non-OPD bookable slot types (planner-query-gaps G23 + G39).
--
-- WHY: appointments only modelled OPD doctor slots (hospilot.doctor_slots). Q5
-- ("book the sample collection appointments") and Q11 ("notify patients of pickup
-- appointment windows") had nothing to schedule against -- bookings fell back to
-- arbitrary OPD doctor slots. This adds a dedicated non-OPD slot model for
-- sample_collection (phlebotomy) and pharmacy_pickup windows.
--
-- SCHEMA: hospilot -- HIS-owned operational data like doctor_slots (Fabric has no
-- endpoint for these yet, so _sync_service_slots warms Redis from the Hasura mirror,
-- same fallback as waitlist/staff_roster). Hasura exposes it as hospilot_service_slots.
-- Idempotent.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.service_slots (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slot_type      VARCHAR(50),     -- sample_collection | pharmacy_pickup
  slot_date      DATE,
  slot_start     TIME,
  slot_end       TIME,
  location       TEXT,
  specialization TEXT,            -- optional (e.g. lab discipline); usually NULL
  max_patients   INTEGER DEFAULT 1,
  booked_count   INTEGER DEFAULT 0,
  status         VARCHAR(50) DEFAULT 'open',
  synced_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_service_slots_type   ON hospilot.service_slots (slot_type);
CREATE INDEX IF NOT EXISTS idx_service_slots_date    ON hospilot.service_slots (slot_date);
CREATE INDEX IF NOT EXISTS idx_service_slots_status  ON hospilot.service_slots (status);

-- Seed sample-collection (phlebotomy) + pharmacy-pickup windows over the next 3 days.
-- Runs once on a fresh DB.
INSERT INTO hospilot.service_slots (slot_type, slot_date, slot_start, slot_end, location, max_patients, booked_count, status)
SELECT v.slot_type, (CURRENT_DATE + v.day_offset)::date, v.slot_start::time, v.slot_end::time,
       v.location, v.max_patients, 0, 'open'
FROM (VALUES
  ('sample_collection', 0, '07:00', '07:30', 'Phlebotomy Room 1', 4),
  ('sample_collection', 0, '07:30', '08:00', 'Phlebotomy Room 1', 4),
  ('sample_collection', 0, '08:00', '08:30', 'Phlebotomy Room 2', 4),
  ('sample_collection', 1, '07:00', '07:30', 'Phlebotomy Room 1', 4),
  ('sample_collection', 1, '08:00', '08:30', 'Phlebotomy Room 2', 4),
  ('sample_collection', 2, '07:30', '08:00', 'Phlebotomy Room 1', 4),
  ('pharmacy_pickup',   0, '10:00', '10:30', 'Pharmacy Counter A', 6),
  ('pharmacy_pickup',   0, '14:00', '14:30', 'Pharmacy Counter A', 6),
  ('pharmacy_pickup',   0, '16:00', '16:30', 'Pharmacy Counter B', 6),
  ('pharmacy_pickup',   1, '10:00', '10:30', 'Pharmacy Counter A', 6),
  ('pharmacy_pickup',   1, '14:00', '14:30', 'Pharmacy Counter B', 6),
  ('pharmacy_pickup',   2, '11:00', '11:30', 'Pharmacy Counter A', 6)
) AS v(slot_type, day_offset, slot_start, slot_end, location, max_patients)
WHERE NOT EXISTS (SELECT 1 FROM hospilot.service_slots);
