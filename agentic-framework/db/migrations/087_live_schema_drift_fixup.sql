-- Reconciles this migration history with DDL that was applied directly to the
-- live dev database outside any tracked migration (discovered by diffing an
-- exported Hasura metadata snapshot against a from-scratch replay of this
-- directory -- see deployments/hasura-metadata/metadata.json and
-- deployments/db-bootstrap/). Column/constraint definitions below were read
-- directly off the live database via Hasura's run_sql admin API.

-- ── 4 tables that exist live but were never captured in a migration ─────────

CREATE TABLE IF NOT EXISTS hospilot.staff (
    id                      uuid NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    role                    character varying,
    department              character varying,
    speciality              text,
    on_duty_status          character varying DEFAULT 'off-duty',
    requires_certification  boolean DEFAULT false,
    is_certified            boolean DEFAULT false,
    is_cross_trained        boolean DEFAULT false,
    branch_id               uuid,
    synced_at               timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.ventilator (
    id          uuid NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    bed_id      uuid,
    status      character varying,
    branch_id   uuid,
    synced_at   timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS hospilot.change_queue (
    resource_type   text NOT NULL,
    resource_id     text NOT NULL,
    table_name      text NOT NULL,
    operation       text NOT NULL CHECK (operation = ANY (ARRAY['PUT'::text, 'DELETE'::text])),
    raw_data        jsonb,
    changed_at      timestamp with time zone NOT NULL DEFAULT now(),
    snapshot_state  text NOT NULL DEFAULT 'pending' CHECK (snapshot_state = ANY (ARRAY['pending'::text, 'in_flight'::text])),
    PRIMARY KEY (resource_type, resource_id)
);

CREATE TABLE IF NOT EXISTS hospilot.fabric_approval_queue (
    change_id       text NOT NULL PRIMARY KEY,
    snapshot_id     text NOT NULL,
    method          text NOT NULL,
    url             text NOT NULL,
    entry_json      jsonb NOT NULL,
    approval_state  text NOT NULL DEFAULT 'pending' CHECK (approval_state = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text])),
    reason          text,
    created_at      timestamp with time zone NOT NULL DEFAULT now(),
    decided_at      timestamp with time zone,
    origin          text DEFAULT 'http',
    entity          text,
    change_type     text,
    record_id       text,
    envelope_json   jsonb
);

-- ── primary keys present live but missing from this migration history ───────

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'departments_pkey') THEN
    ALTER TABLE hospilot.departments ADD CONSTRAINT departments_pkey PRIMARY KEY (id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'insurance_contracts_pkey') THEN
    ALTER TABLE hospilot.insurance_contracts ADD CONSTRAINT insurance_contracts_pkey PRIMARY KEY (id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claims_pkey') THEN
    ALTER TABLE hospilot.claims ADD CONSTRAINT claims_pkey PRIMARY KEY (id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'lab_orders_pkey') THEN
    ALTER TABLE hospilot.lab_orders ADD CONSTRAINT lab_orders_pkey PRIMARY KEY (id);
  END IF;
END $$;

-- ── foreign keys present live but missing from this migration history ───────

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ipd_admissions_bed_id_fkey') THEN
    ALTER TABLE hospilot.ipd_admissions ADD CONSTRAINT ipd_admissions_bed_id_fkey
      FOREIGN KEY (bed_id) REFERENCES hospilot.beds(id) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ipd_admissions_department_id_fkey') THEN
    ALTER TABLE hospilot.ipd_admissions ADD CONSTRAINT ipd_admissions_department_id_fkey
      FOREIGN KEY (department_id) REFERENCES hospilot.departments(id) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'contract_service_rates_contract_id_fkey') THEN
    ALTER TABLE hospilot.contract_service_rates ADD CONSTRAINT contract_service_rates_contract_id_fkey
      FOREIGN KEY (contract_id) REFERENCES hospilot.insurance_contracts(id) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claim_history_claim_id_fkey') THEN
    ALTER TABLE hospilot.claim_history ADD CONSTRAINT claim_history_claim_id_fkey
      FOREIGN KEY (claim_id) REFERENCES hospilot.claims(id) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'visits_department_id_fkey') THEN
    ALTER TABLE hospilot.visits ADD CONSTRAINT visits_department_id_fkey
      FOREIGN KEY (department_id) REFERENCES hospilot.departments(id) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claim_line_items_claim_id_fkey') THEN
    ALTER TABLE hospilot.claim_line_items ADD CONSTRAINT claim_line_items_claim_id_fkey
      FOREIGN KEY (claim_id) REFERENCES hospilot.claims(id) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'claim_queries_claim_id_fkey') THEN
    ALTER TABLE hospilot.claim_queries ADD CONSTRAINT claim_queries_claim_id_fkey
      FOREIGN KEY (claim_id) REFERENCES hospilot.claims(id) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'lab_results_order_id_fkey') THEN
    ALTER TABLE hospilot.lab_results ADD CONSTRAINT lab_results_order_id_fkey
      FOREIGN KEY (order_id) REFERENCES hospilot.lab_orders(id) NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sessions_user_id_fkey') THEN
    ALTER TABLE hospilot_app.sessions ADD CONSTRAINT sessions_user_id_fkey
      FOREIGN KEY (user_id) REFERENCES hospilot_app.users(id) NOT VALID;
  END IF;
END $$;
