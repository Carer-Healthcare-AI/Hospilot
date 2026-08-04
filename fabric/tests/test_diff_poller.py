"""Polling-mode diff poller tests — pure, no live DB / Kafka.

Drives the async diff logic with asyncio.run (no pytest-asyncio, matching the rest of
the suite) and captures publishes by monkeypatching data_events.publish.
"""

import asyncio
import json

import pytest

from ingest import diff_poller as dp
from messaging import data_events as kafka_publisher
from messaging import producer as kafka_producer
from service import transform as tx

_REAL_PUBLISH = kafka_publisher.publish      # captured before the autouse fixture patches it


# ─── publish capture ──────────────────────────────────────────────────────────────
PUBLISHED: list[dict] = []


@pytest.fixture(autouse=True)
def capture(monkeypatch):
    PUBLISHED.clear()
    dp.reset_cache()

    async def _pub(entity, record_id, data, operation="upsert", changed=None):
        PUBLISHED.append({"entity": entity, "id": record_id, "operation": operation,
                          "data": data, "changed": changed})

    monkeypatch.setattr(kafka_publisher, "publish", _pub)
    yield
    PUBLISHED.clear()
    dp.reset_cache()


def _run_entity_once(entity, rows):
    """Run one poll cycle for a DiffEntity whose fetch returns `rows`."""
    e = dp.DiffEntity(entity.entity, lambda: _async(rows), entity.mutable_cols, entity.id_key)
    asyncio.run(dp._poll_entity(e))


async def _async(value):
    return value


BED = next(e for e in dp.CLINICAL_ENTITIES if e.entity == "bed")
ADMISSION = next(e for e in dp.CLINICAL_ENTITIES if e.entity == "admission")


# ─── diff algorithm ────────────────────────────────────────────────────────────────
def test_new_record_publishes_full_upsert():
    row = {"id": "B1", "status": "Available", "is_active": True, "ward": "ICU"}
    _run_entity_once(BED, [row])
    assert len(PUBLISHED) == 1
    ev = PUBLISHED[0]
    assert ev["entity"] == "bed" and ev["id"] == "B1"
    assert ev["operation"] == "upsert" and ev["changed"] is None
    assert ev["data"] == row                      # FULL row on first sight


def test_changed_column_publishes_patch_with_only_that_column():
    row = {"id": "B1", "status": "Available", "is_active": True, "ward": "ICU"}
    _run_entity_once(BED, [row])                   # seed
    PUBLISHED.clear()
    _run_entity_once(BED, [{**row, "status": "Occupied"}])
    assert len(PUBLISHED) == 1
    ev = PUBLISHED[0]
    assert ev["operation"] == "patch"
    assert ev["changed"] == ["status"]
    assert ev["data"] == {"status": "Occupied"}    # ONLY the changed column, not the full row


def test_unchanged_record_publishes_nothing():
    row = {"id": "B1", "status": "Available", "is_active": True, "ward": "ICU"}
    _run_entity_once(BED, [row])
    PUBLISHED.clear()
    _run_entity_once(BED, [dict(row)])             # same tracked values
    assert PUBLISHED == []


def test_untracked_column_change_is_ignored():
    row = {"id": "B1", "status": "Available", "is_active": True, "ward": "ICU"}
    _run_entity_once(BED, [row])
    PUBLISHED.clear()
    _run_entity_once(BED, [{**row, "ward": "WARD-B"}])   # ward is not a tracked mutable col
    assert PUBLISHED == []


# ─── at-least-once: failed publish must not advance the cache ──────────────────────
def test_failed_publish_replays_next_cycle(monkeypatch):
    calls = {"n": 0}

    async def _flaky(entity, record_id, data, operation="upsert", changed=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("kafka down")
        PUBLISHED.append({"entity": entity, "id": record_id, "operation": operation,
                          "data": data, "changed": changed})

    monkeypatch.setattr(kafka_publisher, "publish", _flaky)
    row = {"id": "B1", "status": "Available", "is_active": True}
    _run_entity_once(BED, [row])                   # publish raises → cache NOT updated
    assert PUBLISHED == []
    _run_entity_once(BED, [row])                   # re-detected as new → republished
    assert len(PUBLISHED) == 1 and PUBLISHED[0]["operation"] == "upsert"


# ─── bed Available↔Dirty transition is a status patch, not delete+new ──────────────
def test_bed_suspended_transition_is_a_patch():
    active = {"id": "B1", "status": "Available", "is_active": True}
    _run_entity_once(BED, [active])                # seen as active
    PUBLISHED.clear()
    # next cycle: the same bed now suspended/dirty (union of beds()+dirty_beds())
    dirty = {"id": "B1", "status": "Dirty", "is_active": False}
    _run_entity_once(BED, [dirty])
    assert len(PUBLISHED) == 1
    ev = PUBLISHED[0]
    assert ev["operation"] == "patch"
    assert set(ev["changed"]) == {"status", "is_active"}
    assert ev["data"] == {"status": "Dirty", "is_active": False}


# ─── admission discharge_ready companion topic ─────────────────────────────────────
def test_admission_discharge_ready_fans_out_companion_topic():
    adm = {"id": "a7", "status": "in-progress", "discharge_ready": True,
           "discharge_blocked_reason": None, "transfer_pending": False, "bed_id": "B1"}
    _run_entity_once(ADMISSION, [adm])
    topics = {(e["entity"], e["operation"]) for e in PUBLISHED}
    assert ("admission", "upsert") in topics
    assert ("discharge_ready", "upsert") in topics       # fan-out mirrors change_api


# ─── lab_result keyset pagination ──────────────────────────────────────────────────
def test_lab_result_pagination_walks_all_pages(monkeypatch):
    pages = {
        None: {"sync_id": "s1", "rows": [{"id": "L1", "result_value": 1}],
               "pagination": {"has_more": True, "next_cursor": "C1"}},
        "C1": {"sync_id": "s1", "rows": [{"id": "L2", "result_value": 2}],
               "pagination": {"has_more": False, "next_cursor": None}},
    }

    async def _page(table, *, limit=None, cursor=None, sync_id=None):
        assert table == "lab_result"
        return pages[cursor]

    monkeypatch.setattr(dp.sync_client, "fetch_page", _page)
    rows = asyncio.run(dp._fetch_lab_results())
    ids = {r["id"] for r in rows}
    assert ids == {"L1", "L2"}                            # both pages walked, loop terminated


def test_lab_result_pagination_stops_on_repeated_cursor(monkeypatch):
    async def _page(table, *, limit=None, cursor=None, sync_id=None):
        # pathological server: always says has_more with the same cursor
        return {"rows": [{"id": "L1"}], "pagination": {"has_more": True, "next_cursor": "STUCK"}}

    monkeypatch.setattr(dp.sync_client, "fetch_page", _page)
    rows = asyncio.run(dp._fetch_lab_results())           # must not hang
    assert len(rows) >= 1


# ─── config flag + validation ──────────────────────────────────────────────────────
def test_integration_mode_default_and_property(monkeypatch):
    from config import Settings
    # Assert the built-in DEFAULT, so an exported INTEGRATION_MODE (or a developer's
    # fabric/.env) cannot flip the result.
    monkeypatch.delenv("INTEGRATION_MODE", raising=False)
    s = Settings(_env_file=None)
    assert s.integration_mode == "change_api"
    assert s.polling_mode is False
    assert Settings(integration_mode="POLLING").polling_mode is True


def test_integration_mode_rejects_unknown():
    from config import Settings
    with pytest.raises(Exception):
        Settings(integration_mode="bogus")


def test_poll_intervals_map_keys():
    from config import settings
    assert set(settings.poll_intervals_ms) == {
        "bed", "admission", "visit", "lab_order", "task", "lab_result"}


# ─── publisher payload parity (change_api shape unchanged) ─────────────────────────
def test_publish_payload_omits_changed_unless_given():
    captured = {}

    class _Prod:
        async def send_and_wait(self, topic, value, key):
            captured["topic"] = topic
            captured["payload"] = json.loads(value.decode())

    kafka_producer._producer = _Prod()
    try:
        asyncio.run(_REAL_PUBLISH("bed", "B1", {"status": "Available"}))
        assert "changed" not in captured["payload"]      # upsert: byte-parity with today
        asyncio.run(_REAL_PUBLISH(
            "bed", "B1", {"status": "Occupied"}, operation="patch", changed=["status"]))
        assert captured["payload"]["changed"] == ["status"]
        assert captured["payload"]["operation"] == "patch"
    finally:
        kafka_producer._producer = None


# ─── lab_result_row normalizer ─────────────────────────────────────────────────────
def test_lab_result_row_maps_candidate_columns():
    raw = {"id": "L1", "patient_id": "pt-9", "loinc_code": "718-7", "name": "Hemoglobin",
           "value": 9.1, "units": "g/dL", "flag": "Low", "ref_range": "12-16",
           "resulted_at": "2026-06-12T10:00:00"}
    out = tx.lab_result_row(raw)
    assert out["id"] == "L1" and out["patient_token"] == "pt-9"
    assert out["test_code"] == "718-7" and out["test_name"] == "Hemoglobin"
    assert out["result_value"] == 9.1 and out["unit"] == "g/dL"
    # `flag` is read from the confirmed `flag` column only, and carries the word
    # vocabulary (Critical | High | Low | Normal) that lab_result() also emits.
    assert out["flag"] == "Low" and out["reference_range"] == "12-16"
    assert out["reported_at"] == "2026-06-12T10:00:00"
    # shape parity: same keys the change_api lab_result() emits
    assert set(out) == {"id", "patient_token", "test_code", "test_name",
                        "result_value", "unit", "flag", "reference_range", "reported_at"}
