-- Ask Hospilot — portable SQLite schema
-- Covers beds, patients, visits, admissions, and staff rosters.
--
-- Schema adaptations (vs a fully normalized HIS dump):
-- 1. beds.ward_type denormalizes ICU/General/etc. onto the bed row so
--    "free ICU beds" is a single-table filter (fewer join mistakes).
-- 2. staff_roster stores required_headcount + actual_headcount so
--    short-staffing is a direct comparison, not a multi-table join.
-- 3. Views expose common operational questions as stable shapes for the LLM.

PRAGMA foreign_keys = ON;

CREATE TABLE wards (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    ward_type   TEXT NOT NULL CHECK (ward_type IN ('ICU', 'General', 'Maternity', 'Pediatric', 'Emergency')),
    floor       INTEGER NOT NULL,
    total_beds  INTEGER NOT NULL
);

CREATE TABLE beds (
    id          INTEGER PRIMARY KEY,
    ward_id     INTEGER NOT NULL REFERENCES wards(id),
    bed_number  TEXT NOT NULL,
    -- Denormalized from wards for reliable text-to-SQL on ICU/type questions
    ward_type   TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('free', 'occupied', 'cleaning', 'maintenance')),
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    UNIQUE (ward_id, bed_number)
);

CREATE TABLE patients (
    id          INTEGER PRIMARY KEY,
    uhid        TEXT NOT NULL UNIQUE,
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    date_of_birth DATE,
    sex         TEXT CHECK (sex IN ('M', 'F', 'O'))
);

CREATE TABLE visits (
    id              INTEGER PRIMARY KEY,
    patient_id      INTEGER NOT NULL REFERENCES patients(id),
    ward_id         INTEGER REFERENCES wards(id),
    visit_type      TEXT NOT NULL CHECK (visit_type IN ('OPD', 'Emergency', 'IPD')),
    chief_complaint TEXT,
    status          TEXT NOT NULL CHECK (status IN ('waiting', 'in_progress', 'completed', 'left')),
    arrived_at      TEXT NOT NULL,  -- ISO-8601
    completed_at    TEXT
);

CREATE TABLE admissions (
    id                      INTEGER PRIMARY KEY,
    patient_id              INTEGER NOT NULL REFERENCES patients(id),
    bed_id                  INTEGER NOT NULL REFERENCES beds(id),
    ward_id                 INTEGER NOT NULL REFERENCES wards(id),
    admitted_at             TEXT NOT NULL,
    expected_discharge_at   TEXT,
    discharged_at           TEXT,
    status                  TEXT NOT NULL CHECK (status IN ('admitted', 'discharged', 'transferred')),
    diagnosis               TEXT
);

CREATE TABLE staff_roster (
    id                  INTEGER PRIMARY KEY,
    ward_id             INTEGER NOT NULL REFERENCES wards(id),
    role                TEXT NOT NULL CHECK (role IN ('nurse', 'doctor', 'technician', 'aide')),
    shift               TEXT NOT NULL CHECK (shift IN ('morning', 'evening', 'night')),
    roster_date         TEXT NOT NULL,  -- YYYY-MM-DD
    required_headcount  INTEGER NOT NULL,
    actual_headcount    INTEGER NOT NULL,
    UNIQUE (ward_id, role, shift, roster_date)
);

-- Free / available beds by ward (active beds only)
CREATE VIEW v_bed_availability AS
SELECT
    w.id AS ward_id,
    w.code AS ward_code,
    w.name AS ward_name,
    w.ward_type,
    b.status,
    COUNT(*) AS bed_count
FROM beds b
JOIN wards w ON w.id = b.ward_id
WHERE b.is_active = 1
GROUP BY w.id, w.code, w.name, w.ward_type, b.status;

-- Staffing gaps: actual < required
CREATE VIEW v_staffing_gaps AS
SELECT
    w.id AS ward_id,
    w.code AS ward_code,
    w.name AS ward_name,
    w.ward_type,
    r.role,
    r.shift,
    r.roster_date,
    r.required_headcount,
    r.actual_headcount,
    (r.required_headcount - r.actual_headcount) AS shortfall
FROM staff_roster r
JOIN wards w ON w.id = r.ward_id
WHERE r.actual_headcount < r.required_headcount;

-- Current inpatients with bed location
CREATE VIEW v_current_inpatients AS
SELECT
    a.id AS admission_id,
    p.uhid,
    p.first_name,
    p.last_name,
    w.code AS ward_code,
    w.name AS ward_name,
    w.ward_type,
    b.bed_number,
    a.admitted_at,
    a.expected_discharge_at,
    a.diagnosis
FROM admissions a
JOIN patients p ON p.id = a.patient_id
JOIN beds b ON b.id = a.bed_id
JOIN wards w ON w.id = a.ward_id
WHERE a.status = 'admitted';
