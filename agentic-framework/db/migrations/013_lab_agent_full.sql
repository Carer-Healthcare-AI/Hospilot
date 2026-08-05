-- Lab Agent: full table set + seed data
-- All tables live in the hospilot schema so Hasura exposes them as
-- hospilot_lab_* types (matching the existing hospilot.lab_orders pattern).

-- ── hospilot.lab_samples ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.lab_samples (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id            UUID,
    patient_token       UUID,
    barcode             VARCHAR(60),
    collection_status   VARCHAR(30)  NOT NULL DEFAULT 'Pending',   -- Pending / Collected
    transport_status    VARCHAR(30)  NOT NULL DEFAULT 'Pending',   -- Pending / In-Transit / Received / Delayed
    lab_receipt_status  VARCHAR(30)  NOT NULL DEFAULT 'Pending',   -- Pending / Received / Missing
    is_misplaced        BOOLEAN      NOT NULL DEFAULT FALSE,
    department          VARCHAR(100),
    collected_at        TIMESTAMPTZ,
    received_at         TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── hospilot.lab_analyzers ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.lab_analyzers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(150) NOT NULL,
    analyzer_type       VARCHAR(80)  NOT NULL,   -- Chemistry / Hematology / Microbiology / etc.
    location            VARCHAR(150),
    status              VARCHAR(30)  NOT NULL DEFAULT 'Online',  -- Online / Offline / Maintenance
    current_load_pct    INTEGER      NOT NULL DEFAULT 0,
    capacity_per_hour   INTEGER      NOT NULL DEFAULT 60,
    is_backup           BOOLEAN      NOT NULL DEFAULT FALSE,
    validated_tests     TEXT[]       NOT NULL DEFAULT '{}',
    last_maintenance_at TIMESTAMPTZ,
    next_maintenance_at TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── hospilot.lab_qc_logs ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.lab_qc_logs (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analyzer_id              UUID,
    shift                    VARCHAR(20)  NOT NULL DEFAULT 'Morning',  -- Morning / Afternoon / Night
    qc_status                VARCHAR(10)  NOT NULL DEFAULT 'Pass',     -- Pass / Fail
    qc_material              VARCHAR(100),
    result_value             NUMERIC(10,3),
    target_value             NUMERIC(10,3),
    deviation_pct            NUMERIC(6,2),
    passed_at                TIMESTAMPTZ,
    failed_at                TIMESTAMPTZ,
    recalibration_triggered  BOOLEAN      NOT NULL DEFAULT FALSE,
    notes                    TEXT,
    synced_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── hospilot.lab_critical_escalations ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.lab_critical_escalations (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    result_id                   UUID,
    order_id                    UUID,
    patient_token               UUID,
    test_name                   VARCHAR(255) NOT NULL,
    result_value                VARCHAR(100),
    flag                        VARCHAR(20)  NOT NULL DEFAULT 'Critical',
    physician_notified          BOOLEAN      NOT NULL DEFAULT FALSE,
    physician_acknowledged_at   TIMESTAMPTZ,
    is_icu_er_patient           BOOLEAN      NOT NULL DEFAULT FALSE,
    escalation_level            VARCHAR(20)  NOT NULL DEFAULT 'Standard',  -- Standard / Urgent
    action_documented           BOOLEAN      NOT NULL DEFAULT FALSE,
    closed_at                   TIMESTAMPTZ,
    synced_at                   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── hospilot.lab_reflex_rules ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.lab_reflex_rules (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger_test                VARCHAR(100) NOT NULL,
    trigger_condition           VARCHAR(50)  NOT NULL DEFAULT 'abnormal',
    trigger_value               VARCHAR(100),
    recommended_test            VARCHAR(100) NOT NULL,
    requires_physician_approval BOOLEAN      NOT NULL DEFAULT TRUE,
    is_active                   BOOLEAN      NOT NULL DEFAULT TRUE,
    department                  VARCHAR(100),
    synced_at                   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── hospilot.lab_validation_rules ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.lab_validation_rules (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    test_code               VARCHAR(50)  NOT NULL UNIQUE,
    test_name               VARCHAR(150) NOT NULL,
    min_normal              NUMERIC(12,4),
    max_normal              NUMERIC(12,4),
    critical_low            NUMERIC(12,4),
    critical_high           NUMERIC(12,4),
    delta_pct               NUMERIC(6,2),
    unit                    VARCHAR(50),
    auto_release_on_normal  BOOLEAN      NOT NULL DEFAULT TRUE,
    synced_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── hospilot.lab_capacity_history ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.lab_capacity_history (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    record_date      DATE         NOT NULL,
    department       VARCHAR(100) NOT NULL DEFAULT 'Main Lab',
    total_orders     INTEGER      NOT NULL DEFAULT 0,
    stat_orders      INTEGER      NOT NULL DEFAULT 0,
    routine_orders   INTEGER      NOT NULL DEFAULT 0,
    peak_hour        INTEGER,
    avg_tat_minutes  INTEGER,
    notes            TEXT,
    synced_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE(record_date, department)
);

-- ── Seed Data ─────────────────────────────────────────────────────────────────
DO $$
DECLARE
    v_p1 UUID; v_p2 UUID; v_p3 UUID;
    v_o1 UUID; v_o2 UUID; v_o3 UUID;
    v_a1 UUID; v_a2 UUID; v_a3 UUID;
    v_r1 UUID; v_r2 UUID;
BEGIN
    SELECT id INTO v_p1 FROM public.patients LIMIT 1 OFFSET 0;
    SELECT id INTO v_p2 FROM public.patients LIMIT 1 OFFSET 1;
    SELECT id INTO v_p3 FROM public.patients LIMIT 1 OFFSET 2;

    -- hospilot.lab_orders already exists (created in migration 005 equivalent in hospilot schema)
    SELECT id INTO v_o1 FROM hospilot.lab_orders LIMIT 1 OFFSET 0;
    SELECT id INTO v_o2 FROM hospilot.lab_orders LIMIT 1 OFFSET 1;
    SELECT id INTO v_o3 FROM hospilot.lab_orders LIMIT 1 OFFSET 2;

    -- Analyzers
    v_a1 := gen_random_uuid();
    v_a2 := gen_random_uuid();
    v_a3 := gen_random_uuid();

    INSERT INTO hospilot.lab_analyzers VALUES
        (v_a1, 'Beckman AU680 Chemistry',       'Chemistry',   'Main Lab - Bay 1',    'Online',
         92, 80, FALSE, ARRAY['Creatinine','BUN','Glucose','Potassium','Sodium','Troponin'],
         NOW() - INTERVAL '30 days', NOW() + INTERVAL '60 days', NOW()),
        (v_a2, 'Sysmex XN-1000 Hematology',     'Hematology',  'Main Lab - Bay 2',    'Online',
         65, 120, FALSE, ARRAY['CBC','WBC Differential','Reticulocyte'],
         NOW() - INTERVAL '15 days', NOW() + INTERVAL '75 days', NOW()),
        (v_a3, 'Beckman AU480 Chemistry Backup', 'Chemistry',   'Backup Lab - Bay 3',  'Online',
         10, 40, TRUE,  ARRAY['Creatinine','BUN','Glucose','Potassium','Sodium'],
         NOW() - INTERVAL '7 days', NOW() + INTERVAL '83 days', NOW())
    ON CONFLICT DO NOTHING;

    -- Samples
    INSERT INTO hospilot.lab_samples
        (order_id, patient_token, barcode, collection_status, transport_status, lab_receipt_status,
         is_misplaced, department, collected_at, received_at)
    VALUES
        (v_o1, v_p1, 'LAB-00100-A', 'Collected', 'In-Transit', 'Pending',  FALSE, 'ICU',
         NOW() - INTERVAL '45 minutes', NULL),
        (v_o1, v_p1, 'LAB-00100-B', 'Collected', 'Received',   'Received', FALSE, 'ICU',
         NOW() - INTERVAL '2 hours',   NOW() - INTERVAL '90 minutes'),
        (v_o2, v_p2, 'LAB-00200-A', 'Pending',   'Pending',    'Pending',  FALSE, 'ER',
         NULL, NULL),
        (v_o2, v_p2, 'LAB-00200-B', 'Collected', 'Delayed',    'Pending',  FALSE, 'ER',
         NOW() - INTERVAL '3 hours', NULL),
        (v_o3, v_p3, 'LAB-00300-A', 'Collected', 'Received',   'Missing',  TRUE,  'Ward 4',
         NOW() - INTERVAL '4 hours', NULL),
        (v_o3, v_p3, 'LAB-00300-B', 'Collected', 'Received',   'Received', FALSE, 'Ward 4',
         NOW() - INTERVAL '90 minutes', NOW() - INTERVAL '60 minutes')
    ON CONFLICT DO NOTHING;

    -- QC logs
    INSERT INTO hospilot.lab_qc_logs
        (analyzer_id, shift, qc_status, qc_material, result_value, target_value, deviation_pct,
         passed_at, failed_at, recalibration_triggered, notes)
    VALUES
        (v_a1, 'Morning',   'Pass', 'Bio-Rad Level 1',  98.4, 100.0,  1.60,
         NOW() - INTERVAL '6 hours', NULL,               FALSE, NULL),
        (v_a1, 'Afternoon', 'Fail', 'Bio-Rad Level 2',  88.2, 100.0, 11.80,
         NULL, NOW() - INTERVAL '2 hours',               TRUE,  'Reagent lot change suspected'),
        (v_a2, 'Morning',   'Pass', 'Sysmex E-CHECK',  101.2, 100.0,  1.20,
         NOW() - INTERVAL '7 hours', NULL,               FALSE, NULL),
        (v_a3, 'Morning',   'Pass', 'Bio-Rad Level 1',  99.1, 100.0,  0.90,
         NOW() - INTERVAL '5 hours', NULL,               FALSE, 'Backup analyzer daily QC')
    ON CONFLICT DO NOTHING;

    -- Critical escalations — pull from hospilot.lab_results
    SELECT id INTO v_r1 FROM hospilot.lab_results WHERE flag = 'Critical' LIMIT 1 OFFSET 0;
    SELECT id INTO v_r2 FROM hospilot.lab_results WHERE flag = 'Critical' LIMIT 1 OFFSET 1;

    INSERT INTO hospilot.lab_critical_escalations
        (result_id, order_id, patient_token, test_name, result_value, flag,
         physician_notified, physician_acknowledged_at, is_icu_er_patient,
         escalation_level, action_documented, closed_at)
    VALUES
        (v_r1, v_o1, v_p1, 'Serum Potassium', '6.8 mEq/L', 'Critical',
         TRUE,  NOW() - INTERVAL '15 minutes', TRUE,  'Urgent',   TRUE,  NULL),
        (v_r2, v_o2, v_p2, 'Creatinine',      '4.1 mg/dL', 'Critical',
         TRUE,  NULL,                          FALSE, 'Standard', FALSE, NULL),
        (NULL,  v_o3, v_p3, 'Troponin I',     '8.5 ng/mL', 'Critical',
         FALSE, NULL,                          TRUE,  'Urgent',   FALSE, NULL)
    ON CONFLICT DO NOTHING;
END $$;

-- Reflex rules (static config — no patient lookups)
INSERT INTO hospilot.lab_reflex_rules
    (trigger_test, trigger_condition, trigger_value, recommended_test, requires_physician_approval, is_active, department)
VALUES
    ('TSH',          'abnormal',  NULL,   'Free T4',                          FALSE, TRUE, 'Endocrinology'),
    ('Creatinine',   'critical',  '>3.0', 'eGFR + Urine Electrolytes',        TRUE,  TRUE, 'Nephrology'),
    ('Hemoglobin',   'low',       '<8.0', 'Reticulocyte Count + Iron Studies', FALSE, TRUE, 'Hematology'),
    ('Blood Culture','abnormal',  NULL,   'Sensitivity Panel',                FALSE, TRUE, 'Microbiology'),
    ('Troponin I',   'critical',  '>2.0', 'Repeat Troponin at 3h + ECG',      FALSE, TRUE, 'Cardiology')
ON CONFLICT DO NOTHING;

-- Validation rules (static config)
INSERT INTO hospilot.lab_validation_rules
    (test_code, test_name, min_normal, max_normal, critical_low, critical_high, delta_pct, unit, auto_release_on_normal)
VALUES
    ('K-001',     'Serum Potassium',        3.5,   5.0,   2.5,   6.5,  30.0, 'mEq/L',  TRUE),
    ('NA-001',    'Serum Sodium',          136.0, 145.0, 120.0, 160.0, 10.0, 'mEq/L',  TRUE),
    ('HGB-001',   'Hemoglobin',             12.0,  17.5,   7.0,  20.0, 20.0, 'g/dL',   TRUE),
    ('CREAT-001', 'Creatinine',              0.7,   1.3,   0.2,   6.0, 50.0, 'mg/dL',  TRUE),
    ('TROP-001',  'Troponin I',              0.0,   0.04,  0.0,   2.0, 50.0, 'ng/mL',  FALSE),
    ('GLU-001',   'Fasting Blood Glucose',  70.0, 100.0,  40.0, 500.0, 25.0, 'mg/dL',  TRUE)
ON CONFLICT (test_code) DO NOTHING;

-- Capacity history: 14 days realistic weekday/weekend variation
INSERT INTO hospilot.lab_capacity_history
    (record_date, department, total_orders, stat_orders, routine_orders, peak_hour, avg_tat_minutes)
VALUES
    (CURRENT_DATE - 14, 'Main Lab', 310, 42, 268, 10, 58),
    (CURRENT_DATE - 13, 'Main Lab', 285, 38, 247,  9, 61),
    (CURRENT_DATE - 12, 'Main Lab', 195, 22, 173, 11, 54),
    (CURRENT_DATE - 11, 'Main Lab', 182, 20, 162, 10, 52),
    (CURRENT_DATE - 10, 'Main Lab', 320, 45, 275, 10, 60),
    (CURRENT_DATE -  9, 'Main Lab', 298, 40, 258,  9, 63),
    (CURRENT_DATE -  8, 'Main Lab', 335, 50, 285, 11, 72),
    (CURRENT_DATE -  7, 'Main Lab', 308, 44, 264, 10, 59),
    (CURRENT_DATE -  6, 'Main Lab', 290, 39, 251,  9, 57),
    (CURRENT_DATE -  5, 'Main Lab', 200, 25, 175, 11, 53),
    (CURRENT_DATE -  4, 'Main Lab', 188, 21, 167, 10, 51),
    (CURRENT_DATE -  3, 'Main Lab', 325, 48, 277, 10, 62),
    (CURRENT_DATE -  2, 'Main Lab', 315, 46, 269,  9, 60),
    (CURRENT_DATE -  1, 'Main Lab', 342, 52, 290, 11, 74)
ON CONFLICT (record_date, department) DO NOTHING;
