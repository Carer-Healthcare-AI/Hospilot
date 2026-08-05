-- ─────────────────────────────────────────────────────────────────────────────
-- Hospilot agent tables  —  hospilot schema
-- Run in Hasura SQL console (Data → SQL, check "Track this")
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 1. BEDS ──────────────────────────────────────────────────────────────────
-- Synced fields: id, branch_id, ward, bed_number, room_type, status, is_active
-- Hospilot extras: ventilation, room_sharing, proximity, floor, wing,
--                  natural_light, noise_level, features

CREATE TABLE IF NOT EXISTS hospilot.beds (
    -- CarerOS fields (synced)
    id              UUID PRIMARY KEY,
    branch_id       UUID,
    ward            TEXT,
    bed_number      TEXT,
    room_type       TEXT,
    status          TEXT         NOT NULL DEFAULT 'Available',  -- Available | Occupied
    is_active       BOOLEAN      NOT NULL DEFAULT true,
    synced_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Hospilot enrichment (set once, never overwritten by poller)
    ventilation     TEXT         DEFAULT 'none',    -- none | oxygen | full_ventilator
    room_sharing    TEXT         DEFAULT 'unknown', -- private | shared_2 | shared_4 | bay
    proximity       INTEGER      DEFAULT 3,         -- 1 (closest) → 5 (farthest) to nurse station
    floor           INTEGER,
    wing            TEXT,
    natural_light   BOOLEAN      DEFAULT false,
    noise_level     TEXT         DEFAULT 'moderate', -- quiet | moderate | loud
    features        TEXT[]       DEFAULT '{}'        -- isolation | telemetry | bariatric | negative_pressure
);

COMMENT ON TABLE hospilot.beds IS
    'Bed operational data synced from CarerOS + Hospilot-specific enrichment for AI ranking.';


-- ── 2. DEPARTMENTS ───────────────────────────────────────────────────────────
-- Synced fields: id, name, type
-- Hospilot extras: capacity, target_occupancy_pct

CREATE TABLE IF NOT EXISTS hospilot.departments (
    -- CarerOS fields (synced)
    id              UUID PRIMARY KEY,
    name            TEXT         NOT NULL,
    type            TEXT,                           -- ward | icu | er | ot | pharmacy
    synced_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Hospilot enrichment
    capacity        INTEGER      DEFAULT 0,         -- total beds in this department
    target_occupancy_pct INTEGER DEFAULT 85         -- alert if above this %
);

COMMENT ON TABLE hospilot.departments IS
    'Department reference data + Hospilot capacity metadata.';


-- ── 3. IPD ADMISSIONS ────────────────────────────────────────────────────────
-- Synced fields: id, patient_token, bed_id, department_id,
--                admitted_at, expected_discharge_at, status
-- Hospilot extras: discharge_ready, discharge_blocked_reason

CREATE TABLE IF NOT EXISTS hospilot.ipd_admissions (
    -- CarerOS fields (synced)
    id                      UUID PRIMARY KEY,
    patient_token           TEXT,                    -- anonymised — no PHI
    bed_id                  UUID REFERENCES hospilot.beds(id) ON DELETE SET NULL,
    department_id           UUID REFERENCES hospilot.departments(id) ON DELETE SET NULL,
    admitted_at             TIMESTAMPTZ,
    expected_discharge_at   TIMESTAMPTZ,
    status                  TEXT         DEFAULT 'admitted', -- admitted | discharging | discharged
    synced_at               TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Hospilot enrichment
    discharge_ready         BOOLEAN      DEFAULT false,
    discharge_blocked_reason TEXT                    -- pending_tasks | vitals_unstable | transport | other
);

COMMENT ON TABLE hospilot.ipd_admissions IS
    'In-patient admissions synced from CarerOS + Hospilot discharge readiness flags.';


-- ── 4. VITALS ────────────────────────────────────────────────────────────────
-- One row per patient per reading — agents always use latest per patient_token
-- Synced fields: all clinical readings
-- Hospilot extras: is_critical

CREATE TABLE IF NOT EXISTS hospilot.vitals (
    id                  UUID PRIMARY KEY,
    patient_token       TEXT,
    admission_id        UUID REFERENCES hospilot.ipd_admissions(id) ON DELETE CASCADE,
    recorded_at         TIMESTAMPTZ  NOT NULL,
    temperature         NUMERIC(4,1),               -- °C
    pulse               INTEGER,                    -- bpm
    bp_systolic         INTEGER,                    -- mmHg
    bp_diastolic        INTEGER,                    -- mmHg
    spo2                INTEGER,                    -- %
    respiratory_rate    INTEGER,                    -- breaths/min
    gcs                 INTEGER,                    -- Glasgow Coma Scale 3-15
    synced_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Hospilot enrichment
    is_critical         BOOLEAN      DEFAULT false  -- flagged by Hospilot after Claude analysis
);

CREATE INDEX IF NOT EXISTS idx_vitals_patient_recorded
    ON hospilot.vitals (patient_token, recorded_at DESC);

COMMENT ON TABLE hospilot.vitals IS
    'Vital signs synced from CarerOS. is_critical flagged by Hospilot AI.';


-- ── 5. DISCHARGE SUMMARIES ───────────────────────────────────────────────────
-- Synced as-is from CarerOS discharge_summaries
-- Hospilot adds: ai_generated_note (Claude-written summary)

CREATE TABLE IF NOT EXISTS hospilot.discharge_summaries (
    id                  UUID PRIMARY KEY,
    admission_id        UUID REFERENCES hospilot.ipd_admissions(id) ON DELETE CASCADE,
    summary_text        TEXT,                       -- synced from CarerOS
    created_at          TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Hospilot enrichment
    ai_generated_note   TEXT                        -- Claude-written discharge instructions
);

COMMENT ON TABLE hospilot.discharge_summaries IS
    'Discharge summaries synced from CarerOS + Claude-generated patient instructions.';


-- ── 6. NURSING TASKS ─────────────────────────────────────────────────────────
-- Synced as-is from CarerOS nursing_tasks
-- Used by discharge agent to check if any tasks block discharge

CREATE TABLE IF NOT EXISTS hospilot.nursing_tasks (
    id              UUID PRIMARY KEY,
    admission_id    UUID REFERENCES hospilot.ipd_admissions(id) ON DELETE CASCADE,
    task            TEXT         NOT NULL,
    completed       BOOLEAN      DEFAULT false,
    due_at          TIMESTAMPTZ,
    assigned_to     TEXT,                           -- staff name/id — not a FK (different DB)
    synced_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_nursing_tasks_admission_completed
    ON hospilot.nursing_tasks (admission_id, completed);

COMMENT ON TABLE hospilot.nursing_tasks IS
    'Nursing tasks synced from CarerOS. Incomplete tasks block discharge eligibility.';


-- ── 7. VISITS (ER) ───────────────────────────────────────────────────────────
-- Synced fields: id, patient_token, department_id, arrived_at, status, chief_complaint
-- Hospilot extras: triage_score (Claude assigns this)

CREATE TABLE IF NOT EXISTS hospilot.visits (
    -- CarerOS fields (synced)
    id                  UUID PRIMARY KEY,
    patient_token       TEXT,                       -- anonymised
    department_id       UUID REFERENCES hospilot.departments(id) ON DELETE SET NULL,
    arrived_at          TIMESTAMPTZ,
    status              TEXT         DEFAULT 'waiting', -- waiting | triaged | in_treatment | admitted | discharged
    chief_complaint     TEXT,
    synced_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- Hospilot enrichment
    triage_score        INTEGER                     -- CTAS 1 (resuscitation) → 5 (non-urgent), set by Claude
);

CREATE INDEX IF NOT EXISTS idx_visits_status_arrived
    ON hospilot.visits (status, arrived_at DESC);

COMMENT ON TABLE hospilot.visits IS
    'ER visits synced from CarerOS. triage_score assigned by Hospilot AI (CTAS 1-5).';
