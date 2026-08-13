import os
import sqlite3
import uuid
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "hospilot.db")


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def seed_beds(db: sqlite3.Connection) -> dict[str, list[str]]:
    """Seed beds matching example-qa.md counts. Returns ward -> bed ids."""
    ward_statuses = {
        "Cardiology": {"Available": 2, "Occupied": 2, "Reserved": 4},
        "Emergency": {"Available": 4, "Occupied": 3, "Reserved": 6},
        "General Ward": {"Available": 4, "Occupied": 6, "Reserved": 9},
        "ICU": {"Available": 6, "Occupied": 17, "Reserved": 3, "Dirty": 4},
        "Orthopedics": {"Available": 1, "Occupied": 1, "Reserved": 6},
        "Pediatrics": {"Available": 2, "Occupied": 2, "Reserved": 4},
        "Private": {"Available": 2, "Occupied": 4, "Reserved": 2},
        "Semi-Private": {"Available": 1, "Occupied": 4, "Reserved": 1},
    }

    beds_by_ward: dict[str, list[str]] = {ward: [] for ward in ward_statuses}
    bed_num = 1

    for ward, statuses in ward_statuses.items():
        for status, count in statuses.items():
            for _ in range(count):
                bed_id = uid("bed")
                db.execute(
                    """INSERT INTO beds
                    (id, branch_id, ward, bed_number, room_type, status, is_active,
                     ventilation, room_sharing, floor, wing, natural_light, noise_level, features)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        bed_id,
                        "branch_demo",
                        ward,
                        f"B{bed_num:03d}",
                        "ICU" if ward == "ICU" else "Standard",
                        status,
                        1,
                        "Central" if ward == "ICU" else "Natural",
                        "Single" if ward == "Private" else "Shared",
                        1,
                        "A",
                        1,
                        "Low",
                        "Oxygen,Monitor" if ward == "ICU" else "Standard",
                    ),
                )
                beds_by_ward[ward].append(bed_id)
                bed_num += 1

    return beds_by_ward


def seed_admissions(db: sqlite3.Connection, beds_by_ward: dict[str, list[str]]) -> None:
    """Active admissions for occupancy ranking from example-qa.md example 2."""
    occupancy_targets = {
        "Semi-Private": 3,
        "General Ward": 6,
        "ICU": 8,
        "Private": 2,
    }

    now = datetime.now().isoformat(timespec="seconds")
    for ward, count in occupancy_targets.items():
        occupied_beds = [
            bed_id
            for bed_id in beds_by_ward[ward]
            if db.execute("SELECT status FROM beds WHERE id = ?", (bed_id,)).fetchone()[0]
            == "Occupied"
        ]
        for i in range(min(count, len(occupied_beds))):
            db.execute(
                """INSERT INTO ipd_admissions
                (id, patient_token, bed_id, department_id, admitted_at, expected_discharge_at,
                 status, discharge_ready, discharge_blocked_reason, transfer_pending)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    uid("adm"),
                    f"patient_{ward.lower().replace(' ', '_')}_{i}",
                    occupied_beds[i],
                    uid("dept"),
                    now,
                    (datetime.now() + timedelta(days=2)).isoformat(timespec="seconds"),
                    "admitted",
                    0,
                    None,
                    0,
                ),
            )


def seed() -> None:
    db = sqlite3.connect(DB_PATH)
    db.executescript(open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8").read())

    if db.execute("SELECT COUNT(*) FROM beds").fetchone()[0] == 0:
        beds_by_ward = seed_beds(db)
        seed_admissions(db, beds_by_ward)

    if db.execute("SELECT COUNT(*) FROM departments").fetchone()[0] == 0:
        for name, capacity in [
            ("ICU", 30),
            ("General Ward", 19),
            ("Private", 8),
            ("Semi-Private", 6),
            ("Emergency", 13),
            ("Cardiology", 8),
            ("Orthopedics", 8),
            ("Pediatrics", 8),
        ]:
            db.execute(
                "INSERT INTO departments VALUES (?, ?, ?, ?, ?)",
                (uid("dept"), name, "inpatient", capacity, 85),
            )

    if db.execute("SELECT COUNT(*) FROM staff_roster").fetchone()[0] == 0:
        rows = [
            ("ICU", "ICU", "Nurse", "Night", 8, 32, 4),
            ("ICU", "ICU", "Nurse", "Day", 10, 30, 3),
            ("Emergency", "Emergency", "Nurse", "Night", 6, 30, 5),
            ("General Ward", "General Ward", "Nurse", "Night", 8, 24, 3),
            ("OT", "Operating Theatre", "Technician", "Day", 4, 8, 2),
        ]
        for area, label, role, shift, headcount, load, lps in rows:
            db.execute(
                "INSERT INTO staff_roster VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uid("staff"), area, label, role, shift, headcount, load, lps, "branch_demo"),
            )

    if db.execute("SELECT COUNT(*) FROM supplies").fetchone()[0] == 0:
        rows = [
            ("SYR-001", "Syringes 5ml", "Consumables", 420, 500, "units", 2.5),
            ("GLO-001", "Sterile Gloves", "Consumables", 1200, 1000, "pairs", 8),
            ("OXY-001", "Oxygen Masks", "Respiratory", 35, 50, "units", 65),
            ("SAL-001", "Normal Saline 500ml", "Fluids", 80, 100, "bags", 45),
        ]
        for row in rows:
            db.execute("INSERT INTO supplies VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (uid("sup"), *row))

    if db.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0:
        now = datetime.now()
        rows = [
            ("Dr. Arjun", "Cardiology", "confirmed"),
            ("Dr. Meera", "Orthopedics", "confirmed"),
            ("Dr. Priya", "Ophthalmology", "completed"),
            ("Dr. Rahul", "General Surgery", "scheduled"),
        ]
        for i, (doc, spec, status) in enumerate(rows):
            t = (now + timedelta(hours=i + 1)).isoformat(timespec="minutes")
            db.execute(
                "INSERT INTO appointments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uid("appt"),
                    uid("patient"),
                    uid("provider"),
                    uid("dept"),
                    t,
                    status,
                    "outpatient",
                    f"Patient {i + 1}",
                    spec,
                    spec,
                ),
            )

    if db.execute("SELECT COUNT(*) FROM lab_orders").fetchone()[0] == 0:
        now = datetime.now().isoformat(timespec="seconds")
        for status, priority, count in [("pending", "routine", 5), ("in_progress", "urgent", 2)]:
            for _ in range(count):
                db.execute(
                    "INSERT INTO lab_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (uid("lab"), uid("visit"), uid("pt"), "Dr. Test", status, priority, now, None),
                )

    db.commit()
    db.close()


if __name__ == "__main__":
    seed()
    print(f"Seeded database at {DB_PATH}")
