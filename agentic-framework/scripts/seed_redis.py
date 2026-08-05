"""
Seed Redis with sample bed data for the Hospilot demo.

Run from hospilot-backend/:
    python scripts/seed_redis.py

Requires REDIS_URL in .env (defaults to redis://localhost:6379).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv()
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# Sample beds: 20 beds across ICU, PCU, ER, General, Private
# Using real CarerOS field names: id, org_id, ward, bed_number, status, room_type, is_active, branch_id
BEDS = [
    # ICU -- 4 beds (2 available, 2 occupied)
    {"id": "bed-icu-01", "ward": "ICU", "bed_number": "ICU-01", "status": "Available",
     "room_type": "ICU", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-icu-02", "ward": "ICU", "bed_number": "ICU-02", "status": "Occupied",
     "room_type": "ICU", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-icu-03", "ward": "ICU", "bed_number": "ICU-03", "status": "Available",
     "room_type": "ICU", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-icu-04", "ward": "ICU", "bed_number": "ICU-04", "status": "Occupied",
     "room_type": "ICU", "is_active": False, "branch_id": "branch-main"},   # maintenance

    # PCU -- 3 beds (2 available, 1 occupied)
    {"id": "bed-pcu-01", "ward": "PCU", "bed_number": "PCU-01", "status": "Available",
     "room_type": "PCU", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-pcu-02", "ward": "PCU", "bed_number": "PCU-02", "status": "Occupied",
     "room_type": "PCU", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-pcu-03", "ward": "PCU", "bed_number": "PCU-03", "status": "Available",
     "room_type": "PCU", "is_active": True, "branch_id": "branch-main"},

    # ER -- 3 beds (2 available, 1 occupied)
    {"id": "bed-er-01", "ward": "Emergency", "bed_number": "ER-01", "status": "Available",
     "room_type": "ER", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-er-02", "ward": "Emergency", "bed_number": "ER-02", "status": "Occupied",
     "room_type": "ER", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-er-03", "ward": "Emergency", "bed_number": "ER-03", "status": "Available",
     "room_type": "ER", "is_active": True, "branch_id": "branch-main"},

    # General Ward -- 6 beds (3 available, 3 occupied)
    {"id": "bed-gen-01", "ward": "General Ward A", "bed_number": "GWA-01", "status": "Available",
     "room_type": "General", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-gen-02", "ward": "General Ward A", "bed_number": "GWA-02", "status": "Occupied",
     "room_type": "General", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-gen-03", "ward": "General Ward A", "bed_number": "GWA-03", "status": "Available",
     "room_type": "General", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-gen-04", "ward": "General Ward B", "bed_number": "GWB-01", "status": "Occupied",
     "room_type": "General", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-gen-05", "ward": "General Ward B", "bed_number": "GWB-02", "status": "Available",
     "room_type": "General", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-gen-06", "ward": "General Ward B", "bed_number": "GWB-03", "status": "Occupied",
     "room_type": "General", "is_active": True, "branch_id": "branch-main"},

    # Private Rooms -- 4 beds (2 available, 2 occupied)
    {"id": "bed-prv-01", "ward": "Private Wing", "bed_number": "PVT-01", "status": "Available",
     "room_type": "Private", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-prv-02", "ward": "Private Wing", "bed_number": "PVT-02", "status": "Occupied",
     "room_type": "Private", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-prv-03", "ward": "Private Wing", "bed_number": "PVT-03", "status": "Available",
     "room_type": "Private", "is_active": True, "branch_id": "branch-main"},
    {"id": "bed-prv-04", "ward": "Private Wing", "bed_number": "PVT-04", "status": "Occupied",
     "room_type": "Private", "is_active": True, "branch_id": "branch-main"},
]

# Sample departments
DEPARTMENTS = [
    {"id": "dept-icu",  "name": "Intensive Care Unit",  "type": "Clinical"},
    {"id": "dept-pcu",  "name": "Progressive Care Unit", "type": "Clinical"},
    {"id": "dept-er",   "name": "Emergency",             "type": "Clinical"},
    {"id": "dept-gen",  "name": "General Medicine",      "type": "Clinical"},
    {"id": "dept-surg", "name": "Surgery",               "type": "Clinical"},
]


async def seed():
    r = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Clear existing bed + dept keys
    existing_beds = await r.keys("bed:*")
    existing_depts = await r.keys("dept:*")
    if existing_beds:
        await r.delete(*existing_beds)
    if existing_depts:
        await r.delete(*existing_depts)

    # Write beds
    for bed in BEDS:
        await r.setex(f"bed:{bed['id']}", 90, json.dumps(bed))

    available = sum(1 for b in BEDS if b["status"] == "Available" and b["is_active"])
    occupied  = sum(1 for b in BEDS if b["status"] == "Occupied")
    print(f"[ok] Seeded {len(BEDS)} beds  (available={available}  occupied={occupied}  maintenance=1)")

    # Write departments
    for dept in DEPARTMENTS:
        await r.setex(f"dept:{dept['id']}", 2000, json.dumps(dept))
    print(f"[ok] Seeded {len(DEPARTMENTS)} departments")

    # Summary by type
    by_type: dict[str, dict] = {}
    for bed in BEDS:
        t = bed["room_type"]
        if t not in by_type:
            by_type[t] = {"available": 0, "occupied": 0}
        if bed["status"] == "Available" and bed["is_active"]:
            by_type[t]["available"] += 1
        elif bed["status"] == "Occupied":
            by_type[t]["occupied"] += 1

    print("\nBed summary:")
    for t, counts in by_type.items():
        print(f"  {t:12s}  available={counts['available']}  occupied={counts['occupied']}")

    await r.aclose()


if __name__ == "__main__":
    asyncio.run(seed())
