"""
Endpoint tests — mock the outbound clients (DB FHIR + financial REST) and verify
the route → service → transform path end-to-end, plus a write PATCH.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fhir.resources.location import Location

from clients import fhir_client, rest_client
from fhirgw.mappers import encounter as enc_map, observation as obs_map
from runtime import router


def _strip(model):
    d = json.loads(model.model_dump_json(exclude_none=True, by_alias=True))

    def s(x):
        if isinstance(x, dict):
            x.pop("extension", None)
            return {k: s(v) for k, v in x.items()}
        if isinstance(x, list):
            return [s(i) for i in x]
        return x

    return type(model).model_validate(s(d))


def _bed(bid, name, oper, ward_ref="Location/W1"):
    return Location(
        id=bid, status="active", name=name, mode="instance",
        form={"coding": [{"system": "http://terminology.hl7.org/CodeSystem/location-physical-type", "code": "bd"}]},
        operationalStatus={"system": "http://terminology.hl7.org/CodeSystem/v2-0116", "code": oper},
        partOf={"reference": ward_ref},
    )


def _ward(wid, name):
    return Location(
        id=wid, status="active", name=name, mode="instance",
        form={"coding": [{"system": "http://terminology.hl7.org/CodeSystem/location-physical-type", "code": "wa"}]},
    )


ADM = _strip(enc_map.admission_to_fhir({
    "id": "adm-1", "patient_token": "pt-1", "bed_id": "B1",
    "admitted_at": "2025-01-15T10:30:00+00:00", "status": "admitted"}))
VITALS_OLD = [_strip(o) for o in obs_map.vitals_to_fhir({
    "id": "vit-0", "patient_token": "pt-1", "recorded_at": "2025-01-15T13:00:00+00:00",
    "pulse": 70, "bp_systolic": 118, "bp_diastolic": 78, "spo2": 99, "is_critical": False})]
VITALS = [_strip(o) for o in obs_map.vitals_to_fhir({
    "id": "vit-1", "patient_token": "pt-1", "recorded_at": "2025-01-15T14:00:00+00:00",
    "pulse": 85, "bp_systolic": 130, "bp_diastolic": 85, "spo2": 96, "is_critical": True})]
BEDS = [_bed("B1", "ICU-01", "U"), _ward("W1", "ICU")]

PATCHES = []
OBS_CALLS = []


@pytest.fixture
def client(monkeypatch):
    PATCHES.clear()
    OBS_CALLS.clear()

    async def _locations(params): return BEDS
    async def _encounters(params): return [ADM] if params.get("class") == "IMP" else []
    async def _observations(params):
        OBS_CALLS.append(params)
        return (VITALS_OLD + VITALS) if params.get("category") == "vital-signs" else []
    async def _read_observation(rid): return VITALS[0] if rid == "vit-1.8867-4" else None
    async def _patch(rt, rid, ops): PATCHES.append((rt, rid, ops)); return {}
    async def _try_patch(rt, rid, ops): PATCHES.append((rt, rid, ops)); return True
    async def _fin_list(base_url, path, **kw): return [{"id": "clm-1", "status": "Pending"}]

    monkeypatch.setattr(fhir_client, "search_locations", _locations)
    monkeypatch.setattr(fhir_client, "search_encounters", _encounters)
    monkeypatch.setattr(fhir_client, "search_observations", _observations)
    monkeypatch.setattr(fhir_client, "read_observation", _read_observation)
    monkeypatch.setattr(fhir_client, "patch", _patch)
    monkeypatch.setattr(fhir_client, "try_patch", _try_patch)
    monkeypatch.setattr(rest_client, "list_", _fin_list)

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_admissions_icu(client):
    r = client.get("/admissions/icu")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == "adm-1"
    assert body[0]["patient_token"] == "pt-1"
    assert body[0]["bed"]["ward"] == "ICU"


def test_beds_available_icu(client):
    r = client.get("/beds/available-icu")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["id"] == "B1" and body[0]["status"] == "Available" and body[0]["ward"] == "ICU"


def test_vitals_latest(client):
    r = client.get("/vitals/latest?patient=pt-1")
    assert r.status_code == 200
    body = r.json()
    assert body["pulse"] == 85 and body["bp_systolic"] == 130
    assert body["is_critical"] is True
    assert OBS_CALLS[-1] == {"patient": "pt-1", "category": "vital-signs", "_count": 200}


def test_vitals_critical_uses_fhir_interpretation_filter(client):
    r = client.get("/vitals/critical")
    assert r.status_code == 200
    assert r.json()
    assert OBS_CALLS[-1] == {"category": "vital-signs", "interpretation": "AA", "_count": 200}


def test_vital_observation_read(client):
    r = client.get("/vitals/observations/vit-1.8867-4")
    assert r.status_code == 200
    body = r.json()
    assert body["resourceType"] == "Observation"
    assert body["id"] == "vit-1.8867-4"


def test_financial_claims(client):
    r = client.get("/financial/claims?status=Pending")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "clm-1"


def test_flag_critical_write(client):
    # Writes no longer PATCH directly — they queue a PendingChange for the DB to pull.
    # The bare reading uuid is recorded as record_id; resource_id resolves later.
    import asyncio
    from writeback.change_store import get_change_store
    store = get_change_store()
    store._pending.clear(); store._inflight = None
    r = client.post("/vitals/vit-1/critical")
    assert r.status_code == 200 and r.json()["is_critical"] is True
    pending = asyncio.run(store.drain_pending())
    assert len(pending) == 1
    c = pending[0]
    assert c.change_type == "critical_vital" and c.entity == "vital" and c.record_id == "vit-1"


def test_set_triage_write(client):
    # bare visit uuid in -> queued PendingChange targeting the em-prefixed Encounter
    import asyncio
    from writeback.change_store import get_change_store
    store = get_change_store()
    store._pending.clear(); store._inflight = None
    r = client.post("/visits/v-9/triage", json={"score": 2})
    assert r.status_code == 200 and r.json()["score"] == 2
    pending = asyncio.run(store.drain_pending())
    assert len(pending) == 1
    c = pending[0]
    assert c.change_type == "triage_score" and c.resource_id == "em-v-9"
    assert c.entity == "visit" and c.record_id == "v-9"


# ─── upstream financial paths (regression guard) ────────────────────────────────
def test_financial_upstream_paths(monkeypatch):
    """Pin the exact upstream path per financial read.

    `refunds` in particular is nested under payments; a bare `/refunds` 404s and
    `safe_list` converts that into an empty list, so a wrong path here is invisible
    at runtime. These assertions are the only thing that catches it.
    """
    import asyncio

    from service import financial

    seen = []

    async def _capture(base_url, path, **params):
        seen.append(path)
        return []

    monkeypatch.setattr(financial.rc, "list_", _capture)
    monkeypatch.setattr(financial.rc, "safe_list", _capture)

    asyncio.run(financial.refunds())
    asyncio.run(financial.invoices())
    asyncio.run(financial.claims())
    asyncio.run(financial.payments())
    asyncio.run(financial.payment_entries("pay-1"))
    asyncio.run(financial.invoice_line_items("inv-1"))
    asyncio.run(financial.claim_line_items("clm-1"))
    asyncio.run(financial.contracts())
    asyncio.run(financial.contract_rates("ct-1"))

    assert seen == [
        "payments/refunds",           # NOT "refunds" — nested under payments upstream
        "invoices",
        "claims",
        "payments",
        "payments/pay-1/entries",
        "invoices/inv-1/line_items",  # underscore upstream, hyphen on Fabric's own route
        "claims/clm-1/line_items",
        "contracts",
        "contracts/ct-1/rates",
    ]


# ─── dirty beds must be beds (regression guard) ─────────────────────────────────
def _suspended(loc):
    """Same Location, status=suspended — how upstream marks a bed as needing cleaning."""
    return type(loc).model_validate({**loc.model_dump(exclude_none=True), "status": "suspended"})


def test_dirty_beds_excludes_wards(monkeypatch):
    """Wards are permanently status=suspended upstream because they aren't bookable.

    tx.bed() maps anything handed to it, so without a form=='bd' filter every ward comes
    back as a dirty bed (8 phantoms against 4 real ones on the reference dataset). The
    phantoms have no ward, so they slipped past /beds/dirty-icu but not /beds/dirty --
    and the SLA evaluator then tracked wards as beds stuck in cleaning forever.
    """
    import asyncio
    from service import clinical

    dirty_bed = _suspended(_bed("B9", "ICU-09", "K"))       # a genuinely dirty ICU bed
    suspended_wards = [_suspended(_ward("W1", "ICU")), _suspended(_ward("W2", "Cardiology"))]

    async def _locations(params):
        if params.get("status") == "suspended":
            return [dirty_bed, *suspended_wards]
        return [_bed("B1", "ICU-01", "U"), _ward("W1", "ICU")]

    monkeypatch.setattr(fhir_client, "search_locations", _locations)

    out = asyncio.run(clinical.dirty_beds())
    assert [b["id"] for b in out] == ["B9"], f"wards leaked into dirty beds: {out}"

    icu = asyncio.run(clinical.dirty_beds(icu_only=True))
    assert [b["id"] for b in icu] == ["B9"]
