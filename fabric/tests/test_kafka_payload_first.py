"""Kafka-mode ingest must use the event's own payload for cached entities.

The DB already sends the changed row in the event, so re-reading it over HTTP is
duplicated work on the DB's server. These tests pin that behaviour: a payload-carrying
event for a mapped entity must issue NO read, while the fallbacks (no payload, no
mapper, join-sourced field missing) must still re-read.
"""

import asyncio

import pytest

from clients import fhir_client
from ingest import kafka_consumer as kc
from messaging import data_events as kafka_publisher


# ─── harness ──────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _spy(monkeypatch):
    """Count every outbound read, and capture what gets published."""
    reads: list[str] = []
    published: list[tuple] = []

    async def _read_location(rid):
        reads.append(f"Location/{rid}")
        return None

    async def _read_encounter(rid):
        reads.append(f"Encounter/{rid}")
        return None

    async def _read_observation(rid):
        reads.append(f"Observation/{rid}")
        return None

    monkeypatch.setattr(fhir_client, "read_location", _read_location)
    monkeypatch.setattr(fhir_client, "read_encounter", _read_encounter)
    monkeypatch.setattr(fhir_client, "read_observation", _read_observation)

    def _searcher(kind):
        async def _f(params):
            reads.append(f"{kind}?{params}")
            return []
        return _f

    for name, kind in (
        ("search_service_requests", "ServiceRequest"),
        ("search_tasks", "Task"),
        ("search_specimens", "Specimen"),
        ("search_devices", "Device"),
        ("search_medication_requests", "MedicationRequest"),
        ("search_inventory_items", "InventoryItem"),
        ("search_organizations", "Organization"),
    ):
        monkeypatch.setattr(fhir_client, name, _searcher(kind))

    async def _publish(entity, record_id, data, operation="upsert", changed=None):
        published.append((entity, record_id, data, operation))

    monkeypatch.setattr(kafka_publisher, "publish", _publish)

    _spy.reads = reads
    _spy.published = published
    yield
    reads.clear()
    published.clear()


def _handle(entity, rid, data, operation="upsert"):
    asyncio.run(kc._handle(entity, rid, operation, data))


# ─── payload-first: no read for a cached entity ───────────────────────────────
BED_ROW = {"id": "b1", "bed_number": "ICU-02", "ward": "ICU",
           "status": "reserved", "is_active": True, "features": "oxygen,monitor"}


def test_bed_event_payload_issues_no_read():
    _handle("bed", "b1", BED_ROW)
    assert _spy.reads == [], f"expected zero reads, got {_spy.reads}"
    entity, rid, data, op = _spy.published[0]
    assert (entity, rid, op) == ("bed", "b1", "upsert")
    assert data["ward"] == "ICU" and data["bed_number"] == "ICU-02"


def test_raw_status_survives_instead_of_collapsing_to_occupied():
    """The FHIR path maps reserved -> v2-0116 'O' -> 'Occupied', losing a distinction
    the consumers test for. The payload path must preserve it."""
    _handle("bed", "b1", BED_ROW)
    assert _spy.published[0][2]["status"] == "reserved"


def test_admission_payload_keeps_internal_status():
    _handle("admission", "a1", {"id": "a1", "patient_token": "p1",
                                "bed_id": "b1", "status": "critical"})
    assert _spy.reads == []
    assert _spy.published[0][2]["status"] == "critical"


@pytest.mark.parametrize("entity,row", [
    ("task", {"id": "t1", "admission_id": "a1", "task": "IV abx", "completed": False}),
    ("lab_result", {"id": "L1", "patient_token": "p1", "flag": "High"}),
    ("lab_sample", {"id": "s1", "order_id": "o1", "barcode": "BC-1"}),
    ("lab_analyzer", {"id": "d1", "name": "XN-1000", "status": "active"}),
    ("pharmacy_inventory", {"id": "i1", "name": "Pip-Taz", "qty_in_stock": 42}),
    ("visit", {"id": "v1", "patient_token": "p2", "status": "in-progress"}),
])
def test_mapped_entities_never_read(entity, row):
    _handle(entity, row["id"], row)
    assert _spy.reads == [], f"{entity} should not read; got {_spy.reads}"
    assert _spy.published, f"{entity} published nothing"


def test_lab_topic_publishes_as_lab_order():
    """Topic suffix is `lab`; the cache keys it as `lab_order`."""
    _handle("lab", "o1", {"id": "o1", "patient_token": "p1",
                          "status": "active", "test_name": "CBC"})
    assert _spy.reads == []
    assert _spy.published[0][0] == "lab_order"


def test_admission_discharge_ready_fans_out_without_extra_read():
    _handle("admission", "a2", {"id": "a2", "patient_token": "p1",
                                "discharge_ready": True, "status": "admitted"})
    assert _spy.reads == []
    assert [e for e, *_ in _spy.published] == ["admission", "discharge_ready"]


# ─── fallbacks: a read is still issued when the payload cannot be trusted ─────
def test_no_payload_falls_back_to_read():
    _handle("bed", "b9", None)
    assert _spy.reads == ["Location/bed-b9"]


def test_empty_payload_falls_back_to_read():
    _handle("admission", "a9", {})
    assert _spy.reads == ["Encounter/ipd-a9"]


def test_payload_without_id_falls_back_to_read():
    _handle("bed", "b8", {"ward": "ICU", "status": "Available"})
    assert _spy.reads == ["Location/bed-b8"]


def test_lab_order_without_test_name_falls_back_to_read():
    """test_name lives on the lab_results relation, so a bare orders row is incomplete."""
    _handle("lab", "o2", {"id": "o2", "patient_token": "p1", "status": "active"})
    assert _spy.reads and _spy.reads[0].startswith("ServiceRequest?")


def test_pharmacy_order_without_medication_falls_back_to_read():
    _handle("pharmacy_order", "rx1", {"id": "rx1", "patient_token": "p1", "status": "active"})
    assert _spy.reads and _spy.reads[0].startswith("MedicationRequest?")


def test_unmapped_entity_still_reads():
    """dept has no row mapper and is not cached — behaviour is unchanged."""
    _handle("dept", "d1", {"id": "d1", "name": "Emergency"})
    assert _spy.reads and _spy.reads[0].startswith("Organization?")


def test_raw_row_entities_pass_through_untouched():
    """ambulance/ot_* have no FHIR resource; the row is published as-is, no read."""
    row = {"id": "amb1", "vehicle_no": "KA-01", "status": "available"}
    _handle("ambulance", "amb1", row)
    assert _spy.reads == []
    assert _spy.published[0][2] == row


def test_delete_never_reads_and_publishes_null():
    _handle("bed", "b1", BED_ROW, operation="delete")
    assert _spy.reads == []
    entity, rid, data, op = _spy.published[0]
    assert (entity, op, data) == ("bed", "delete", None)
