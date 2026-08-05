-- =============================================================================
-- Migration 005: Schema Reorganisation
--
-- Goals:
--   1. Create hospilot_app schema — move all orchestration/frontend tables here
--   2. Create missing hospilot data tables (lab, infection, supply, billing)
--   3. Create hospilot copies of all public (CarerOS) tables used by agents
--
-- Run order: hospilot_app first, then data tables, then public copies.
-- After running: re-track all moved/new tables in Hasura console.
-- =============================================================================


-- =============================================================================
-- PART 1: hospilot_app schema — orchestration / frontend tables
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS hospilot_app;

-- Move app tables from hospilot → hospilot_app
-- (PostgreSQL preserves FKs within the same database when moving schemas)

ALTER TABLE IF EXISTS hospilot.sessions                  SET SCHEMA hospilot_app;
ALTER TABLE IF EXISTS hospilot.approval_tasks            SET SCHEMA hospilot_app;
ALTER TABLE IF EXISTS hospilot.audit_log                 SET SCHEMA hospilot_app;
ALTER TABLE IF EXISTS hospilot.session_agent_overrides   SET SCHEMA hospilot_app;
ALTER TABLE IF EXISTS hospilot.agent_registry            SET SCHEMA hospilot_app;
ALTER TABLE IF EXISTS hospilot.subagent_registry         SET SCHEMA hospilot_app;
ALTER TABLE IF EXISTS hospilot.task_registry             SET SCHEMA hospilot_app;

-- Add function_code column to task_registry (used by task codegen)
ALTER TABLE hospilot_app.task_registry
    ADD COLUMN IF NOT EXISTS function_code text;


-- =============================================================================
-- PART 2: Missing hospilot data tables (referenced in code but don't exist yet)
-- =============================================================================

-- ── Lab ──────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.lab_orders (
    id          uuid        PRIMARY KEY,
    visit_id    uuid,
    patient_token text,
    ordered_by  text,
    status      text        DEFAULT 'Pending',  -- Pending | In Progress | Completed
    priority    text,
    ordered_at  timestamptz,
    completed_at timestamptz,
    synced_at   timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lab_orders_status       ON hospilot.lab_orders (status);
CREATE INDEX IF NOT EXISTS idx_lab_orders_patient      ON hospilot.lab_orders (patient_token);
CREATE INDEX IF NOT EXISTS idx_lab_orders_ordered_at   ON hospilot.lab_orders (ordered_at);

CREATE TABLE IF NOT EXISTS hospilot.lab_results (
    id              uuid    PRIMARY KEY,
    order_id        uuid    REFERENCES hospilot.lab_orders (id) ON DELETE CASCADE,
    patient_token   text,
    test_name       text,
    test_code       text,
    result_value    text,
    flag            text,
    reference_range text,
    unit            text,
    reported_at     timestamptz,
    synced_at       timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lab_results_order       ON hospilot.lab_results (order_id);
CREATE INDEX IF NOT EXISTS idx_lab_results_reported_at ON hospilot.lab_results (reported_at);

-- ── Infection Control ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.infection_cases (
    id                  uuid    PRIMARY KEY,
    patient_token       text,
    admission_id        uuid,
    ward                text,
    pathogen            text,
    severity            text,
    isolation_required  boolean DEFAULT false,
    isolation_confirmed boolean DEFAULT false,
    isolation_room      text,
    status              text    DEFAULT 'active',  -- active | resolved
    reported_at         timestamptz,
    notes               text,
    synced_at           timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_infection_status     ON hospilot.infection_cases (status);
CREATE INDEX IF NOT EXISTS idx_infection_ward       ON hospilot.infection_cases (ward);

-- ── Supply Chain ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.supplies (
    id                  uuid    PRIMARY KEY,
    item_code           text,
    item_name           text,
    category            text,
    current_stock       numeric,
    min_stock           numeric,
    unit                text,
    unit_cost           numeric,
    last_ordered_at     timestamptz,
    last_received_at    timestamptz,
    synced_at           timestamptz DEFAULT now()
);

-- ── Revenue / Billing ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.invoices (
    id              uuid    PRIMARY KEY,
    invoice_number  text,
    patient_token   text,
    admission_id    uuid,
    visit_id        uuid,
    invoice_type    text,
    invoice_date    date,
    due_date        date,
    grand_total     numeric,
    paid_amount     numeric,
    balance         numeric,
    status          text,
    payment_status  text,   -- Unpaid | Partial | Paid
    synced_at       timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_invoices_payment_status ON hospilot.invoices (payment_status);
CREATE INDEX IF NOT EXISTS idx_invoices_patient        ON hospilot.invoices (patient_token);

CREATE TABLE IF NOT EXISTS hospilot.payments (
    id              uuid    PRIMARY KEY,
    invoice_id      uuid,
    patient_token   text,
    payment_date    date,
    total_amount    numeric,
    status          text,
    synced_at       timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.daily_collections (
    id                      uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_date         date    UNIQUE NOT NULL,
    total_collection        numeric,
    cash_total              numeric,
    upi_total               numeric,
    card_total              numeric,
    bank_transfer_total     numeric,
    invoice_count           int,
    payment_count           int,
    is_reconciled           boolean DEFAULT false,
    variance                numeric,
    synced_at               timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.claims (
    id                  uuid    PRIMARY KEY,
    patient_token       text,
    visit_id            text,
    tpa_id              text,
    tpa_name            text,
    claim_amount        numeric,
    status              text,
    created_at          timestamptz,
    submitted_date      timestamptz,
    approved_amount     numeric,
    denial_reason       text,
    claim_number        text,
    payer_type          text,
    risk_level          text,
    risk_score          numeric,
    stage               text,
    compliance_status   text,
    diagnosis_code      text,
    branch_id           uuid,
    synced_at           timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_claims_status   ON hospilot.claims (status);
CREATE INDEX IF NOT EXISTS idx_claims_patient  ON hospilot.claims (patient_token);

CREATE TABLE IF NOT EXISTS hospilot.claim_line_items (
    id                  uuid    PRIMARY KEY,
    claim_id            uuid    REFERENCES hospilot.claims (id) ON DELETE CASCADE,
    service_code        text,
    service_name        text,
    description         text,
    quantity            numeric,
    rate                numeric,
    amount              numeric,
    approved_amount     numeric,
    approved_quantity   numeric,
    approved_rate       numeric,
    status              text,
    category            text,
    unit                text,
    rejection_reason    text,
    synced_at           timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.claim_history (
    id          uuid    PRIMARY KEY,
    claim_id    uuid    REFERENCES hospilot.claims (id) ON DELETE CASCADE,
    from_status text,
    to_status   text,
    action      text,
    changed_at  timestamptz,
    changed_by  text,
    remarks     text,
    synced_at   timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_claim_history_claim ON hospilot.claim_history (claim_id);

CREATE TABLE IF NOT EXISTS hospilot.claim_queries (
    id              uuid    PRIMARY KEY,
    claim_id        uuid    REFERENCES hospilot.claims (id) ON DELETE CASCADE,
    query_type      text,
    query_text      text,
    status          text,
    raised_at       timestamptz,
    raised_by       text,
    responded_by    text,
    response_date   timestamptz,
    response_text   text,
    created_at      timestamptz,
    synced_at       timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.insurance_contracts (
    id                  uuid    PRIMARY KEY,
    insurer_name        text,
    tpa_name            text,
    contract_type       text,
    contract_number     text,
    start_date          date,
    end_date            date,
    status              text,
    branch_id           uuid,
    total_claims        int,
    approved_amount     numeric,
    rejection_rate      numeric,
    avg_settlement_days numeric,
    synced_at           timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.contract_service_rates (
    id                  uuid    PRIMARY KEY,
    contract_id         uuid    REFERENCES hospilot.insurance_contracts (id) ON DELETE CASCADE,
    service_id          uuid,
    service_code        text,
    service_name        text,
    contract_rate       numeric,
    hospital_rate       numeric,
    discount_percentage numeric,
    is_active           boolean DEFAULT true,
    synced_at           timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.invoice_line_items (
    id                  uuid    PRIMARY KEY,
    invoice_id          uuid    REFERENCES hospilot.invoices (id) ON DELETE CASCADE,
    service_id          uuid,
    service_code        text,
    service_name        text,
    description         text,
    quantity            numeric,
    rate                numeric,
    amount              numeric,
    total               numeric,
    gst_rate            numeric,
    gst_amount          numeric,
    discount_amount     numeric,
    source_type         text,
    source_id           uuid,
    synced_at           timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.payment_entries (
    id                      uuid    PRIMARY KEY,
    payment_id              uuid    REFERENCES hospilot.payments (id) ON DELETE CASCADE,
    payment_mode            text,
    amount                  numeric,
    transaction_reference   text,
    bank_name               text,
    card_last_four          text,
    created_at              timestamptz,
    synced_at               timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.refunds (
    id              uuid    PRIMARY KEY,
    invoice_id      uuid,
    payment_id      uuid,
    refund_amount   numeric,
    reason          text,
    status          text,
    refund_date     date,
    refund_mode     text,
    refund_number   text,
    created_at      timestamptz,
    synced_at       timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.payment_reconciliation (
    id                  uuid    PRIMARY KEY DEFAULT gen_random_uuid(),
    reconciliation_date date    UNIQUE NOT NULL,
    total_expected      numeric,
    total_actual        numeric,
    total_variance      numeric,
    actual_cash         numeric,
    actual_card         numeric,
    actual_upi          numeric,
    actual_bank         numeric,
    cash_variance       numeric,
    card_variance       numeric,
    upi_variance        numeric,
    bank_variance       numeric,
    status              text,
    created_at          timestamptz,
    synced_at           timestamptz DEFAULT now()
);


-- =============================================================================
-- PART 3: hospilot copies of public (CarerOS) tables not yet synced
-- =============================================================================

-- ── Patients (used by get_patient_names) ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.patients (
    id          uuid    PRIMARY KEY,   -- same as CarerOS public.patients.id
    first_name  text,
    last_name   text,
    uhid        text,
    synced_at   timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_patients_uhid ON hospilot.patients (uhid);

-- ── OT Surgeries ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.ot_surgeries (
    id              uuid    PRIMARY KEY,
    admission_id    uuid,
    patient_token   text,
    ward            text,
    status          text,   -- Scheduled | In Progress | Completed | Cancelled
    created_at      timestamptz,
    synced_at       timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ot_status ON hospilot.ot_surgeries (status);

-- ── Purchase Orders ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hospilot.purchase_orders (
    id                  uuid    PRIMARY KEY,
    po_number           text,
    vendor_id           uuid,
    status              text,   -- Pending Approval | Approved | etc.
    total               numeric,
    order_date          date,
    expected_delivery   date,
    created_at          timestamptz,
    synced_at           timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_po_status ON hospilot.purchase_orders (status);


-- =============================================================================
-- POST-MIGRATION CHECKLIST
-- =============================================================================
-- After running this migration:
--
-- 1. Hasura console → Data → hospilot_app schema:
--    Track all moved tables (sessions, approval_tasks, audit_log,
--    session_agent_overrides, agent_registry, subagent_registry, task_registry)
--
-- 2. Hasura console → Data → hospilot schema:
--    Track all new tables (lab_orders, lab_results, infection_cases, supplies,
--    invoices, payments, daily_collections, claims, claim_line_items,
--    claim_history, claim_queries, insurance_contracts, contract_service_rates,
--    invoice_line_items, payment_entries, refunds, payment_reconciliation,
--    patients, ot_surgeries, purchase_orders)
--
-- 3. Update hasura.py GQL queries:
--    - hospilot_sessions → hospilot_app_sessions
--    - hospilot_approval_tasks → hospilot_app_approval_tasks
--    - hospilot_audit_log → hospilot_app_audit_log
--    - hospilot_session_agent_overrides → hospilot_app_session_agent_overrides
--    - hospilot_agent_registry → hospilot_app_agent_registry (etc.)
--
-- 4. Add upsert methods to hasura.py for new tables
-- 5. Wire new tables into carerOS_poller.py sync loops
-- =============================================================================
