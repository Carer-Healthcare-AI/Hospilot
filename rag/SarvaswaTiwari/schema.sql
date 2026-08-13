CREATE TABLE IF NOT EXISTS beds (
  id TEXT PRIMARY KEY,
  branch_id TEXT,
  ward TEXT NOT NULL,
  bed_number TEXT NOT NULL,
  room_type TEXT,
  status TEXT NOT NULL,
  is_active INTEGER NOT NULL CHECK (is_active IN (0,1)),
  ventilation TEXT,
  room_sharing TEXT,
  floor INTEGER,
  wing TEXT,
  natural_light INTEGER,
  noise_level TEXT,
  features TEXT
);

CREATE TABLE IF NOT EXISTS departments (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT,
  capacity INTEGER,
  target_occupancy_pct INTEGER
);

CREATE TABLE IF NOT EXISTS ipd_admissions (
  id TEXT PRIMARY KEY,
  patient_token TEXT,
  bed_id TEXT,
  department_id TEXT,
  admitted_at TEXT,
  expected_discharge_at TEXT,
  status TEXT,
  discharge_ready INTEGER,
  discharge_blocked_reason TEXT,
  transfer_pending INTEGER
);

CREATE TABLE IF NOT EXISTS staff_roster (
  id TEXT PRIMARY KEY,
  area TEXT,
  area_label TEXT,
  role TEXT,
  shift TEXT,
  headcount INTEGER,
  assigned_load INTEGER,
  load_per_staff INTEGER,
  branch_id TEXT
);

CREATE TABLE IF NOT EXISTS appointments (
  id TEXT PRIMARY KEY,
  patient_id TEXT,
  provider_id TEXT,
  department_id TEXT,
  appointment_time TEXT,
  status TEXT,
  type TEXT,
  patient_name TEXT,
  specialization TEXT,
  department_name TEXT
);

CREATE TABLE IF NOT EXISTS visits (
  id TEXT PRIMARY KEY,
  patient_token TEXT,
  department_id TEXT,
  arrived_at TEXT,
  status TEXT,
  chief_complaint TEXT,
  triage_score INTEGER,
  visit_type TEXT,
  appointment_id TEXT
);

CREATE TABLE IF NOT EXISTS supplies (
  id TEXT PRIMARY KEY,
  item_code TEXT,
  item_name TEXT,
  category TEXT,
  current_stock REAL,
  min_stock REAL,
  unit TEXT,
  unit_cost REAL
);

CREATE TABLE IF NOT EXISTS lab_orders (
  id TEXT PRIMARY KEY,
  visit_id TEXT,
  patient_token TEXT,
  ordered_by TEXT,
  status TEXT,
  priority TEXT,
  ordered_at TEXT,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS lab_results (
  id TEXT PRIMARY KEY,
  order_id TEXT,
  patient_token TEXT,
  test_name TEXT,
  test_code TEXT,
  result_value TEXT,
  flag TEXT,
  reference_range TEXT,
  unit TEXT,
  reported_at TEXT
);
