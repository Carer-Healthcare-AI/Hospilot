-- ─────────────────────────────────────────────────────────────────────────────
-- Staffing Agent — generalized staff-area/location roster
-- (planner-query-gaps G11 + G20 + G24 + G28 + lab-staff).
--
-- WHY: staff_agent only knew inpatient nursing wards (built from FHIR admissions ->
-- bed -> ward). Any "check X staffing" for a NON-inpatient area -- front desk (Q2),
-- recovery/PACU (Q4), phlebotomy (Q5), OT + recovery (Q6), lab (Q8) -- found zero
-- and silently failed. One staff-area/location dimension closes all five.
--
-- SCHEMA: hospilot -- staffing rosters are HIS-owned operational data (like
-- appointments/wards), not internal app state. Hasura exposes it as
-- hospilot_staff_roster. Fabric has no roster endpoint yet, so _sync_staff_roster
-- warms Redis from this mirror (same fallback pattern as waitlist). Idempotent.
--
-- MODEL: one row per (area, role, shift). headcount = staff on shift; assigned_load
-- = current units of work (patients/registrations/samples/cases) on that area;
-- load_per_staff = max units one staff can safely cover. capacity = headcount *
-- load_per_staff; understaffed when assigned_load > capacity (or headcount = 0).
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.staff_roster (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  area           VARCHAR(50),     -- front_desk | opd | phlebotomy | ot | recovery | lab | inpatient_nursing
  area_label     TEXT,
  role           VARCHAR(50),     -- front_desk | nurse | phlebotomist | ot_nurse | recovery_nurse | lab_tech
  shift          VARCHAR(20),     -- day | evening | night
  headcount      INTEGER DEFAULT 0,
  assigned_load  INTEGER DEFAULT 0,
  load_per_staff INTEGER DEFAULT 1,
  branch_id      UUID,
  synced_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_staff_roster_area  ON hospilot.staff_roster (area);
CREATE INDEX IF NOT EXISTS idx_staff_roster_shift ON hospilot.staff_roster (shift);

-- Seed a representative roster across areas + shifts. A few areas are deliberately
-- understaffed (front_desk, phlebotomy, recovery, night inpatient) so "check X
-- staffing" surfaces real gaps for Q2/Q4/Q5/Q6/Q8. Runs once on a fresh DB.
INSERT INTO hospilot.staff_roster (area, area_label, role, shift, headcount, assigned_load, load_per_staff)
SELECT * FROM (VALUES
  ('front_desk',        'Front Desk / Reception',  'front_desk',     'day',     2, 38, 15),  -- cap 30 < 38  UNDER
  ('front_desk',        'Front Desk / Reception',  'front_desk',     'evening', 1, 20, 15),  -- cap 15 < 20  UNDER
  ('front_desk',        'Front Desk / Reception',  'front_desk',     'night',   1, 6,  15),
  ('opd',               'OPD Clinics',             'nurse',          'day',     3, 30, 15),
  ('opd',               'OPD Clinics',             'nurse',          'evening', 2, 22, 15),
  ('phlebotomy',        'Phlebotomy / Collection', 'phlebotomist',   'day',     2, 55, 20),  -- cap 40 < 55  UNDER
  ('phlebotomy',        'Phlebotomy / Collection', 'phlebotomist',   'evening', 1, 16, 20),
  ('ot',                'Operating Theatres',      'ot_nurse',       'day',     4, 6,  2),
  ('ot',                'Operating Theatres',      'ot_nurse',       'evening', 2, 5,  2),   -- cap 4 < 5    UNDER
  ('recovery',          'Recovery / PACU',         'recovery_nurse', 'day',     1, 9,  4),   -- cap 4 < 9    UNDER
  ('recovery',          'Recovery / PACU',         'recovery_nurse', 'evening', 1, 3,  4),
  ('lab',               'Laboratory',              'lab_tech',       'day',     5, 70, 20),
  ('lab',               'Laboratory',              'lab_tech',       'evening', 3, 64, 20),  -- cap 60 < 64  UNDER
  ('lab',               'Laboratory',              'lab_tech',       'night',   2, 30, 20),
  ('inpatient_nursing', 'Inpatient Nursing',       'nurse',          'day',     12, 50, 5),
  ('inpatient_nursing', 'Inpatient Nursing',       'nurse',          'evening', 10, 48, 5),
  ('inpatient_nursing', 'Inpatient Nursing',       'nurse',          'night',   8, 44, 5)    -- cap 40 < 44  UNDER
) AS v(area, area_label, role, shift, headcount, assigned_load, load_per_staff)
WHERE NOT EXISTS (SELECT 1 FROM hospilot.staff_roster);
