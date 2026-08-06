-- ============================================================
-- Hospilot Schema — All Tables
-- ============================================================

CREATE TABLE hospilot.appointments (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    patient_id uuid,
    provider_id uuid,
    department_id uuid,
    appointment_time timestamp without time zone,
    status character varying(50),
    type character varying(100),
    patient_name text,
    phone text,
    email text,
    specialization text,
    department_name text,
    synced_at timestamp with time zone DEFAULT now()
);

CREATE TABLE hospilot.beds (
    id uuid NOT NULL,
    branch_id uuid,
    ward text,
    bed_number text,
    room_type text,
    status text NOT NULL,
    is_active boolean NOT NULL,
    synced_at timestamp with time zone NOT NULL,
    ventilation text,
    room_sharing text,
    proximity integer,
    floor integer,
    wing text,
    natural_light boolean,
    noise_level text,
    features ARRAY
);

CREATE TABLE hospilot.claim_history (
    id uuid NOT NULL,
    claim_id uuid,
    from_status text,
    to_status text,
    action text,
    changed_at timestamp with time zone,
    changed_by text,
    remarks text,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.claim_line_items (
    id uuid NOT NULL,
    claim_id uuid,
    service_code text,
    service_name text,
    description text,
    quantity numeric,
    rate numeric,
    amount numeric,
    approved_amount numeric,
    approved_quantity numeric,
    approved_rate numeric,
    status text,
    category text,
    unit text,
    rejection_reason text,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.claim_queries (
    id uuid NOT NULL,
    claim_id uuid,
    query_type text,
    query_text text,
    status text,
    raised_at timestamp with time zone,
    raised_by text,
    responded_by text,
    response_date timestamp with time zone,
    response_text text,
    created_at timestamp with time zone,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.claims (
    id uuid NOT NULL,
    patient_token text,
    visit_id text,
    tpa_id text,
    tpa_name text,
    claim_amount numeric,
    status text,
    created_at timestamp with time zone,
    submitted_date timestamp with time zone,
    approved_amount numeric,
    denial_reason text,
    claim_number text,
    payer_type text,
    risk_level text,
    risk_score numeric,
    stage text,
    compliance_status text,
    diagnosis_code text,
    branch_id uuid,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.contract_service_rates (
    id uuid NOT NULL,
    contract_id uuid,
    service_id uuid,
    service_code text,
    service_name text,
    contract_rate numeric,
    hospital_rate numeric,
    discount_percentage numeric,
    is_active boolean,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.daily_collections (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    org_id uuid,
    collection_date date NOT NULL,
    cash_total numeric DEFAULT 0,
    upi_total numeric DEFAULT 0,
    card_total numeric DEFAULT 0,
    bank_transfer_total numeric DEFAULT 0,
    cheque_total numeric DEFAULT 0,
    total_collection numeric DEFAULT 0,
    invoice_count integer DEFAULT 0,
    payment_count integer DEFAULT 0,
    is_reconciled boolean DEFAULT false,
    reconciled_by uuid,
    reconciled_at timestamp with time zone,
    variance numeric DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    synced_at timestamp with time zone DEFAULT now()
);

CREATE TABLE hospilot.departments (
    id uuid NOT NULL,
    name text NOT NULL,
    type text,
    synced_at timestamp with time zone NOT NULL,
    capacity integer,
    target_occupancy_pct integer
);

CREATE TABLE hospilot.discharge_summaries (
    id uuid NOT NULL,
    admission_id uuid,
    summary_text text,
    created_at timestamp with time zone,
    synced_at timestamp with time zone NOT NULL,
    ai_generated_note text
);

CREATE TABLE hospilot.doctor_slots (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    provider_id uuid,
    slot_date date,
    slot_start time without time zone,
    slot_end time without time zone,
    slot_type character varying(50),
    status character varying(50),
    max_patients integer,
    booked_count integer,
    specialization text,
    synced_at timestamp with time zone DEFAULT now()
);

CREATE TABLE hospilot.waitlist (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    patient_id uuid,
    patient_name text,
    phone text,
    email text,
    specialization text,
    priority character varying(20) DEFAULT 'medium',
    requested_date date,
    status character varying(50) DEFAULT 'waitlisted',
    reason text,
    created_at timestamp with time zone DEFAULT now(),
    synced_at timestamp with time zone DEFAULT now()
);

CREATE TABLE hospilot.staff_roster (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    area character varying(50),
    area_label text,
    role character varying(50),
    shift character varying(20),
    headcount integer DEFAULT 0,
    assigned_load integer DEFAULT 0,
    load_per_staff integer DEFAULT 1,
    branch_id uuid,
    synced_at timestamp with time zone DEFAULT now()
);

CREATE TABLE hospilot.service_slots (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    slot_type character varying(50),
    slot_date date,
    slot_start time without time zone,
    slot_end time without time zone,
    location text,
    specialization text,
    max_patients integer DEFAULT 1,
    booked_count integer DEFAULT 0,
    status character varying(50) DEFAULT 'open',
    synced_at timestamp with time zone DEFAULT now()
);

CREATE TABLE hospilot.hospilot_agent_registry (
    id text NOT NULL,
    label text NOT NULL,
    description text NOT NULL DEFAULT '',
    emoji text NOT NULL DEFAULT '🤖',
    color text NOT NULL DEFAULT '#94a3b8',
    is_active boolean NOT NULL DEFAULT true,
    sort_order integer NOT NULL DEFAULT 0,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE hospilot.hospilot_subagent_registry (
    id text NOT NULL,
    agent_id text NOT NULL,
    label text NOT NULL,
    description text NOT NULL DEFAULT '',
    capabilities jsonb NOT NULL DEFAULT '[]',
    is_active boolean NOT NULL DEFAULT true,
    is_prefetch_eligible boolean NOT NULL DEFAULT false,
    sort_order integer NOT NULL DEFAULT 0,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE hospilot.hospilot_task_registry (
    id text NOT NULL,
    subagent_id text NOT NULL,
    label text NOT NULL,
    description text NOT NULL DEFAULT '',
    outputs jsonb NOT NULL DEFAULT '[]',
    is_active boolean NOT NULL DEFAULT true,
    sort_order integer NOT NULL DEFAULT 0,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now()
);

CREATE TABLE hospilot.infection_cases (
    id uuid NOT NULL,
    patient_token text,
    admission_id uuid,
    ward text,
    pathogen text,
    severity text,
    isolation_required boolean,
    isolation_confirmed boolean,
    isolation_room text,
    status text,
    reported_at timestamp with time zone,
    notes text,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.insurance_contracts (
    id uuid NOT NULL,
    insurer_name text,
    tpa_name text,
    contract_type text,
    contract_number text,
    start_date date,
    end_date date,
    status text,
    branch_id uuid,
    total_claims integer,
    approved_amount numeric,
    rejection_rate numeric,
    avg_settlement_days numeric,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.invoice_line_items (
    id uuid NOT NULL,
    invoice_id uuid,
    service_id uuid,
    service_code text,
    service_name text,
    description text,
    quantity numeric,
    rate numeric,
    amount numeric,
    total numeric,
    gst_rate numeric,
    gst_amount numeric,
    discount_amount numeric,
    source_type text,
    source_id uuid,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.invoices (
    id uuid NOT NULL,
    org_id uuid,
    invoice_number character varying(50),
    patient_id uuid,
    invoice_date timestamp with time zone,
    due_date timestamp with time zone,
    invoice_type character varying(20),
    visit_id uuid,
    admission_id uuid,
    package_id uuid,
    insurance_contract_id uuid,
    subtotal numeric DEFAULT 0,
    discount_amount numeric DEFAULT 0,
    discount_percentage numeric DEFAULT 0,
    gst_amount numeric DEFAULT 0,
    cgst_amount numeric DEFAULT 0,
    sgst_amount numeric DEFAULT 0,
    igst_amount numeric DEFAULT 0,
    grand_total numeric,
    paid_amount numeric DEFAULT 0,
    balance numeric,
    status character varying(20) DEFAULT 'Draft',
    payment_status character varying(20) DEFAULT 'Unpaid',
    is_inter_state boolean DEFAULT false,
    notes text,
    created_by uuid,
    updated_by uuid,
    cancelled_by uuid,
    cancelled_at timestamp with time zone,
    cancellation_reason text,
    branch_id uuid,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    synced_at timestamp with time zone DEFAULT now()
);

CREATE TABLE hospilot.ipd_admissions (
    id uuid NOT NULL,
    patient_token text,
    bed_id uuid,
    department_id uuid,
    admitted_at timestamp with time zone,
    expected_discharge_at timestamp with time zone,
    status text,
    synced_at timestamp with time zone NOT NULL,
    discharge_ready boolean,
    discharge_blocked_reason text,
    transfer_pending boolean NOT NULL
);

CREATE TABLE hospilot.lab_orders (
    id uuid NOT NULL,
    visit_id uuid,
    patient_token text,
    ordered_by text,
    status text,
    priority text,
    ordered_at timestamp with time zone,
    completed_at timestamp with time zone,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.lab_results (
    id uuid NOT NULL,
    order_id uuid,
    patient_token text,
    test_name text,
    test_code text,
    result_value text,
    flag text,
    reference_range text,
    unit text,
    reported_at timestamp with time zone,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.nursing_tasks (
    id uuid NOT NULL,
    admission_id uuid,
    task text NOT NULL,
    completed boolean,
    due_at timestamp with time zone,
    assigned_to text,
    synced_at timestamp with time zone NOT NULL
);

CREATE TABLE hospilot.ot_surgeries (
    id uuid NOT NULL,
    admission_id uuid,
    patient_token text,
    ward text,
    status text,
    created_at timestamp with time zone,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.patients (
    id uuid NOT NULL,
    first_name text,
    last_name text,
    uhid text,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.payment_entries (
    id uuid NOT NULL,
    payment_id uuid,
    payment_mode text,
    amount numeric,
    transaction_reference text,
    bank_name text,
    card_last_four text,
    created_at timestamp with time zone,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.payment_reconciliation (
    id uuid NOT NULL,
    reconciliation_date date NOT NULL,
    total_expected numeric,
    total_actual numeric,
    total_variance numeric,
    actual_cash numeric,
    actual_card numeric,
    actual_upi numeric,
    actual_bank numeric,
    cash_variance numeric,
    card_variance numeric,
    upi_variance numeric,
    bank_variance numeric,
    status text,
    created_at timestamp with time zone,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.payments (
    id uuid NOT NULL,
    org_id uuid,
    receipt_number character varying(50),
    invoice_id uuid,
    patient_id uuid,
    payment_date timestamp with time zone,
    total_amount numeric,
    status character varying(20) DEFAULT 'Completed',
    received_by uuid,
    notes text,
    branch_id uuid,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    synced_at timestamp with time zone DEFAULT now()
);

CREATE TABLE hospilot.purchase_orders (
    id uuid NOT NULL,
    po_number text,
    vendor_id uuid,
    status text,
    total numeric,
    order_date date,
    expected_delivery date,
    created_at timestamp with time zone,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.refunds (
    id uuid NOT NULL,
    invoice_id uuid,
    payment_id uuid,
    refund_amount numeric,
    reason text,
    status text,
    refund_date date,
    refund_mode text,
    refund_number text,
    created_at timestamp with time zone,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.supplies (
    id uuid NOT NULL,
    item_code text,
    item_name text,
    category text,
    current_stock numeric,
    min_stock numeric,
    unit text,
    unit_cost numeric,
    last_ordered_at timestamp with time zone,
    last_received_at timestamp with time zone,
    synced_at timestamp with time zone
);

CREATE TABLE hospilot.visits (
    id uuid NOT NULL,
    patient_token text,
    department_id uuid,
    arrived_at timestamp with time zone,
    status text,
    chief_complaint text,
    synced_at timestamp with time zone NOT NULL,
    triage_score integer,
    visit_type text,
    appointment_id uuid
);

CREATE TABLE hospilot.vitals (
    id uuid NOT NULL,
    patient_token text,
    admission_id uuid,
    recorded_at timestamp with time zone NOT NULL,
    temperature numeric,
    pulse integer,
    bp_systolic integer,
    bp_diastolic integer,
    spo2 integer,
    respiratory_rate integer,
    gcs integer,
    synced_at timestamp with time zone NOT NULL,
    is_critical boolean
);
