"""Two-phase pending-changes protocol: state machine + $acknowledge/$confirm flow.

State-machine tests drive ChangeStore directly (via asyncio.run, no pytest-asyncio).
The API test exercises GET → $acknowledge → $confirm end to end and asserts one Kafka
ack is published per change with the right entity/id/status.
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from messaging import data_events as kafka_publisher
from writeback.change_store import (
    ChangeStore,
    PendingChange,
    SnapshotError,
    get_change_store,
)


@pytest.fixture(autouse=True)
def _force_http_pull_mode(monkeypatch):
    """The pull endpoints are disabled (409) in kafka write mode. These tests exercise
    the HTTP pull, so pin the mode here rather than inheriting it from the environment."""
    from config import settings
    monkeypatch.setattr(settings, "integration_mode", "change_api", raising=False)
    yield


def _change(cid, entity, rid, ctype="bed_status"):
    return PendingChange(
        change_type=ctype, resource_type="Location", resource_id=f"bed-{rid}",
        http_method="PATCH", payload={"code": "U", "display": "Unoccupied"},
        timestamp="2026-06-12T00:00:00+00:00", change_id=cid, entity=entity, record_id=rid,
    )


# ─── state machine ──────────────────────────────────────────────────────────────

def test_offer_lock_confirm_releases():
    async def go():
        store = ChangeStore()
        await store.add(_change("c1", "bed", "101"))
        await store.add(_change("c2", "bed", "102"))

        # GET: nothing in flight yet, then commit the drained set
        assert await store.current_inflight(60) is None
        drained = await store.drain_pending()
        assert len(drained) == 2
        snap = await store.commit_inflight(drained)
        assert snap is not None and snap.state == "offered"

        # re-pull returns the SAME snapshot (idempotent)
        again = await store.current_inflight(60)
        assert again.snapshot_id == snap.snapshot_id

        # $acknowledge locks it
        locked = await store.mark_locked(snap.snapshot_id)
        assert locked.state == "locked"

        # $confirm reads changes without releasing, then release clears
        changes = await store.inflight_changes(snap.snapshot_id)
        assert {c.change_id for c in changes} == {"c1", "c2"}
        await store.release(snap.snapshot_id)
        assert await store.current_inflight(60) is None
    asyncio.run(go())


def test_writes_during_inflight_queue_for_next_pull():
    async def go():
        store = ChangeStore()
        await store.add(_change("c1", "bed", "101"))
        snap = await store.commit_inflight(await store.drain_pending())

        # a write arrives while c1's snapshot is in flight
        await store.add(_change("c2", "bed", "102"))
        # draining is a no-op while a snapshot is in flight
        assert await store.drain_pending() == []

        # release, then the next pull offers only the queued c2
        await store.release(snap.snapshot_id)
        nxt = await store.commit_inflight(await store.drain_pending())
        assert [c.change_id for c in nxt.changes] == ["c2"]
    asyncio.run(go())


def test_expired_lock_requeues_changes():
    async def go():
        store = ChangeStore()
        await store.add(_change("c1", "bed", "101"))
        snap = await store.commit_inflight(await store.drain_pending())
        await store.mark_locked(snap.snapshot_id)

        # with a zero timeout the in-flight snapshot is expired and re-queued
        assert await store.current_inflight(0) is None
        reoffered = await store.commit_inflight(await store.drain_pending())
        assert reoffered.snapshot_id != snap.snapshot_id
        assert [c.change_id for c in reoffered.changes] == ["c1"]
    asyncio.run(go())


def test_ack_confirm_reject_unknown_snapshot():
    async def go():
        store = ChangeStore()
        with pytest.raises(SnapshotError):
            await store.mark_locked("nope")
        with pytest.raises(SnapshotError):
            await store.inflight_changes("nope")
    asyncio.run(go())


# ─── API end-to-end ──────────────────────────────────────────────────────────────

ACKS = []


@pytest.fixture
def client(monkeypatch):
    ACKS.clear()
    # reset the process-wide store between tests
    store = get_change_store()
    store._pending.clear()
    store._inflight = None

    async def _capture(**kw):
        ACKS.append(kw)

    monkeypatch.setattr(kafka_publisher, "publish_ack", _capture)

    from writeback.http import router as changes_router
    from runtime import router as runtime_router
    app = FastAPI()
    app.include_router(runtime_router)
    app.include_router(changes_router)
    return TestClient(app)


def test_two_phase_flow_publishes_one_ack_per_change(client):
    # queue two writes through the normal write endpoints
    assert client.post("/beds/B1/status", json={"status": "available"}).status_code == 200
    assert client.post("/visits/v9/triage", json={"score": 2}).status_code == 200

    # 1. DB pulls the snapshot — one entry per change, each with a change-id fullUrl
    bundle = client.get("/fhir/Bundle/$pending-changes").json()
    snapshot_id = bundle["id"]
    entries = bundle["entry"]
    assert len(entries) == 2
    assert all(e["fullUrl"].startswith("urn:hospilot:change:") for e in entries)
    # every entry carries the DB-side approval flag; an available bed and a triage score
    # are both auto-appliable (no approval needed)
    assert all(e["approvalNeeded"] is False for e in entries)
    change_ids = [e["fullUrl"].split(":")[-1] for e in entries]

    # re-pull is idempotent (same snapshot)
    assert client.get("/fhir/Bundle/$pending-changes").json()["id"] == snapshot_id

    # 2. receipt — locks
    ack = client.post("/fhir/Bundle/$pending-changes/$acknowledge", json={"snapshot_id": snapshot_id})
    assert ack.status_code == 200 and ack.json()["state"] == "locked"

    # 3. confirm — one accepted, one rejected
    confirm = client.post("/fhir/Bundle/$pending-changes/$confirm", json={
        "snapshot_id": snapshot_id,
        "results": [
            {"change_id": change_ids[0], "status": "accepted"},
            {"change_id": change_ids[1], "status": "rejected", "reason": "stale"},
        ],
    })
    assert confirm.status_code == 200 and confirm.json()["published"] == 2

    # exactly one ack per change, carrying entity/id/status
    assert len(ACKS) == 2
    by_status = {a["status"]: a for a in ACKS}
    assert set(by_status) == {"accepted", "rejected"}
    entities = {a["entity"] for a in ACKS}
    assert entities == {"bed", "visit"}
    assert by_status["rejected"]["reason"] == "stale"

    # lock released — next pull is empty
    assert client.get("/fhir/Bundle/$pending-changes").json()["entry"] == []


def test_approval_needed_flag_per_change(client):
    # bed reservation + discharge-ready need approval; cleaning bed does not
    assert client.post("/beds/B1/status", json={"status": "reserved"}).status_code == 200
    assert client.post("/beds/B2/status", json={"status": "cleaning"}).status_code == 200
    assert client.post("/admissions/a7/discharge-ready", json={"ready": True}).status_code == 200

    entries = client.get("/fhir/Bundle/$pending-changes").json()["entry"]
    approval_by_url = {e["request"]["url"]: e["approvalNeeded"] for e in entries}
    assert approval_by_url["Location/bed-B1"] is True
    assert approval_by_url["Location/bed-B2"] is False
    assert approval_by_url["Encounter/ipd-a7"] is True


def test_confirm_unknown_snapshot_is_409(client):
    r = client.post("/fhir/Bundle/$pending-changes/$confirm",
                    json={"snapshot_id": "ghost", "results": []})
    assert r.status_code == 409


# ─── lossy statuses must ride along in bed-raw-status ────────────────────────────
def test_bed_status_bundle_carries_raw_status(client):
    """reserved / vacating / occupied all map to FHIR "O", so the code alone would tell
    the HIS "Occupied" when we mean "reserved". The Bundle must also set the
    bed-raw-status extension -- the same one location.to_internal reads back."""
    from fhirgw import extensions as X

    assert client.post("/beds/B1/status", json={"status": "reserved"}).status_code == 200
    entry = client.get("/fhir/Bundle/$pending-changes").json()["entry"][0]
    params = entry["resource"]["parameter"]

    def _part(op, name):
        return next((p for p in op["part"] if p["name"] == name), None)

    coding = [o for o in params if (_part(o, "path") or {}).get("valueString")
              == "Location.operationalStatus"]
    assert len(coding) == 1, "expected the standard operationalStatus op"
    assert _part(coding[0], "value")["valueCoding"]["code"] == "O"

    exts = [o for o in params if (_part(o, "value") or {}).get("valueExtension")]
    assert len(exts) == 1, f"expected one extension op, got {len(exts)}"
    ve = _part(exts[0], "value")["valueExtension"]
    assert ve["url"] == X.EXT_BED_RAW_STATUS
    assert ve["valueString"] == "reserved", f"raw status not preserved: {ve}"


def test_bed_status_lossless_word_survives_a_round_trip(client):
    """The write says "reserved"; a read of the resulting resource must say "reserved"
    too, not the squashed "Occupied"."""
    from fhirgw import extensions as X
    from fhirgw.mappers import location as loc_map
    from fhir.resources.location import Location

    assert client.post("/beds/B3/status", json={"status": "reserved"}).status_code == 200
    entry = client.get("/fhir/Bundle/$pending-changes").json()["entry"][0]
    params = entry["resource"]["parameter"]
    ext = next(p["valueExtension"] for op in params for p in op["part"]
               if p["name"] == "value" and "valueExtension" in p)

    # apply the patch the way the HIS would, then read it back through Fabric's mapper
    patched = Location(id="bed-B3", status="active", mode="instance",
                       operationalStatus={"system": X.EXT_BASE, "code": "O"},
                       extension=[ext])
    assert loc_map.to_internal(patched)["status"] == "reserved"
