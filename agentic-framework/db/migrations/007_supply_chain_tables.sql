-- Supply Chain Agent: non-pharma supply inventory tracking
CREATE TABLE IF NOT EXISTS hospilot_supplies (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  item_code VARCHAR(50) UNIQUE NOT NULL,
  item_name VARCHAR(255) NOT NULL,
  category VARCHAR(100) NOT NULL,
  current_stock DECIMAL(12,2) DEFAULT 0,
  min_stock DECIMAL(12,2) DEFAULT 0,
  unit VARCHAR(50) NOT NULL,
  unit_cost DECIMAL(10,2) DEFAULT 0,
  last_ordered_at TIMESTAMPTZ,
  last_received_at TIMESTAMPTZ,
  synced_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed realistic supply data (some below minimum to trigger alerts)
INSERT INTO hospilot_supplies (item_code, item_name, category, current_stock, min_stock, unit, unit_cost) VALUES
  ('PPE-001', 'Nitrile Examination Gloves (M)', 'PPE', 200, 500, 'boxes', 450.00),
  ('PPE-002', 'N95 Respirator Masks', 'PPE', 30, 100, 'boxes', 1200.00),
  ('PPE-003', 'Surgical Gowns (L)', 'PPE', 80, 150, 'units', 85.00),
  ('PPE-004', 'Face Shields', 'PPE', 45, 50, 'units', 120.00),
  ('MED-001', 'IV Cannula 18G', 'Medical Supplies', 500, 300, 'units', 18.00),
  ('MED-002', 'IV Cannula 22G', 'Medical Supplies', 120, 300, 'units', 18.00),
  ('MED-003', 'Sterile Dressing 10x10cm', 'Medical Supplies', 800, 500, 'units', 25.00),
  ('MED-004', 'Surgical Sutures 2-0', 'Medical Supplies', 15, 50, 'boxes', 320.00),
  ('MED-005', 'Foley Catheter 16Fr', 'Medical Supplies', 60, 80, 'units', 95.00),
  ('MED-006', 'Nasogastric Tubes 14Fr', 'Medical Supplies', 8, 30, 'units', 75.00),
  ('LIN-001', 'Hospital Bed Sheets', 'Linen', 90, 200, 'units', 180.00),
  ('LIN-002', 'Patient Gowns', 'Linen', 40, 100, 'units', 120.00),
  ('LIN-003', 'Pillow Covers', 'Linen', 60, 150, 'units', 45.00),
  ('EQP-001', 'Pulse Oximeter Probes', 'Equipment', 25, 40, 'units', 850.00),
  ('EQP-002', 'ECG Electrode Patches', 'Equipment', 300, 200, 'units', 12.00)
ON CONFLICT (item_code) DO UPDATE SET
  current_stock = EXCLUDED.current_stock,
  synced_at = NOW();
