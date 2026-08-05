-- =====================================================
-- Revenue Agent: hospilot schema billing tables
-- Direct copies of public.invoices / payments / daily_collections
-- with synced_at added for poller tracking.
-- =====================================================

-- ── invoices ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.invoices (
    id                      UUID PRIMARY KEY,
    org_id                  UUID,
    invoice_number          VARCHAR(50),
    patient_id              UUID,
    invoice_date            TIMESTAMPTZ,
    due_date                TIMESTAMPTZ,
    invoice_type            VARCHAR(20),
    visit_id                UUID,
    admission_id            UUID,
    package_id              UUID,
    insurance_contract_id   UUID,
    subtotal                DECIMAL(12, 2) DEFAULT 0,
    discount_amount         DECIMAL(12, 2) DEFAULT 0,
    discount_percentage     DECIMAL(5, 2)  DEFAULT 0,
    gst_amount              DECIMAL(12, 2) DEFAULT 0,
    cgst_amount             DECIMAL(12, 2) DEFAULT 0,
    sgst_amount             DECIMAL(12, 2) DEFAULT 0,
    igst_amount             DECIMAL(12, 2) DEFAULT 0,
    grand_total             DECIMAL(12, 2),
    paid_amount             DECIMAL(12, 2) DEFAULT 0,
    balance                 DECIMAL(12, 2),
    status                  VARCHAR(20)    DEFAULT 'Draft',
    payment_status          VARCHAR(20)    DEFAULT 'Unpaid',
    is_inter_state          BOOLEAN        DEFAULT FALSE,
    notes                   TEXT,
    created_by              UUID,
    updated_by              UUID,
    cancelled_by            UUID,
    cancelled_at            TIMESTAMPTZ,
    cancellation_reason     TEXT,
    branch_id               UUID,
    created_at              TIMESTAMPTZ    DEFAULT NOW(),
    updated_at              TIMESTAMPTZ    DEFAULT NOW(),
    synced_at               TIMESTAMPTZ    DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hinvoices_patient    ON hospilot.invoices(patient_id);
CREATE INDEX IF NOT EXISTS idx_hinvoices_admission  ON hospilot.invoices(admission_id);
CREATE INDEX IF NOT EXISTS idx_hinvoices_visit      ON hospilot.invoices(visit_id);
CREATE INDEX IF NOT EXISTS idx_hinvoices_pstatus    ON hospilot.invoices(payment_status);
CREATE INDEX IF NOT EXISTS idx_hinvoices_status     ON hospilot.invoices(status);
CREATE INDEX IF NOT EXISTS idx_hinvoices_date       ON hospilot.invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_hinvoices_type       ON hospilot.invoices(invoice_type);

INSERT INTO hospilot.invoices (
    id, org_id, invoice_number, patient_id, invoice_date, due_date,
    invoice_type, visit_id, admission_id, package_id, insurance_contract_id,
    subtotal, discount_amount, discount_percentage,
    gst_amount, cgst_amount, sgst_amount, igst_amount,
    grand_total, paid_amount, balance,
    status, payment_status, is_inter_state, notes,
    created_by, updated_by, cancelled_by, cancelled_at, cancellation_reason,
    branch_id, created_at, updated_at, synced_at
)
SELECT
    id, org_id, invoice_number, patient_id, invoice_date, due_date,
    invoice_type, visit_id, admission_id, package_id, insurance_contract_id,
    subtotal, discount_amount, discount_percentage,
    gst_amount, cgst_amount, sgst_amount, igst_amount,
    grand_total, paid_amount, balance,
    status, payment_status, is_inter_state, notes,
    created_by, updated_by, cancelled_by, cancelled_at, cancellation_reason,
    branch_id, created_at, updated_at, NOW()
FROM public.invoices
ON CONFLICT (id) DO NOTHING;

-- ── payments ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.payments (
    id              UUID PRIMARY KEY,
    org_id          UUID,
    receipt_number  VARCHAR(50),
    invoice_id      UUID,
    patient_id      UUID,
    payment_date    TIMESTAMPTZ,
    total_amount    DECIMAL(12, 2),
    status          VARCHAR(20)  DEFAULT 'Completed',
    received_by     UUID,
    notes           TEXT,
    branch_id       UUID,
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  DEFAULT NOW(),
    synced_at       TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_hpayments_invoice  ON hospilot.payments(invoice_id);
CREATE INDEX IF NOT EXISTS idx_hpayments_patient  ON hospilot.payments(patient_id);
CREATE INDEX IF NOT EXISTS idx_hpayments_date     ON hospilot.payments(payment_date);
CREATE INDEX IF NOT EXISTS idx_hpayments_status   ON hospilot.payments(status);

INSERT INTO hospilot.payments (
    id, org_id, receipt_number, invoice_id, patient_id,
    payment_date, total_amount, status, received_by, notes,
    branch_id, created_at, updated_at, synced_at
)
SELECT
    id, org_id, receipt_number, invoice_id, patient_id,
    payment_date, total_amount, status, received_by, notes,
    branch_id, created_at, updated_at, NOW()
FROM public.payments
ON CONFLICT (id) DO NOTHING;

-- ── daily_collections ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.daily_collections (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id              UUID,
    collection_date     DATE         NOT NULL,
    cash_total          DECIMAL(12, 2) DEFAULT 0,
    upi_total           DECIMAL(12, 2) DEFAULT 0,
    card_total          DECIMAL(12, 2) DEFAULT 0,
    bank_transfer_total DECIMAL(12, 2) DEFAULT 0,
    cheque_total        DECIMAL(12, 2) DEFAULT 0,
    total_collection    DECIMAL(12, 2) DEFAULT 0,
    invoice_count       INTEGER        DEFAULT 0,
    payment_count       INTEGER        DEFAULT 0,
    is_reconciled       BOOLEAN        DEFAULT FALSE,
    reconciled_by       UUID,
    reconciled_at       TIMESTAMPTZ,
    variance            DECIMAL(12, 2) DEFAULT 0,
    created_at          TIMESTAMPTZ    DEFAULT NOW(),
    updated_at          TIMESTAMPTZ    DEFAULT NOW(),
    synced_at           TIMESTAMPTZ    DEFAULT NOW(),
    UNIQUE (org_id, collection_date)
);

CREATE INDEX IF NOT EXISTS idx_hdaily_collections_date ON hospilot.daily_collections(collection_date);
CREATE INDEX IF NOT EXISTS idx_hdaily_collections_org  ON hospilot.daily_collections(org_id);

INSERT INTO hospilot.daily_collections (
    id, org_id, collection_date,
    cash_total, upi_total, card_total, bank_transfer_total, cheque_total,
    total_collection, invoice_count, payment_count,
    is_reconciled, reconciled_by, reconciled_at, variance,
    created_at, updated_at, synced_at
)
SELECT
    id, org_id, collection_date,
    cash_total, upi_total, card_total, bank_transfer_total, cheque_total,
    total_collection, invoice_count, payment_count,
    is_reconciled, reconciled_by, reconciled_at, variance,
    created_at, updated_at, NOW()
FROM public.daily_collections
ON CONFLICT (org_id, collection_date) DO NOTHING;
