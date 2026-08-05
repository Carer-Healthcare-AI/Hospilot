-- =====================================================
-- Seed: claim_queries and payment_reconciliation
-- =====================================================

-- ── claim_queries ─────────────────────────────────────────────────────────────
-- CLM010 (94f1a8b0) is in "Query" status — seed 2 queries, one responded

INSERT INTO hospilot.claim_queries
    (id, claim_id, query_type, query_text, status,
     raised_at, raised_by, responded_by, response_date, response_text,
     created_at, synced_at)
VALUES
(
    gen_random_uuid(),
    '94f1a8b0-a996-4e90-a46d-54d13a3f4d1c',
    'Documentation',
    'Discharge summary not attached. Please provide the complete discharge summary with treating doctor signature.',
    'Responded',
    '2026-05-05 09:00:00+00', 'tpa_ops@starhealth.in',
    'billing@hospital.in',
    '2026-05-06 11:30:00+00',
    'Discharge summary attached as document ref DS-2026-0504. Signed by Dr. Ramesh Kumar.',
    '2026-05-05 09:00:00+00', NOW()
),
(
    gen_random_uuid(),
    '94f1a8b0-a996-4e90-a46d-54d13a3f4d1c',
    'Medical Necessity',
    'Specialist consultation charge of Rs 3000 appears excessive for OPD visit. Please justify medical necessity.',
    'Pending',
    '2026-05-07 10:15:00+00', 'tpa_ops@starhealth.in',
    NULL, NULL, NULL,
    '2026-05-07 10:15:00+00', NOW()
),
-- CLM009 (3cc3a2a7) Submitted — seed 1 pre-auth query
(
    gen_random_uuid(),
    '3cc3a2a7-0d6f-4b0c-8be3-53a29ed367ed',
    'Pre-Auth',
    'Pre-authorisation number not found in TPA system. Please confirm procedure codes submitted.',
    'Responded',
    '2026-04-29 08:00:00+00', 'tpa_ops@unitedhealth.in',
    'billing@hospital.in',
    '2026-04-29 16:45:00+00',
    'Pre-auth number PA-2026-00312 issued on 2026-04-28. Procedure codes: OPD002, RAD-DEXA.',
    '2026-04-29 08:00:00+00', NOW()
),
-- CLM008 (6f4fa695) Submitted — billing discrepancy query
(
    gen_random_uuid(),
    '6f4fa695-7b43-484c-9efb-674c3929138a',
    'Billing',
    'Duplicate charge detected: Ambulance Service (Local) billed twice on the same invoice. Clarification required.',
    'Pending',
    '2026-04-30 14:00:00+00', 'tpa_ops@unitedhealth.in',
    NULL, NULL, NULL,
    '2026-04-30 14:00:00+00', NOW()
);

-- ── payment_reconciliation ────────────────────────────────────────────────────
-- Seeding reconciliation for the 3 most recent collection dates.
-- total_expected = total_collection from daily_collections.
-- actual splits are realistic breakdowns; variance = actual - expected.

INSERT INTO hospilot.payment_reconciliation
    (id, reconciliation_date,
     total_expected, total_actual, total_variance,
     actual_cash, actual_card, actual_upi, actual_bank,
     cash_variance, card_variance, upi_variance, bank_variance,
     status, created_at, synced_at)
VALUES
(
    gen_random_uuid(),
    '2026-05-04',
    1163.62, 1163.62, 0.00,
    400.00, 263.62, 500.00, 0.00,
    0.00, 0.00, 0.00, 0.00,
    'Reconciled',
    '2026-05-04 22:00:00+00', NOW()
),
(
    gen_random_uuid(),
    '2026-04-28',
    1900.64, 1875.00, -25.64,
    500.00, 375.00, 1000.00, 0.00,
    0.00, 0.00, -25.64, 0.00,
    'Discrepancy',
    '2026-04-28 22:00:00+00', NOW()
),
(
    gen_random_uuid(),
    '2026-04-27',
    13.54, 13.54, 0.00,
    13.54, 0.00, 0.00, 0.00,
    0.00, 0.00, 0.00, 0.00,
    'Reconciled',
    '2026-04-27 22:00:00+00', NOW()
);
