-- One-time backfill: copy public schema data into hospilot schema tables
-- Run in Hasura SQL console: http://192.46.212.81:8080/console/data/default/sql

-- ── Claims ────────────────────────────────────────────────────────────────────

INSERT INTO hospilot.claims (id,patient_token,visit_id,tpa_id,tpa_name,claim_amount,status,created_at,submitted_date,approved_amount,denial_reason,claim_number,payer_type,risk_level,risk_score,stage,compliance_status,diagnosis_code,branch_id)
SELECT id,CAST(patient_id AS text),CAST(visit_id AS text),CAST(tpa_id AS text),tpa_name,claim_amount,status,created_at,submitted_date,approved_amount,denial_reason,claim_number,payer_type,risk_level,risk_score,stage,compliance_status,diagnosis_code,branch_id
FROM public.claims
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot.claim_line_items (id,claim_id,service_code,service_name,description,quantity,rate,amount,approved_amount,approved_quantity,approved_rate,status,category,unit,rejection_reason)
SELECT id,claim_id,service_code,service_name,description,quantity,rate,amount,approved_amount,approved_quantity,approved_rate,status,category,unit,rejection_reason
FROM public.claim_line_items
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot.claim_history (id,claim_id,from_status,to_status,action,changed_at,changed_by,remarks)
SELECT id,claim_id,from_status,to_status,action,changed_at,changed_by,remarks
FROM public.claim_history
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot.claim_queries (id,claim_id,query_type,query_text,status,raised_at,raised_by,responded_by,response_date,response_text,created_at)
SELECT id,claim_id,query_type,query_text,status,raised_at,raised_by,responded_by,response_date,response_text,created_at
FROM public.claim_queries
ON CONFLICT (id) DO NOTHING;

-- ── Insurance / Billing reference ─────────────────────────────────────────────

INSERT INTO hospilot.insurance_contracts (id,insurer_name,tpa_name,contract_type,contract_number,start_date,end_date,status,branch_id,total_claims,approved_amount,rejection_rate,avg_settlement_days)
SELECT id,insurer_name,tpa_name,contract_type,contract_number,start_date,end_date,status,branch_id,total_claims,approved_amount,rejection_rate,avg_settlement_days
FROM public.insurance_contracts
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot.contract_service_rates (id,contract_id,service_id,service_code,service_name,contract_rate,hospital_rate,discount_percentage,is_active)
SELECT id,contract_id,service_id,service_code,service_name,contract_rate,hospital_rate,discount_percentage,is_active
FROM public.contract_service_rates
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot.invoice_line_items (id,invoice_id,service_id,service_code,service_name,description,quantity,rate,amount,total,gst_rate,gst_amount,discount_amount,source_type,source_id)
SELECT id,invoice_id,service_id,service_code,service_name,description,quantity,rate,amount,total,gst_rate,gst_amount,discount_amount,source_type,source_id
FROM public.invoice_line_items
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot.payment_entries (id,payment_id,payment_mode,amount,transaction_reference,bank_name,card_last_four,created_at)
SELECT id,payment_id,payment_mode,amount,transaction_reference,bank_name,card_last_four,created_at
FROM public.payment_entries
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot.refunds (id,invoice_id,payment_id,refund_amount,reason,status,refund_date,refund_mode,refund_number,created_at)
SELECT id,invoice_id,payment_id,refund_amount,reason,status,refund_date,refund_mode,refund_number,created_at
FROM public.refunds
ON CONFLICT (id) DO NOTHING;

INSERT INTO hospilot.payment_reconciliation (id,reconciliation_date,total_expected,total_actual,total_variance,actual_cash,actual_card,actual_upi,actual_bank,cash_variance,card_variance,upi_variance,bank_variance,status,created_at)
SELECT id,reconciliation_date,total_expected,total_actual,total_variance,actual_cash,actual_card,actual_upi,actual_bank,cash_variance,card_variance,upi_variance,bank_variance,status,created_at
FROM public.payment_reconciliation
ON CONFLICT (id) DO NOTHING;

-- ── Lab ───────────────────────────────────────────────────────────────────────
-- Note: public.lab_orders has no patient_id/priority/completed_at columns

INSERT INTO hospilot.lab_orders (id,visit_id,ordered_by,status,ordered_at)
SELECT id,visit_id,ordered_by,status,created_at
FROM public.lab_orders
ON CONFLICT (id) DO NOTHING;

-- Note: public.lab_results has no test_name/code/reference_range/unit columns
-- order_id left NULL — public uses order_item_id (different FK)

INSERT INTO hospilot.lab_results (id,patient_token,result_value,flag,reported_at)
SELECT id,CAST(patient_id AS text),result_value,flag,created_at
FROM public.lab_results
ON CONFLICT (id) DO NOTHING;

-- ── Patients ──────────────────────────────────────────────────────────────────

INSERT INTO hospilot.patients (id,first_name,last_name,uhid)
SELECT id,first_name,last_name,uhid
FROM public.patients
ON CONFLICT (id) DO NOTHING;

-- ── OT Surgeries ──────────────────────────────────────────────────────────────

INSERT INTO hospilot.ot_surgeries (id,admission_id,patient_token,status,created_at)
SELECT id,admission_id,CAST(patient_id AS text),status,created_at
FROM public.ot_surgeries
ON CONFLICT (id) DO NOTHING;

-- ── Purchase Orders ───────────────────────────────────────────────────────────

INSERT INTO hospilot.purchase_orders (id,po_number,vendor_id,status,total,order_date,expected_delivery,created_at)
SELECT id,po_number,vendor_id,status,total,order_date,expected_delivery,created_at
FROM public.purchase_orders
ON CONFLICT (id) DO NOTHING;
