-- Lab Agent mirror tables
CREATE TABLE IF NOT EXISTS hospilot_lab_orders (
  id UUID PRIMARY KEY,
  visit_id UUID,
  patient_token UUID,
  ordered_by UUID,
  status VARCHAR(30) NOT NULL DEFAULT 'Pending',
  priority VARCHAR(20) DEFAULT 'Routine',
  ordered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ,
  tat_minutes INTEGER,
  is_overdue BOOLEAN DEFAULT FALSE,
  synced_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hospilot_lab_results (
  id UUID PRIMARY KEY,
  order_id UUID,
  patient_token UUID,
  test_name VARCHAR(255) NOT NULL,
  test_code VARCHAR(50),
  result_value VARCHAR(100),
  flag VARCHAR(20) DEFAULT 'Normal',
  reference_range VARCHAR(100),
  unit VARCHAR(50),
  reported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  synced_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed dummy data using a DO block that looks up real patient/visit IDs
DO $$
DECLARE
  v_p1 UUID; v_p2 UUID; v_p3 UUID;
  v_v1 UUID; v_v2 UUID; v_v3 UUID;
  v_o1 UUID; v_o2 UUID; v_o3 UUID; v_o4 UUID;
BEGIN
  SELECT id INTO v_p1 FROM patients LIMIT 1 OFFSET 0;
  SELECT id INTO v_p2 FROM patients LIMIT 1 OFFSET 1;
  SELECT id INTO v_p3 FROM patients LIMIT 1 OFFSET 2;

  SELECT id INTO v_v1 FROM visits WHERE patient_id = v_p1 LIMIT 1;
  SELECT id INTO v_v2 FROM visits WHERE patient_id = v_p2 LIMIT 1;
  SELECT id INTO v_v3 FROM visits WHERE patient_id = v_p3 LIMIT 1;

  v_o1 := gen_random_uuid();
  v_o2 := gen_random_uuid();
  v_o3 := gen_random_uuid();
  v_o4 := gen_random_uuid();

  -- Pending STAT order (150 min old — overdue)
  INSERT INTO hospilot_lab_orders VALUES (v_o1, v_v1, v_p1, NULL, 'Pending', 'STAT',
    NOW() - INTERVAL '150 minutes', NULL, 150, TRUE, NOW()) ON CONFLICT DO NOTHING;

  -- In Progress Routine (45 min old — not overdue)
  INSERT INTO hospilot_lab_orders VALUES (v_o2, v_v2, v_p2, NULL, 'In Progress', 'Routine',
    NOW() - INTERVAL '45 minutes', NULL, 45, FALSE, NOW()) ON CONFLICT DO NOTHING;

  -- Pending Urgent (95 min old — overdue)
  INSERT INTO hospilot_lab_orders VALUES (v_o3, v_v3, v_p3, NULL, 'Pending', 'Urgent',
    NOW() - INTERVAL '95 minutes', NULL, 95, TRUE, NOW()) ON CONFLICT DO NOTHING;

  -- Completed order (results available)
  INSERT INTO hospilot_lab_orders VALUES (v_o4, v_v1, v_p1, NULL, 'Completed', 'Routine',
    NOW() - INTERVAL '3 hours', NOW() - INTERVAL '30 minutes', 150, FALSE, NOW()) ON CONFLICT DO NOTHING;

  -- Results for completed order — one Critical, one High, one Normal
  INSERT INTO hospilot_lab_results VALUES
    (gen_random_uuid(), v_o4, v_p1, 'Serum Potassium', 'K-001', '6.8', 'Critical', '3.5-5.0', 'mEq/L', NOW() - INTERVAL '30 minutes', NOW()),
    (gen_random_uuid(), v_o4, v_p1, 'Hemoglobin', 'HGB-001', '7.2', 'Low', '12.0-17.5', 'g/dL', NOW() - INTERVAL '30 minutes', NOW()),
    (gen_random_uuid(), v_o4, v_p1, 'Fasting Blood Glucose', 'GLU-001', '92', 'Normal', '70-100', 'mg/dL', NOW() - INTERVAL '30 minutes', NOW()),
    (gen_random_uuid(), v_o2, v_p2, 'Creatinine', 'CREAT-001', '4.1', 'Critical', '0.7-1.3', 'mg/dL', NOW() - INTERVAL '20 minutes', NOW()),
    (gen_random_uuid(), v_o2, v_p2, 'Blood Urea Nitrogen', 'BUN-001', '38', 'High', '7-20', 'mg/dL', NOW() - INTERVAL '20 minutes', NOW())
  ON CONFLICT DO NOTHING;

END $$;
