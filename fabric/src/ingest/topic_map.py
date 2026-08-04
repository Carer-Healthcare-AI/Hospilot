"""Map changed DB data → Kafka (entity, id, data) events.

Two sources feed the same event shape:

  • FHIR change feed (incremental) — the DB's `$changed-resources` Bundle carries
    standard FHIR R5 resources. `fhir_resources_to_events` turns each into the
    normalized dict via service.transform (the same transforms the read endpoints
    use), so `data` matches what Fabric already serves.

  • Plain-REST endpoints (no change feed) — OT / ambulance / appointments have no
    incremental feed, so the poller fetches the full list and diffs. REST_ENTITIES
    is the (topic-entity, fetch-coroutine) registry it iterates; rows are already in
    Fabric's served shape, so they're published as-is.

Entities with no Kafka topic (Patient, Organization, Composition) are skipped.
`pharmacy_order` has no Fabric endpoint, so it is not published (logged once).
"""

import logging

from clients import fhir_client as fc
from service import ambulance as amb_svc
from service import appointments as appt_svc
from service import ot as ot_svc
from service import transform as tx
from service import lab as lab_svc
from service import pharmacy as pharmacy_svc
from service import staff as staff_svc
from service import ventilator as vent_svc
from service import financial as fin_svc

logger = logging.getLogger("poller")

# Non-FHIR operational tables that pass through the change feed as raw rows.
# resourceType in the Bundle entry equals the entity name (set by fabricSync.js).
_RAW_ENTITIES: frozenset[str] = frozenset({"ventilator", "staff_roster", "staff"})


# ─── REST entities (full-list poll + diff) ──────────────────────────────────────
# topic entity -> coroutine returning list[dict] in Fabric's served shape.
REST_ENTITIES = [
    ("ot_room",        ot_svc.rooms),
    ("ot_room_status", ot_svc.room_status),
    ("ot_schedule",    ot_svc.surgery_schedule),
    ("ot_surgery",     ot_svc.surgeries),
    ("ambulance",      amb_svc.fleet),
    ("appointment",    appt_svc.list_all),
    ("doctor_slot",    appt_svc.slots),
    # billing (DATA_NEEDED) — live via the DB's plain-REST financial API:
    #   invoice → amount (grand_total) / outstanding (balance) / status
    #   claim   → payer_type / claim_status (status) / amounts
    ("invoice",        fin_svc.invoices),
    ("claim",          fin_svc.claims),
    # HRMS / ICU (DATA_NEEDED) — sourced by draining the DB keyset sync API. INERT
    # until the DB registers /api/sync/{staff,staff_roster,ventilator}: the fetch
    # 404s, the poller logs + skips, nothing is published. No internal-DB routing on the
    # backend either (intentional — these feed a separate consumer, not the cache).
    ("staff",          staff_svc.members),
    ("staff_roster",   staff_svc.roster),
    ("ventilator",     vent_svc.units),
    # lab_sample + lab_analyzer come via the FHIR change feed (Specimen/Device),
    # not REST polling. pharmacy_order + pharmacy_inventory similarly via
    # MedicationRequest/InventoryItem. All four are mapped in _map_single below.
]


# ─── FHIR change-feed mapping ───────────────────────────────────────────────────
def _obs_category(raw: dict) -> str | None:
    for c in (raw.get("category") or []):
        for coding in (c.get("coding") or []):
            if coding.get("code"):
                return coding["code"]
    return None


def _map_single(rt: str, rid: str, model) -> list[tuple[str, str, dict]]:
    """Map one non-vital FHIR resource to zero or more (entity, id, data) events."""
    if rt == "Location" and rid.startswith("bed-"):
        d = tx.bed(model)
        return [("bed", d["id"], d)]
    if rt == "Encounter" and rid.startswith("ipd-"):
        d = tx.admission(model)
        out = [("admission", d["id"], d)]
        if d.get("discharge_ready"):                       # discharge_ready topic: only when true
            out.append(("discharge_ready", d["id"], d))
        return out
    if rt == "Encounter" and rid.startswith("em-"):
        d = tx.visit(model)
        return [("visit", d["id"], d)]
    if rt == "Observation":                                # laboratory (vitals handled separately)
        d = tx.lab_result(model)
        return [("lab_result", d["id"], d)]
    if rt == "ServiceRequest":
        d = tx.lab_order(model)
        return [("lab_order", d["id"], d)]
    if rt == "Task":
        d = tx.nursing_task(model)
        return [("task", d["id"], d)]
    if rt == "Specimen":
        d = tx.lab_sample(model)
        return [("lab_sample", d["id"], d)]
    if rt == "Device":
        d = tx.lab_analyzer(model)
        return [("lab_analyzer", d["id"], d)]
    if rt == "MedicationRequest":
        d = tx.pharmacy_order(model)
        return [("pharmacy_order", d["id"], d)]
    if rt == "InventoryItem":
        d = tx.pharmacy_inventory(model)
        return [("pharmacy_inventory", d["id"], d)]
    return []                                              # Patient / Organization / Composition: no topic


def _is_vital_obs(raw: dict, rid: str) -> bool:
    return raw.get("resourceType") == "Observation" \
        and not rid.startswith("lab-") \
        and _obs_category(raw) != "laboratory"


def fhir_delete_to_events(request_url: str) -> list[tuple[str, str]]:
    """Map a FHIR DELETE request.url (e.g. 'Location/bed-101') to (entity, bare_id) pairs.

    Returns empty list for types with no Kafka topic (Patient, Organization,
    Composition) and for vital Observations (they're grouped readings — a single
    Observation delete doesn't map cleanly to one vital event).
    """
    if "/" not in request_url:
        return []
    rt, rid = request_url.split("/", 1)
    if rt in _RAW_ENTITIES:
        return [(rt, rid)]
    if rt == "Location" and rid.startswith("bed-"):
        return [("bed", rid[len("bed-"):])]
    if rt == "Encounter" and rid.startswith("ipd-"):
        return [("admission", rid[len("ipd-"):])]
    if rt == "Encounter" and rid.startswith("em-"):
        return [("visit", rid[len("em-"):])]
    if rt == "Observation" and rid.startswith("lab-"):
        return [("lab_result", rid[len("lab-"):])]
    if rt == "ServiceRequest":
        return [("lab_order", rid)]
    if rt == "Task":
        return [("task", rid)]
    if rt == "Specimen":
        return [("lab_sample", rid)]
    if rt == "Device":
        return [("lab_analyzer", rid)]
    if rt == "MedicationRequest":
        return [("pharmacy_order", rid)]
    if rt == "InventoryItem":
        return [("pharmacy_inventory", rid)]
    return []


def fhir_resources_to_events(resources: list[dict]) -> list[tuple[str, str, dict]]:
    """Turn the change feed's FHIR resources into Kafka events. Vital Observations are
    grouped by reading (the DB emits one Observation per measure) before transforming."""
    events: list[tuple[str, str, dict]] = []
    vital_obs: list = []
    for raw in resources:
        rt = raw.get("resourceType")
        rid = raw.get("id") or ""

        # Raw (non-FHIR) entities: resourceType is the entity name. Pass the row
        # straight through, stripping the resourceType field added by fabricSync.js.
        if rt in _RAW_ENTITIES:
            data = {k: v for k, v in raw.items() if k != "resourceType"}
            events.append((rt, rid, data))
            continue

        model = fc.parse_resource(raw)
        if model is None:
            logger.warning("skip unparseable change-feed %s/%s", rt, rid)
            continue
        if _is_vital_obs(raw, rid):
            vital_obs.append(model)
            continue
        events.extend(_map_single(rt, rid, model))
    for group in tx.group_vitals_by_reading(vital_obs).values():
        d = tx.vital(group)
        if d:
            events.append(("vital", d["id"], d))
    return events
