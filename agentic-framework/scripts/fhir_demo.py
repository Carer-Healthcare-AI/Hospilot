"""
Standalone FHIR demo server -- verify the /fhir API with NO infrastructure.

It mounts the REAL fhirgw router + mappers (so hitting it validates the shipped
code), but backs the cache/Hasura reads with in-memory sample data. No Redis,
Kafka, Temporal, or Hasura required.

Run from the repo root:
    venv\\Scripts\\python.exe src\\scripts\\fhir_demo.py      (Windows)
    python src/scripts/fhir_demo.py                          (POSIX)

Then open http://localhost:8077/fhir/metadata or use the curl examples in
docs/FHIR.md. Set FHIR_DEMO_PORT to change the port.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))  # put src/ on path

# Config needs these; dummy values are fine (the demo never calls them).
os.environ.setdefault("ANTHROPIC_API_KEY", "demo")
os.environ.setdefault("HASURA_URL", "http://localhost/v1/graphql")
os.environ.setdefault("HASURA_ADMIN_SECRET", "demo")
os.environ.setdefault("FHIR_BASE_URL", "http://localhost:8077/fhir")
# Set FHIR_API_KEY before running to exercise auth (then send Authorization: Bearer <key>).

import uvicorn
from fastapi import FastAPI, Depends, Request

import cache.redis as cache_redis
from db.hasura import hasura
from api.routes.fhir import router as fhir_router
from fhirgw.security import require_fhir_auth
from fhirgw.outcomes import FHIRError
from fhirgw.mappers import observation

# --- in-memory sample data (mirrors the real projection shapes) ---------------
BEDS = [
    {"id": "bed-icu-01", "ward": "ICU", "bed_number": "ICU-01", "room_type": "ICU", "status": "Available",
     "is_active": True, "branch_id": "branch-main", "ventilation": "full_ventilator", "room_sharing": "private",
     "proximity": 1, "floor": 3, "wing": "North", "natural_light": True, "noise_level": "quiet",
     "features": ["isolation", "telemetry"]},
    {"id": "bed-icu-02", "ward": "ICU", "bed_number": "ICU-02", "room_type": "ICU", "status": "Occupied",
     "is_active": True, "branch_id": "branch-main", "ventilation": "oxygen", "room_sharing": "private",
     "proximity": 2, "floor": 3, "wing": "North", "natural_light": False, "noise_level": "moderate",
     "features": ["telemetry"]},
    {"id": "bed-gen-01", "ward": "General Ward A", "bed_number": "GWA-01", "room_type": "General", "status": "Dirty",
     "is_active": True, "branch_id": "branch-main", "ventilation": "none", "room_sharing": "shared_4",
     "proximity": 5, "floor": 1, "wing": "South", "natural_light": True, "noise_level": "loud", "features": []},
]
DEPARTMENTS = [
    {"id": "dept-icu", "name": "Intensive Care Unit", "type": "icu"},
    {"id": "dept-er", "name": "Emergency", "type": "er"},
]
ADMISSIONS = [
    {"id": "adm-1", "patient_token": "patient-001", "bed_id": "bed-icu-02",
     "admitted_at": "2026-06-01T09:15:00+00:00", "expected_discharge_at": "2026-06-06T12:00:00+00:00",
     "status": "admitted"},
]
VISITS = [
    {"id": "visit-1", "patient_token": "patient-002", "department_id": "dept-er",
     "arrived_at": "2026-06-02T08:40:00+00:00", "status": "waiting", "chief_complaint": "Chest pain",
     "triage_score": 2},
]
VITALS = {
    "patient-001": {"id": "vital-1", "patient_token": "patient-001", "admission_id": "adm-1",
                    "recorded_at": "2026-06-02T07:30:00+00:00", "temperature": 38.5, "pulse": 110,
                    "bp_systolic": 90, "bp_diastolic": 60, "spo2": 92, "respiratory_rate": 24,
                    "gcs": 14, "is_critical": True},
}
LABS = [
    {"id": "lab-1", "order_id": "order-1", "patient_token": "patient-001", "test_name": "Serum Potassium",
     "test_code": "K-001", "result_value": "6.8", "flag": "Critical", "reference_range": "3.5-5.0",
     "unit": "mEq/L", "reported_at": "2026-06-02T07:45:00+00:00"},
]
_KV = {f"bed:{b['id']}": b for b in BEDS}
_KV.update({f"dept:{d['id']}": d for d in DEPARTMENTS})
_KV.update({f"admission:{a['id']}": a for a in ADMISSIONS})
_KV.update({f"visit:{v['id']}": v for v in VISITS})


# --- in-memory replacements for the cache + hasura reads the router uses ------
async def _get_all_beds(): return BEDS
async def _get_all_departments(): return DEPARTMENTS
async def _get_all_admissions(): return ADMISSIONS
async def _get_all_visits(): return VISITS
async def _get(key): return _KV.get(key)
async def _get_vitals(token): return VITALS.get(token)


async def _get_fhir(resource_type, rid):
    if resource_type == "Observation":
        for v in VITALS.values():
            for o in observation.vitals_to_fhir(v):
                if o.id == rid:
                    import json
                    return json.loads(o.model_dump_json(exclude_none=True, by_alias=True))
    return None


async def _fhir_get_lab_results(patient_token=None, test_code=None, limit=200):
    rows = LABS
    if patient_token:
        rows = [r for r in rows if r["patient_token"] == patient_token]
    if test_code:
        rows = [r for r in rows if r["test_code"] == test_code]
    return rows[:limit]


async def _fhir_get_lab_result_by_id(result_id):
    return next((r for r in LABS if r["id"] == result_id), None)


cache_redis.get_all_beds = _get_all_beds
cache_redis.get_all_departments = _get_all_departments
cache_redis.get_all_admissions = _get_all_admissions
cache_redis.get_all_visits = _get_all_visits
cache_redis.get = _get
cache_redis.get_vitals = _get_vitals
cache_redis.get_fhir = _get_fhir
hasura.fhir_get_lab_results = _fhir_get_lab_results
hasura.fhir_get_lab_result_by_id = _fhir_get_lab_result_by_id


# --- app (same wiring as main.py) ---------------------------------------------
app = FastAPI(title="Hospilot FHIR demo")
app.include_router(fhir_router, prefix="/fhir", dependencies=[Depends(require_fhir_auth)])


@app.exception_handler(FHIRError)
async def _fhir_error_handler(request: Request, exc: FHIRError):
    return exc.response()


if __name__ == "__main__":
    port = int(os.getenv("FHIR_DEMO_PORT", "8077"))
    print(f"\n  Hospilot FHIR demo -> http://localhost:{port}/fhir/metadata")
    print("  Sample IDs: Location/bed-icu-01  Encounter/adm-1  Observation?patient=patient-001&category=vital-signs\n")
    uvicorn.run(app, host="127.0.0.1", port=port)
