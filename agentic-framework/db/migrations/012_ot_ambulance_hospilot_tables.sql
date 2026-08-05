-- =====================================================
-- OT + Ambulance: copy public schema tables into hospilot
-- All 6 tables become hospilot-owned; APIs query hospilot_* root fields.
-- =====================================================

-- ── ot_rooms ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.ot_rooms (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id           UUID,
    room_code        VARCHAR(50)  NOT NULL,
    room_name        VARCHAR(100) NOT NULL,
    room_type        VARCHAR(100),
    floor            VARCHAR(50),
    status           VARCHAR(50)  DEFAULT 'Available',
    equipment_details TEXT,
    capacity         INTEGER      DEFAULT 1,
    is_active        BOOLEAN      DEFAULT TRUE,
    branch_id        UUID,
    created_by       UUID,
    created_at       TIMESTAMPTZ  DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  DEFAULT NOW(),
    synced_at        TIMESTAMPTZ  DEFAULT NOW()
);

INSERT INTO hospilot.ot_rooms (
    id, org_id, room_code, room_name, room_type, floor, status,
    equipment_details, capacity, is_active, branch_id, created_by,
    created_at, updated_at, synced_at
)
SELECT
    id, org_id, room_code, room_name, room_type, floor, status,
    equipment_details, capacity, is_active, branch_id, created_by,
    created_at, updated_at, NOW()
FROM public.ot_rooms
ON CONFLICT (id) DO NOTHING;

-- ── ot_room_status ────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.ot_room_status (
    id                   UUID PRIMARY KEY,
    org_id               UUID,
    room_code            VARCHAR(50),
    room_name            VARCHAR(100),
    room_type            VARCHAR(100),
    status               VARCHAR(50),
    is_active            BOOLEAN,
    current_surgery_id   UUID,
    current_surgery_code VARCHAR(50),
    current_surgery_name VARCHAR(200),
    surgery_status       VARCHAR(50),
    patient_name         TEXT,
    scheduled_start_time TIME,
    scheduled_end_time   TIME,
    synced_at            TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO hospilot.ot_room_status (
    id, org_id, room_code, room_name, room_type, status, is_active,
    current_surgery_id, current_surgery_code, current_surgery_name,
    surgery_status, patient_name, scheduled_start_time, scheduled_end_time,
    synced_at
)
SELECT
    id, org_id, room_code, room_name, room_type, status, is_active,
    current_surgery_id, current_surgery_code, current_surgery_name,
    surgery_status, patient_name, scheduled_start_time, scheduled_end_time,
    NOW()
FROM public.ot_room_status
ON CONFLICT (id) DO NOTHING;

-- ── ot_surgery_schedule ───────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.ot_surgery_schedule (
    id                         UUID PRIMARY KEY,
    org_id                     UUID,
    surgery_code               VARCHAR(50),
    surgery_name               VARCHAR(200),
    surgery_type               VARCHAR(100),
    scheduled_date             DATE,
    scheduled_start_time       TIME,
    scheduled_end_time         TIME,
    estimated_duration_minutes INTEGER,
    status                     VARCHAR(50),
    priority                   VARCHAR(50),
    patient_id                 UUID,
    patient_name               TEXT,
    patient_phone              VARCHAR(50),
    room_code                  VARCHAR(50),
    room_name                  VARCHAR(100),
    room_type                  VARCHAR(100),
    surgeon_id                 UUID,
    surgeon_name               TEXT,
    surgeon_designation        VARCHAR(100),
    assistant_surgeon_id       UUID,
    assistant_surgeon_name     TEXT,
    anesthetist_id             UUID,
    anesthetist_name           TEXT,
    procedure_name             VARCHAR(200),
    procedure_cost             NUMERIC,
    created_at                 TIMESTAMPTZ DEFAULT NOW(),
    updated_at                 TIMESTAMPTZ DEFAULT NOW(),
    synced_at                  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO hospilot.ot_surgery_schedule (
    id, org_id, surgery_code, surgery_name, surgery_type,
    scheduled_date, scheduled_start_time, scheduled_end_time,
    estimated_duration_minutes, status, priority,
    patient_id, patient_name, patient_phone,
    room_code, room_name, room_type,
    surgeon_id, surgeon_name, surgeon_designation,
    assistant_surgeon_id, assistant_surgeon_name,
    anesthetist_id, anesthetist_name,
    procedure_name, procedure_cost,
    created_at, updated_at, synced_at
)
SELECT
    id, org_id, surgery_code, surgery_name, surgery_type,
    scheduled_date, scheduled_start_time, scheduled_end_time,
    estimated_duration_minutes, status, priority,
    patient_id, patient_name, patient_phone,
    room_code, room_name, room_type,
    surgeon_id, surgeon_name, surgeon_designation,
    assistant_surgeon_id, assistant_surgeon_name,
    anesthetist_id, anesthetist_name,
    procedure_name, procedure_cost,
    created_at, updated_at, NOW()
FROM public.ot_surgery_schedule
ON CONFLICT (id) DO NOTHING;

-- ── ot_equipment_usage ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.ot_equipment_usage (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id         UUID,
    surgery_id     UUID NOT NULL,
    equipment_name VARCHAR(200) NOT NULL,
    quantity       INTEGER      DEFAULT 1,
    notes          TEXT,
    created_at     TIMESTAMPTZ  DEFAULT NOW(),
    synced_at      TIMESTAMPTZ  DEFAULT NOW()
);

INSERT INTO hospilot.ot_equipment_usage (
    id, org_id, surgery_id, equipment_name, quantity, notes, created_at, synced_at
)
SELECT
    id, org_id, surgery_id, equipment_name, quantity, notes, created_at, NOW()
FROM public.ot_equipment_usage
ON CONFLICT (id) DO NOTHING;

-- ── ot_surgeries (extend existing slim table + backfill) ─────────────────────

ALTER TABLE hospilot.ot_surgeries
    ADD COLUMN IF NOT EXISTS org_id                     UUID,
    ADD COLUMN IF NOT EXISTS surgery_code               VARCHAR(50),
    ADD COLUMN IF NOT EXISTS patient_id                 UUID,
    ADD COLUMN IF NOT EXISTS visit_id                   UUID,
    ADD COLUMN IF NOT EXISTS procedure_id               UUID,
    ADD COLUMN IF NOT EXISTS surgery_name               VARCHAR(200),
    ADD COLUMN IF NOT EXISTS surgery_type               VARCHAR(100),
    ADD COLUMN IF NOT EXISTS ot_room_id                 UUID,
    ADD COLUMN IF NOT EXISTS scheduled_date             DATE,
    ADD COLUMN IF NOT EXISTS scheduled_start_time       TIME,
    ADD COLUMN IF NOT EXISTS scheduled_end_time         TIME,
    ADD COLUMN IF NOT EXISTS estimated_duration_minutes INTEGER,
    ADD COLUMN IF NOT EXISTS actual_start_time          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS actual_end_time            TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS primary_surgeon_id         UUID,
    ADD COLUMN IF NOT EXISTS assistant_surgeon_id       UUID,
    ADD COLUMN IF NOT EXISTS anesthetist_id             UUID,
    ADD COLUMN IF NOT EXISTS nursing_staff_ids          UUID[],
    ADD COLUMN IF NOT EXISTS priority                   VARCHAR(50),
    ADD COLUMN IF NOT EXISTS pre_op_notes               TEXT,
    ADD COLUMN IF NOT EXISTS post_op_notes              TEXT,
    ADD COLUMN IF NOT EXISTS complications              TEXT,
    ADD COLUMN IF NOT EXISTS cancellation_reason        TEXT,
    ADD COLUMN IF NOT EXISTS updated_at                 TIMESTAMPTZ DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS branch_id                  UUID;

INSERT INTO hospilot.ot_surgeries (
    id, org_id, surgery_code, patient_id, patient_token,
    visit_id, admission_id, procedure_id,
    surgery_name, surgery_type, ot_room_id,
    scheduled_date, scheduled_start_time, scheduled_end_time,
    estimated_duration_minutes, actual_start_time, actual_end_time,
    primary_surgeon_id, assistant_surgeon_id, anesthetist_id,
    nursing_staff_ids, status, priority,
    pre_op_notes, post_op_notes, complications, cancellation_reason,
    branch_id, created_at, updated_at, synced_at
)
SELECT
    id, org_id, surgery_code, patient_id, patient_id::text,
    visit_id, admission_id, procedure_id,
    surgery_name, surgery_type, ot_room_id,
    scheduled_date, scheduled_start_time, scheduled_end_time,
    estimated_duration_minutes, actual_start_time, actual_end_time,
    primary_surgeon_id, assistant_surgeon_id, anesthetist_id,
    nursing_staff_ids, status, priority,
    pre_op_notes, post_op_notes, complications, cancellation_reason,
    branch_id, created_at, updated_at, NOW()
FROM public.ot_surgeries
ON CONFLICT (id) DO UPDATE SET
    org_id = EXCLUDED.org_id,
    surgery_code = EXCLUDED.surgery_code,
    patient_id = EXCLUDED.patient_id,
    surgery_name = EXCLUDED.surgery_name,
    surgery_type = EXCLUDED.surgery_type,
    ot_room_id = EXCLUDED.ot_room_id,
    scheduled_date = EXCLUDED.scheduled_date,
    status = EXCLUDED.status,
    priority = EXCLUDED.priority,
    synced_at = NOW();

-- ── emergency_ambulances ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.emergency_ambulances (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id           UUID NOT NULL,
    branch_id        UUID NOT NULL,
    vehicle_no       TEXT NOT NULL UNIQUE,
    vehicle_type     TEXT NOT NULL DEFAULT 'BLS',
    status           TEXT NOT NULL DEFAULT 'Available',
    priority         TEXT NOT NULL DEFAULT 'Low',
    driver_name      TEXT NOT NULL,
    paramedic_name   TEXT,
    current_location TEXT NOT NULL,
    fuel_level       INTEGER DEFAULT 100,
    destination      TEXT,
    emergency_type   TEXT,
    dispatched_at    TIMESTAMPTZ,
    eta_mins         INTEGER,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    synced_at        TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO hospilot.emergency_ambulances (
    id, org_id, branch_id, vehicle_no, vehicle_type, status, priority,
    driver_name, paramedic_name, current_location, fuel_level,
    destination, emergency_type, dispatched_at, eta_mins,
    created_at, synced_at
)
SELECT
    id, org_id, branch_id, vehicle_no, vehicle_type, status, priority,
    driver_name, paramedic_name, current_location, fuel_level,
    destination, emergency_type, dispatched_at, eta_mins,
    created_at, NOW()
FROM public.emergency_ambulances
ON CONFLICT (id) DO NOTHING;
