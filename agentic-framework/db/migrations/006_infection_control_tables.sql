CREATE TABLE IF NOT EXISTS hospilot_infection_cases (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  patient_token UUID NOT NULL,
  admission_id UUID,
  ward VARCHAR(100) NOT NULL,
  pathogen VARCHAR(100) NOT NULL,
  severity VARCHAR(20) NOT NULL DEFAULT 'medium',
  isolation_required BOOLEAN DEFAULT TRUE,
  isolation_confirmed BOOLEAN DEFAULT FALSE,
  isolation_room VARCHAR(50),
  status VARCHAR(20) DEFAULT 'active',
  reported_at TIMESTAMPTZ DEFAULT NOW(),
  resolved_at TIMESTAMPTZ,
  notes TEXT,
  synced_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed dummy infection cases using real patient tokens
DO $$
DECLARE
  v_p1 UUID; v_p2 UUID; v_p3 UUID; v_p4 UUID; v_p5 UUID;
  v_a1 UUID; v_a2 UUID; v_a3 UUID; v_a4 UUID; v_a5 UUID;
BEGIN
  SELECT id INTO v_p1 FROM patients LIMIT 1 OFFSET 0;
  SELECT id INTO v_p2 FROM patients LIMIT 1 OFFSET 1;
  SELECT id INTO v_p3 FROM patients LIMIT 1 OFFSET 2;
  SELECT id INTO v_p4 FROM patients LIMIT 1 OFFSET 3;
  SELECT id INTO v_p5 FROM patients LIMIT 1 OFFSET 4;

  SELECT id INTO v_a1 FROM admissions WHERE patient_token = v_p1 LIMIT 1;
  SELECT id INTO v_a2 FROM admissions WHERE patient_token = v_p2 LIMIT 1;
  SELECT id INTO v_a3 FROM admissions WHERE patient_token = v_p3 LIMIT 1;
  SELECT id INTO v_a4 FROM admissions WHERE patient_token = v_p4 LIMIT 1;
  SELECT id INTO v_a5 FROM admissions WHERE patient_token = v_p5 LIMIT 1;

  INSERT INTO hospilot_infection_cases (patient_token, admission_id, ward, pathogen, severity, isolation_required, isolation_confirmed, status, reported_at) VALUES
    (v_p1, v_a1, 'ICU Ward A', 'MRSA', 'high',   TRUE,  FALSE, 'active',       NOW() - INTERVAL '2 days'),
    (v_p2, v_a2, 'ICU Ward A', 'MRSA', 'high',   TRUE,  TRUE,  'active',       NOW() - INTERVAL '1 day'),
    (v_p3, v_a3, 'General Ward B', 'C. difficile', 'medium', TRUE, FALSE, 'active', NOW() - INTERVAL '6 hours'),
    (v_p4, v_a4, 'General Ward B', 'C. difficile', 'medium', TRUE, FALSE, 'active', NOW() - INTERVAL '3 hours'),
    (v_p5, v_a5, 'Surgical Ward', 'E. coli (ESBL)', 'high', TRUE, TRUE, 'active', NOW() - INTERVAL '12 hours'),
    (v_p1, NULL, 'General Ward C', 'Pseudomonas aeruginosa', 'medium', TRUE, FALSE, 'under_review', NOW() - INTERVAL '1 day'),
    (v_p2, NULL, 'Paediatric Ward', 'RSV', 'low', FALSE, FALSE, 'active', NOW() - INTERVAL '4 hours'),
    (v_p3, NULL, 'Medical Ward', 'COVID-19', 'medium', TRUE, TRUE, 'active', NOW() - INTERVAL '2 days')
  ON CONFLICT DO NOTHING;
END $$;
