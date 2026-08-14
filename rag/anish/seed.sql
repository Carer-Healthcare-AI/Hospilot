-- Seed data for Ask Hospilot (deterministic answers for example-qa.md)
-- "Today" for roster purposes: 2026-08-13
-- Night shift = tonight

BEGIN;

INSERT INTO wards (id, code, name, ward_type, floor, total_beds) VALUES
  (1, 'ICU-A',  'Intensive Care Unit A', 'ICU',        3, 8),
  (2, 'ICU-B',  'Intensive Care Unit B', 'ICU',        3, 4),
  (3, 'GEN-2E', 'General Ward 2 East',   'General',    2, 10),
  (4, 'GEN-2W', 'General Ward 2 West',   'General',    2, 8),
  (5, 'MAT-1',  'Maternity Ward',        'Maternity',  1, 6),
  (6, 'PED-1',  'Pediatric Ward',        'Pediatric',  1, 6),
  (7, 'ER',     'Emergency Bay',         'Emergency',  0, 6);

-- ICU-A: 8 beds → 3 free, 4 occupied, 1 cleaning
INSERT INTO beds (id, ward_id, bed_number, ward_type, status, is_active) VALUES
  (1,  1, 'ICU-A-01', 'ICU', 'occupied', 1),
  (2,  1, 'ICU-A-02', 'ICU', 'occupied', 1),
  (3,  1, 'ICU-A-03', 'ICU', 'free', 1),
  (4,  1, 'ICU-A-04', 'ICU', 'occupied', 1),
  (5,  1, 'ICU-A-05', 'ICU', 'free', 1),
  (6,  1, 'ICU-A-06', 'ICU', 'cleaning', 1),
  (7,  1, 'ICU-A-07', 'ICU', 'occupied', 1),
  (8,  1, 'ICU-A-08', 'ICU', 'free', 1);

-- ICU-B: 4 beds → 1 free, 2 occupied, 1 maintenance
INSERT INTO beds (id, ward_id, bed_number, ward_type, status, is_active) VALUES
  (9,  2, 'ICU-B-01', 'ICU', 'occupied', 1),
  (10, 2, 'ICU-B-02', 'ICU', 'free', 1),
  (11, 2, 'ICU-B-03', 'ICU', 'occupied', 1),
  (12, 2, 'ICU-B-04', 'ICU', 'maintenance', 1);

-- General 2E: 10 beds → 2 free, 7 occupied, 1 cleaning
INSERT INTO beds (id, ward_id, bed_number, ward_type, status, is_active) VALUES
  (13, 3, '2E-01', 'General', 'occupied', 1),
  (14, 3, '2E-02', 'General', 'occupied', 1),
  (15, 3, '2E-03', 'General', 'occupied', 1),
  (16, 3, '2E-04', 'General', 'free', 1),
  (17, 3, '2E-05', 'General', 'occupied', 1),
  (18, 3, '2E-06', 'General', 'occupied', 1),
  (19, 3, '2E-07', 'General', 'cleaning', 1),
  (20, 3, '2E-08', 'General', 'occupied', 1),
  (21, 3, '2E-09', 'General', 'occupied', 1),
  (22, 3, '2E-10', 'General', 'free', 1);

-- General 2W: 8 beds → 4 free, 4 occupied
INSERT INTO beds (id, ward_id, bed_number, ward_type, status, is_active) VALUES
  (23, 4, '2W-01', 'General', 'occupied', 1),
  (24, 4, '2W-02', 'General', 'free', 1),
  (25, 4, '2W-03', 'General', 'occupied', 1),
  (26, 4, '2W-04', 'General', 'free', 1),
  (27, 4, '2W-05', 'General', 'occupied', 1),
  (28, 4, '2W-06', 'General', 'free', 1),
  (29, 4, '2W-07', 'General', 'occupied', 1),
  (30, 4, '2W-08', 'General', 'free', 1);

-- Maternity: 6 beds → 3 free, 3 occupied
INSERT INTO beds (id, ward_id, bed_number, ward_type, status, is_active) VALUES
  (31, 5, 'MAT-01', 'Maternity', 'occupied', 1),
  (32, 5, 'MAT-02', 'Maternity', 'free', 1),
  (33, 5, 'MAT-03', 'Maternity', 'occupied', 1),
  (34, 5, 'MAT-04', 'Maternity', 'free', 1),
  (35, 5, 'MAT-05', 'Maternity', 'occupied', 1),
  (36, 5, 'MAT-06', 'Maternity', 'free', 1);

-- Pediatric: 6 beds → 1 free, 5 occupied
INSERT INTO beds (id, ward_id, bed_number, ward_type, status, is_active) VALUES
  (37, 6, 'PED-01', 'Pediatric', 'occupied', 1),
  (38, 6, 'PED-02', 'Pediatric', 'occupied', 1),
  (39, 6, 'PED-03', 'Pediatric', 'occupied', 1),
  (40, 6, 'PED-04', 'Pediatric', 'occupied', 1),
  (41, 6, 'PED-05', 'Pediatric', 'free', 1),
  (42, 6, 'PED-06', 'Pediatric', 'occupied', 1);

-- ER: 6 beds → 2 free, 3 occupied, 1 cleaning
INSERT INTO beds (id, ward_id, bed_number, ward_type, status, is_active) VALUES
  (43, 7, 'ER-01', 'Emergency', 'occupied', 1),
  (44, 7, 'ER-02', 'Emergency', 'free', 1),
  (45, 7, 'ER-03', 'Emergency', 'occupied', 1),
  (46, 7, 'ER-04', 'Emergency', 'cleaning', 1),
  (47, 7, 'ER-05', 'Emergency', 'occupied', 1),
  (48, 7, 'ER-06', 'Emergency', 'free', 1);

-- Totals check:
-- ICU free = ICU-A(3) + ICU-B(1) = 4
-- All free = 3+1+2+4+3+1+2 = 16

INSERT INTO patients (id, uhid, first_name, last_name, date_of_birth, sex) VALUES
  (1,  'UHID-1001', 'Asha',     'Verma',   '1978-04-12', 'F'),
  (2,  'UHID-1002', 'Ravi',     'Kumar',   '1965-11-03', 'M'),
  (3,  'UHID-1003', 'Meera',    'Nair',    '1990-07-21', 'F'),
  (4,  'UHID-1004', 'Imran',    'Sheikh',  '1982-01-30', 'M'),
  (5,  'UHID-1005', 'Priya',    'Das',     '1995-09-08', 'F'),
  (6,  'UHID-1006', 'Joseph',   'Thomas',  '1958-12-15', 'M'),
  (7,  'UHID-1007', 'Fatima',   'Begum',   '1988-03-19', 'F'),
  (8,  'UHID-1008', 'Arjun',    'Patel',   '2018-06-02', 'M'),
  (9,  'UHID-1009', 'Sneha',    'Reddy',   '1992-10-11', 'F'),
  (10, 'UHID-1010', 'Karan',    'Singh',   '1971-05-25', 'M'),
  (11, 'UHID-1011', 'Lakshmi',  'Iyer',    '1985-08-14', 'F'),
  (12, 'UHID-1012', 'Omar',     'Hassan',  '1999-02-28', 'M');

-- Active admissions on occupied beds (subset of occupied beds)
INSERT INTO admissions (id, patient_id, bed_id, ward_id, admitted_at, expected_discharge_at, discharged_at, status, diagnosis) VALUES
  (1,  1,  1,  1, '2026-08-10T08:00:00', '2026-08-14T10:00:00', NULL, 'admitted', 'Septic shock'),
  (2,  2,  2,  1, '2026-08-11T14:30:00', '2026-08-15T12:00:00', NULL, 'admitted', 'ARDS'),
  (3,  3,  4,  1, '2026-08-12T09:15:00', '2026-08-16T09:00:00', NULL, 'admitted', 'Post-op monitoring'),
  (4,  4,  7,  1, '2026-08-09T22:00:00', '2026-08-13T18:00:00', NULL, 'admitted', 'Acute MI'),
  (5,  5,  9,  2, '2026-08-11T11:00:00', '2026-08-14T11:00:00', NULL, 'admitted', 'Diabetic ketoacidosis'),
  (6,  6, 11,  2, '2026-08-12T16:45:00', '2026-08-17T10:00:00', NULL, 'admitted', 'Stroke'),
  (7,  7, 13,  3, '2026-08-08T10:00:00', '2026-08-13T14:00:00', NULL, 'admitted', 'Pneumonia'),
  (8,  9, 14,  3, '2026-08-10T13:20:00', '2026-08-14T09:00:00', NULL, 'admitted', 'Cellulitis'),
  (9, 10, 15,  3, '2026-08-11T07:40:00', '2026-08-15T08:00:00', NULL, 'admitted', 'CHF exacerbation'),
  (10, 11, 31, 5, '2026-08-12T05:00:00', '2026-08-14T12:00:00', NULL, 'admitted', 'Postpartum recovery'),
  (11,  8, 37, 6, '2026-08-11T19:00:00', '2026-08-13T16:00:00', NULL, 'admitted', 'Asthma exacerbation'),
  (12, 12, 43, 7, '2026-08-13T06:30:00', NULL,                   NULL, 'admitted', 'Trauma - observation');

INSERT INTO visits (id, patient_id, ward_id, visit_type, chief_complaint, status, arrived_at, completed_at) VALUES
  (1, 12, 7, 'Emergency', 'Road traffic accident', 'in_progress', '2026-08-13T06:20:00', NULL),
  (2,  3, 3, 'OPD',       'Follow-up wound check', 'completed',   '2026-08-12T09:00:00', '2026-08-12T10:15:00'),
  (3, 10, 3, 'OPD',       'Shortness of breath',   'waiting',     '2026-08-13T08:45:00', NULL),
  (4,  8, 6, 'Emergency', 'Wheezing',              'completed',   '2026-08-11T18:30:00', '2026-08-11T19:10:00'),
  (5,  5, 7, 'Emergency', 'High blood sugar',      'completed',   '2026-08-11T09:50:00', '2026-08-11T11:05:00');

-- Night shift tonight = 2026-08-13 night
-- Short-staffed (actual < required):
--   ICU-A nurses: need 4 have 2 (shortfall 2)
--   GEN-2E nurses: need 5 have 3 (shortfall 2)
--   PED-1 nurses: need 3 have 2 (shortfall 1)
--   ER doctors: need 2 have 1 (shortfall 1)
-- Adequately staffed:
--   ICU-B nurses: 2/2
--   GEN-2W nurses: 4/4
--   MAT-1 nurses: 3/3

INSERT INTO staff_roster (id, ward_id, role, shift, roster_date, required_headcount, actual_headcount) VALUES
  (1,  1, 'nurse',  'night', '2026-08-13', 4, 2),
  (2,  1, 'doctor', 'night', '2026-08-13', 1, 1),
  (3,  2, 'nurse',  'night', '2026-08-13', 2, 2),
  (4,  2, 'doctor', 'night', '2026-08-13', 1, 1),
  (5,  3, 'nurse',  'night', '2026-08-13', 5, 3),
  (6,  3, 'doctor', 'night', '2026-08-13', 1, 1),
  (7,  4, 'nurse',  'night', '2026-08-13', 4, 4),
  (8,  4, 'doctor', 'night', '2026-08-13', 1, 1),
  (9,  5, 'nurse',  'night', '2026-08-13', 3, 3),
  (10, 5, 'doctor', 'night', '2026-08-13', 1, 1),
  (11, 6, 'nurse',  'night', '2026-08-13', 3, 2),
  (12, 6, 'doctor', 'night', '2026-08-13', 1, 1),
  (13, 7, 'nurse',  'night', '2026-08-13', 4, 4),
  (14, 7, 'doctor', 'night', '2026-08-13', 2, 1),
  -- Morning shift same day (for phrasing robustness / contrast)
  (15, 1, 'nurse',  'morning', '2026-08-13', 4, 4),
  (16, 3, 'nurse',  'morning', '2026-08-13', 5, 5),
  (17, 7, 'doctor', 'morning', '2026-08-13', 3, 3);

COMMIT;
