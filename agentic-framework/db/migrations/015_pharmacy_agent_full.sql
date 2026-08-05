-- Pharmacy Agent: full table set + seed data
-- All tables live in the hospilot schema.

-- ── hospilot.pharmacy_orders ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.pharmacy_orders (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_token       UUID,
    order_id            UUID,
    medication_name     VARCHAR(200) NOT NULL,
    generic_name        VARCHAR(200),
    dosage              VARCHAR(100),
    route               VARCHAR(50)  NOT NULL DEFAULT 'Oral',   -- Oral / IV / IM / SC / Topical
    frequency           VARCHAR(80),
    order_type          VARCHAR(20)  NOT NULL DEFAULT 'routine', -- STAT / urgent / routine
    status              VARCHAR(20)  NOT NULL DEFAULT 'pending', -- pending / dispensing / dispensed / cancelled / on_hold
    department          VARCHAR(100),
    is_controlled       BOOLEAN      NOT NULL DEFAULT FALSE,
    prescribed_by       VARCHAR(150),
    prescribed_at       TIMESTAMPTZ,
    dispensed_at        TIMESTAMPTZ,
    notes               TEXT,
    synced_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── hospilot.pharmacy_inventory ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.pharmacy_inventory (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    medication_name     VARCHAR(200) NOT NULL,
    generic_name        VARCHAR(200),
    brand_name          VARCHAR(200),
    category            VARCHAR(80)  NOT NULL DEFAULT 'General', -- Antibiotic / Controlled / Chemotherapy / General / Cardiac
    stock_quantity      INTEGER      NOT NULL DEFAULT 0,
    reorder_level       INTEGER      NOT NULL DEFAULT 10,
    unit                VARCHAR(30)  NOT NULL DEFAULT 'tablets',
    location            VARCHAR(50)  NOT NULL DEFAULT 'main',   -- main / satellite / icu_cart
    is_controlled       BOOLEAN      NOT NULL DEFAULT FALSE,
    controlled_class    VARCHAR(10),                             -- II / III / IV / V
    expiry_date         DATE,
    last_restocked_at   TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── hospilot.pharmacy_dispensing_log ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.pharmacy_dispensing_log (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id                UUID,
    patient_token           UUID,
    medication_name         VARCHAR(200) NOT NULL,
    quantity_dispensed      INTEGER      NOT NULL DEFAULT 1,
    dispensed_by            VARCHAR(150),
    dispensed_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    verification_status     VARCHAR(20)  NOT NULL DEFAULT 'pending', -- pending / verified / discrepancy
    patient_verified        BOOLEAN      NOT NULL DEFAULT FALSE,
    prescription_matched    BOOLEAN      NOT NULL DEFAULT FALSE,
    dosage_correct          BOOLEAN      NOT NULL DEFAULT TRUE,
    tat_minutes             INTEGER,
    is_stat                 BOOLEAN      NOT NULL DEFAULT FALSE,
    notes                   TEXT,
    synced_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── hospilot.pharmacy_interaction_rules ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.pharmacy_interaction_rules (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    drug_a              VARCHAR(200) NOT NULL,
    drug_b              VARCHAR(200) NOT NULL,
    severity            VARCHAR(20)  NOT NULL DEFAULT 'moderate', -- major / moderate / minor
    interaction_desc    TEXT         NOT NULL,
    action_required     VARCHAR(20)  NOT NULL DEFAULT 'alert',   -- alert / hold / escalate
    is_active           BOOLEAN      NOT NULL DEFAULT TRUE,
    synced_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── hospilot.pharmacy_substitution_rules ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.pharmacy_substitution_rules (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    original_drug               VARCHAR(200) NOT NULL,
    substitute_drug             VARCHAR(200) NOT NULL,
    therapeutic_class           VARCHAR(100),
    requires_physician_approval BOOLEAN      NOT NULL DEFAULT TRUE,
    is_active                   BOOLEAN      NOT NULL DEFAULT TRUE,
    priority_order              INTEGER      NOT NULL DEFAULT 1,
    synced_at                   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── hospilot.pharmacy_controlled_log ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.pharmacy_controlled_log (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id                UUID,
    patient_token           UUID,
    medication_name         VARCHAR(200) NOT NULL,
    controlled_class        VARCHAR(10)  NOT NULL DEFAULT 'II',
    dispensed_quantity      NUMERIC(10,2) NOT NULL DEFAULT 0,
    authorized_by           VARCHAR(150),
    dispensed_by            VARCHAR(150),
    witness                 VARCHAR(150),
    dispensed_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    wastage_quantity        NUMERIC(10,2) NOT NULL DEFAULT 0,
    documentation_complete  BOOLEAN      NOT NULL DEFAULT FALSE,
    variance_detected       BOOLEAN      NOT NULL DEFAULT FALSE,
    notes                   TEXT,
    synced_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ── hospilot.pharmacy_capacity_history ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS hospilot.pharmacy_capacity_history (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    record_date      DATE         NOT NULL,
    department       VARCHAR(100) NOT NULL DEFAULT 'Main Pharmacy',
    total_orders     INTEGER      NOT NULL DEFAULT 0,
    stat_orders      INTEGER      NOT NULL DEFAULT 0,
    routine_orders   INTEGER      NOT NULL DEFAULT 0,
    controlled_orders INTEGER     NOT NULL DEFAULT 0,
    avg_tat_minutes  INTEGER,
    peak_hour        INTEGER,
    dispensing_errors INTEGER     NOT NULL DEFAULT 0,
    notes            TEXT,
    synced_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE(record_date, department)
);

-- ── Seed Data ─────────────────────────────────────────────────────────────────
DO $$
DECLARE
    v_p1 UUID; v_p2 UUID; v_p3 UUID;
    v_o1 UUID; v_o2 UUID; v_o3 UUID;
BEGIN
    SELECT id INTO v_p1 FROM public.patients LIMIT 1 OFFSET 0;
    SELECT id INTO v_p2 FROM public.patients LIMIT 1 OFFSET 1;
    SELECT id INTO v_p3 FROM public.patients LIMIT 1 OFFSET 2;

    SELECT id INTO v_o1 FROM hospilot.lab_orders LIMIT 1 OFFSET 0;
    SELECT id INTO v_o2 FROM hospilot.lab_orders LIMIT 1 OFFSET 1;
    SELECT id INTO v_o3 FROM hospilot.lab_orders LIMIT 1 OFFSET 2;

    -- Pharmacy orders (8 rows)
    INSERT INTO hospilot.pharmacy_orders
        (patient_token, order_id, medication_name, generic_name, dosage, route, frequency,
         order_type, status, department, is_controlled, prescribed_by, prescribed_at, dispensed_at)
    VALUES
        (v_p1, v_o1, 'Vancomycin 1g',        'Vancomycin',     '1g',     'IV',   'Q12H',
         'STAT',    'pending',    'ICU',    FALSE, 'Dr. Arora',    NOW() - INTERVAL '30 minutes', NULL),
        (v_p1, v_o1, 'Norepinephrine 4mg',   'Norepinephrine', '4mg',    'IV',   'Continuous',
         'STAT',    'dispensing', 'ICU',    FALSE, 'Dr. Arora',    NOW() - INTERVAL '45 minutes', NULL),
        (v_p2, v_o2, 'Ceftriaxone 1g',       'Ceftriaxone',    '1g',     'IV',   'OD',
         'STAT',    'pending',    'ER',     FALSE, 'Dr. Mehta',    NOW() - INTERVAL '20 minutes', NULL),
        (v_p2, v_o2, 'Metoprolol 25mg',      'Metoprolol',     '25mg',   'Oral', 'BD',
         'urgent',  'dispensing', 'Ward 3', FALSE, 'Dr. Singh',    NOW() - INTERVAL '2 hours',    NULL),
        (v_p3, v_o3, 'Pantoprazole 40mg',    'Pantoprazole',   '40mg',   'Oral', 'OD',
         'urgent',  'dispensing', 'Ward 4', FALSE, 'Dr. Patel',    NOW() - INTERVAL '3 hours',    NULL),
        (v_p1, v_o1, 'Paracetamol 500mg',    'Paracetamol',    '500mg',  'Oral', 'QID',
         'routine', 'dispensed',  'Ward 4', FALSE, 'Dr. Sharma',   NOW() - INTERVAL '6 hours',    NOW() - INTERVAL '4 hours'),
        (v_p2, v_o2, 'Amlodipine 5mg',       'Amlodipine',     '5mg',    'Oral', 'OD',
         'routine', 'dispensed',  'Ward 2', FALSE, 'Dr. Gupta',    NOW() - INTERVAL '8 hours',    NOW() - INTERVAL '6 hours'),
        (v_p3, v_o3, 'Morphine 10mg',        'Morphine',       '10mg',   'IV',   'Q4H PRN',
         'STAT',    'on_hold',    'ICU',    TRUE,  'Dr. Arora',    NOW() - INTERVAL '1 hour',     NULL)
    ON CONFLICT DO NOTHING;

    -- Dispensing log (8 rows)
    INSERT INTO hospilot.pharmacy_dispensing_log
        (order_id, patient_token, medication_name, quantity_dispensed, dispensed_by,
         dispensed_at, verification_status, patient_verified, prescription_matched,
         dosage_correct, tat_minutes, is_stat)
    VALUES
        (v_o1, v_p1, 'Norepinephrine 4mg',  1, 'Pharm. Rao',   NOW() - INTERVAL '40 minutes', 'verified',     TRUE,  TRUE,  TRUE,  15, TRUE),
        (v_o1, v_p1, 'Paracetamol 500mg',   4, 'Pharm. Kumar', NOW() - INTERVAL '4 hours',    'verified',     TRUE,  TRUE,  TRUE,  8,  FALSE),
        (v_o2, v_p2, 'Amlodipine 5mg',      1, 'Pharm. Singh', NOW() - INTERVAL '6 hours',    'verified',     TRUE,  TRUE,  TRUE,  10, FALSE),
        (v_o2, v_p2, 'Metoprolol 25mg',     2, 'Pharm. Rao',   NOW() - INTERVAL '1 hour',     'pending',      TRUE,  TRUE,  TRUE,  NULL, FALSE),
        (v_o3, v_p3, 'Pantoprazole 40mg',   1, 'Pharm. Kumar', NOW() - INTERVAL '2 hours',    'verified',     TRUE,  TRUE,  TRUE,  12, FALSE),
        (v_o3, v_p3, 'Ceftriaxone 500mg',   1, 'Pharm. Singh', NOW() - INTERVAL '30 minutes', 'discrepancy',  TRUE,  TRUE,  FALSE, 25, FALSE),
        (v_o1, v_p1, 'Vancomycin 500mg',    2, 'Pharm. Rao',   NOW() - INTERVAL '3 hours',    'verified',     TRUE,  TRUE,  TRUE,  18, FALSE),
        (v_o2, v_p2, 'Amoxicillin 500mg',   3, 'Pharm. Nair',  NOW() - INTERVAL '5 hours',    'pending',      FALSE, TRUE,  TRUE,  NULL, FALSE)
    ON CONFLICT DO NOTHING;
END $$;

-- Inventory (10 rows — static config, no patient lookup needed)
INSERT INTO hospilot.pharmacy_inventory
    (medication_name, generic_name, brand_name, category, stock_quantity, reorder_level,
     unit, location, is_controlled, controlled_class, expiry_date, last_restocked_at)
VALUES
    ('Vancomycin 1g',       'Vancomycin',      'Vancocin',      'Antibiotic', 45,  20, 'vials',   'main',      FALSE, NULL, CURRENT_DATE + 180, NOW() - INTERVAL '7 days'),
    ('Ceftriaxone 1g',      'Ceftriaxone',     'Rocephin',      'Antibiotic', 8,   15, 'vials',   'main',      FALSE, NULL, CURRENT_DATE + 120, NOW() - INTERVAL '14 days'),
    ('Norepinephrine 4mg',  'Norepinephrine',  'Levophed',      'Cardiac',    30,  10, 'vials',   'icu_cart',  FALSE, NULL, CURRENT_DATE + 90,  NOW() - INTERVAL '3 days'),
    ('Metoprolol 25mg',     'Metoprolol',      'Betaloc',       'Cardiac',    6,   20, 'tablets', 'main',      FALSE, NULL, CURRENT_DATE + 365, NOW() - INTERVAL '30 days'),
    ('Pantoprazole 40mg',   'Pantoprazole',    'Pantocid',      'General',    120, 30, 'tablets', 'main',      FALSE, NULL, CURRENT_DATE + 300, NOW() - INTERVAL '10 days'),
    ('Paracetamol 500mg',   'Paracetamol',     'Calpol',        'General',    200, 50, 'tablets', 'main',      FALSE, NULL, CURRENT_DATE + 400, NOW() - INTERVAL '5 days'),
    ('Amlodipine 5mg',      'Amlodipine',      'Norvasc',       'Cardiac',    0,   20, 'tablets', 'main',      FALSE, NULL, CURRENT_DATE + 250, NOW() - INTERVAL '60 days'),
    ('Amlodipine 5mg',      'Amlodipine',      'Norvasc',       'Cardiac',    15,  10, 'tablets', 'satellite', FALSE, NULL, CURRENT_DATE + 250, NOW() - INTERVAL '30 days'),
    ('Morphine 10mg',       'Morphine',        'MS Contin',     'Controlled', 20,  5,  'vials',   'main',      TRUE,  'II', CURRENT_DATE + 60,  NOW() - INTERVAL '2 days'),
    ('Amoxicillin 500mg',   'Amoxicillin',     'Amoxil',        'Antibiotic', 0,   30, 'capsules','main',      FALSE, NULL, CURRENT_DATE + 180, NOW() - INTERVAL '45 days')
ON CONFLICT DO NOTHING;

-- Interaction rules (6 rows)
INSERT INTO hospilot.pharmacy_interaction_rules
    (drug_a, drug_b, severity, interaction_desc, action_required)
VALUES
    ('Warfarin',      'Aspirin',        'major',    'Increased bleeding risk — anticoagulant effect potentiated',             'escalate'),
    ('Methotrexate',  'Ibuprofen',      'major',    'NSAIDs reduce renal clearance of methotrexate — toxicity risk',          'hold'),
    ('Ciprofloxacin', 'Warfarin',       'major',    'Ciprofloxacin inhibits Warfarin metabolism — INR may spike',             'escalate'),
    ('Metoprolol',    'Verapamil',      'moderate', 'Combined negative chronotropy — bradycardia or heart block risk',        'alert'),
    ('Furosemide',    'Aminoglycosides','moderate', 'Additive ototoxicity risk — monitor renal function closely',             'alert'),
    ('Paracetamol',   'Alcohol',        'minor',    'Elevated hepatotoxicity risk with chronic alcohol use',                  'alert')
ON CONFLICT DO NOTHING;

-- Substitution rules (5 rows)
INSERT INTO hospilot.pharmacy_substitution_rules
    (original_drug, substitute_drug, therapeutic_class, requires_physician_approval, is_active, priority_order)
VALUES
    ('Amoxicillin 500mg',    'Ampicillin 500mg',      'Penicillin Antibiotic',  FALSE, TRUE, 1),
    ('Vancomycin 1g',        'Teicoplanin 400mg',     'Glycopeptide Antibiotic', TRUE,  TRUE, 1),
    ('Amlodipine 5mg',       'Felodipine 5mg',        'Calcium Channel Blocker', TRUE,  TRUE, 1),
    ('Pantoprazole 40mg',    'Omeprazole 20mg',       'Proton Pump Inhibitor',   FALSE, TRUE, 1),
    ('Metoprolol 25mg',      'Atenolol 25mg',         'Beta Blocker',            TRUE,  TRUE, 1)
ON CONFLICT DO NOTHING;

-- Controlled drug logs (4 rows)
DO $$
DECLARE
    v_p1 UUID; v_p3 UUID;
    v_o1 UUID; v_o3 UUID;
BEGIN
    SELECT id INTO v_p1 FROM public.patients LIMIT 1 OFFSET 0;
    SELECT id INTO v_p3 FROM public.patients LIMIT 1 OFFSET 2;
    SELECT id INTO v_o1 FROM hospilot.lab_orders LIMIT 1 OFFSET 0;
    SELECT id INTO v_o3 FROM hospilot.lab_orders LIMIT 1 OFFSET 2;

    INSERT INTO hospilot.pharmacy_controlled_log
        (order_id, patient_token, medication_name, controlled_class, dispensed_quantity,
         authorized_by, dispensed_by, witness, dispensed_at, wastage_quantity,
         documentation_complete, variance_detected, notes)
    VALUES
        (v_o1, v_p1, 'Morphine 10mg', 'II', 10.0, 'Dr. Arora',  'Pharm. Rao',   'Nurse Priya',   NOW() - INTERVAL '6 hours',  0.0,  TRUE,  FALSE, 'Routine ICU pain management'),
        (v_o1, v_p1, 'Morphine 10mg', 'II', 10.0, 'Dr. Arora',  'Pharm. Kumar', 'Nurse Amit',    NOW() - INTERVAL '2 hours',  0.0,  TRUE,  FALSE, 'Second dose — patient stable'),
        (v_o3, v_p3, 'Morphine 10mg', 'II', 10.0, 'Dr. Patel',  'Pharm. Rao',   NULL,            NOW() - INTERVAL '4 hours',  2.0,  FALSE, FALSE, 'Witness not present — documentation pending'),
        (v_o3, v_p3, 'Morphine 10mg', 'II', 10.0, 'Dr. Sharma', 'Pharm. Nair',  'Nurse Kavitha', NOW() - INTERVAL '1 hour',   0.0,  TRUE,  TRUE,  'Inventory count mismatch: 1 vial short — investigation initiated')
    ON CONFLICT DO NOTHING;
END $$;

-- Capacity history (14 days)
INSERT INTO hospilot.pharmacy_capacity_history
    (record_date, department, total_orders, stat_orders, routine_orders, controlled_orders,
     avg_tat_minutes, peak_hour, dispensing_errors)
VALUES
    (CURRENT_DATE - 14, 'Main Pharmacy', 280, 38, 230,  4, 14, 10, 1),
    (CURRENT_DATE - 13, 'Main Pharmacy', 265, 35, 218,  3, 16, 11, 2),
    (CURRENT_DATE - 12, 'Main Pharmacy', 180, 22, 150,  2, 12, 10, 0),
    (CURRENT_DATE - 11, 'Main Pharmacy', 172, 20, 144,  2, 11,  9, 0),
    (CURRENT_DATE - 10, 'Main Pharmacy', 295, 42, 240,  5, 15, 10, 1),
    (CURRENT_DATE -  9, 'Main Pharmacy', 310, 45, 250,  6, 18, 11, 2),
    (CURRENT_DATE -  8, 'Main Pharmacy', 330, 50, 264,  7, 20, 10, 3),
    (CURRENT_DATE -  7, 'Main Pharmacy', 275, 40, 222,  4, 13, 10, 1),
    (CURRENT_DATE -  6, 'Main Pharmacy', 260, 36, 212,  3, 14, 11, 0),
    (CURRENT_DATE -  5, 'Main Pharmacy', 185, 24, 153,  2, 12, 10, 0),
    (CURRENT_DATE -  4, 'Main Pharmacy', 175, 21, 147,  2, 11,  9, 0),
    (CURRENT_DATE -  3, 'Main Pharmacy', 300, 44, 242,  5, 16, 10, 1),
    (CURRENT_DATE -  2, 'Main Pharmacy', 320, 48, 258,  6, 17, 11, 2),
    (CURRENT_DATE -  1, 'Main Pharmacy', 345, 55, 272,  8, 22, 10, 3)
ON CONFLICT (record_date, department) DO NOTHING;
