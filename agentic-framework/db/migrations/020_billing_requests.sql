-- =====================================================
-- Initiate Billing: bill-generation request queue.
--
-- The revenue_agent "Initiate Billing" sub-agent stages a draft request during
-- the flow; on session commit (recommendation pushed to the HIS) the backend
-- inserts a row here with status 'pending'. The DB side polls pending rows,
-- creates the actual bill from the patient's recorded charges, then writes back
-- invoice_id and flips status to 'created' (or 'failed' with error_message).
--
-- IDs are stored as TEXT (not UUID) so provisional / incoming-patient tokens that
-- are not yet UUIDs never break the insert -- the DB side resolves the real keys.
-- =====================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS hospilot.billing_requests (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id             UUID        NOT NULL,
    agent_id               TEXT        DEFAULT 'revenue_agent',
    patient_token          TEXT,
    patient_name           TEXT,
    uhid                   TEXT,
    visit_id               TEXT,
    admission_id           TEXT,
    invoice_type           VARCHAR(20) DEFAULT 'IPD',
    generate_from_charges  BOOLEAN     DEFAULT TRUE,
    source                 TEXT        DEFAULT 'initiate_billing',
    notes                  TEXT,
    payload                JSONB       DEFAULT '{}'::jsonb,
    status                 VARCHAR(20) DEFAULT 'pending',   -- pending | processing | created | failed
    invoice_id             UUID,                            -- written by the DB side once the bill is created
    error_message          TEXT,
    created_at             TIMESTAMPTZ DEFAULT NOW(),
    processed_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_hbillreq_session ON hospilot.billing_requests(session_id);
CREATE INDEX IF NOT EXISTS idx_hbillreq_status  ON hospilot.billing_requests(status);
CREATE INDEX IF NOT EXISTS idx_hbillreq_patient ON hospilot.billing_requests(patient_token);
