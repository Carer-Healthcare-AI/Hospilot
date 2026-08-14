# Example Q&A — Ask Hospilot

These examples set the quality bar: answers must be **grounded in the database**,
show **how they were derived**, and **refuse** when the schema cannot support an answer.

Assumptions for this seed: roster "tonight" = night shift on **2026-08-13**.
A free bed means `status = 'free'` and `is_active = 1`. Cleaning/maintenance beds are **not** free.

---

## Q1 — Free ICU beds

**Question:** How many ICU beds are free right now?

**Good answer:**
There are **4** free ICU beds right now (3 in ICU-A and 1 in ICU-B). Beds that are cleaning or under maintenance are not counted as free.

**Why this is good:** Exact count from `beds` where `ward_type = 'ICU'` and `status = 'free'`. Explains the definition used.

**Reference SQL:**
```sql
SELECT COUNT(*) AS free_icu_beds
FROM beds
WHERE ward_type = 'ICU' AND status = 'free' AND is_active = 1;
```

---

## Q2 — Short-staffed wards tonight

**Question:** Which wards are short-staffed tonight?

**Good answer:**
On the night shift for 2026-08-13, these wards are short-staffed (actual headcount below required):

| Ward | Role | Required | Actual | Shortfall |
|------|------|----------|--------|-----------|
| Intensive Care Unit A (ICU-A) | nurse | 4 | 2 | 2 |
| General Ward 2 East (GEN-2E) | nurse | 5 | 3 | 2 |
| Pediatric Ward (PED-1) | nurse | 3 | 2 | 1 |
| Emergency Bay (ER) | doctor | 2 | 1 | 1 |

ICU-B, GEN-2W, and Maternity are fully staffed for night shift.

**Why this is good:** Lists concrete shortfalls, not a vague "several wards." Uses roster data, not invention.

**Reference SQL:**
```sql
SELECT ward_name, ward_code, role, required_headcount, actual_headcount, shortfall
FROM v_staffing_gaps
WHERE shift = 'night' AND roster_date = '2026-08-13'
ORDER BY shortfall DESC, ward_code;
```

---

## Q3 — Occupancy for a specific ward

**Question:** What's the bed occupancy in General Ward 2 East?

**Good answer:**
GEN-2E has 10 active beds: **7 occupied**, 2 free, and 1 being cleaned. Occupancy of assignable occupied beds is 7/10 (70%). If you only count beds that are free or occupied (excluding cleaning), 7 of 9 are occupied (~78%).

**Reference SQL:**
```sql
SELECT status, COUNT(*) AS bed_count
FROM beds
WHERE ward_id = (SELECT id FROM wards WHERE code = 'GEN-2E') AND is_active = 1
GROUP BY status;
```

---

## Q4 — Current inpatients expected to leave soon

**Question:** Which admitted patients are expected to be discharged today?

**Good answer:**
Two current admissions have `expected_discharge_at` on 2026-08-13:

1. **Fatima Begum** (UHID-1007) — General Ward 2 East, bed 2E-01 — expected ~14:00 — Pneumonia  
2. **Arjun Patel** (UHID-1008) — Pediatric Ward, bed PED-01 — expected ~16:00 — Asthma exacerbation  
3. **Imran Sheikh** (UHID-1004) — ICU-A, bed ICU-A-07 — expected ~18:00 — Acute MI

**Reference SQL:**
```sql
SELECT uhid, first_name, last_name, ward_name, bed_number, expected_discharge_at, diagnosis
FROM v_current_inpatients
WHERE date(expected_discharge_at) = '2026-08-13'
ORDER BY expected_discharge_at;
```

---

## Q5 — Unanswerable (must refuse)

**Question:** What was the average patient satisfaction score last month?

**Good answer:**
I can't answer that from the available hospital data. The database has beds, admissions, visits, patients, and staff rosters — but **no patient satisfaction or survey scores**.

**Why this is good:** Explicit refusal. Does not invent a number or "typical" score.

---

## Q6 — Phrasing robustness (same meaning as Q1)

**Question:** Right now, how many intensive-care beds do we have available?

**Good answer:** Same as Q1 — **4** free ICU beds. Different wording, same grounded count.
