"""FHIR data-access layer for the agents (internal, server-side).

The agents' clinical reads go through here so they operate on **canonical FHIR R5
resources**. Two backends:

  * **External EHR (CarerOS)** -- when `settings.fhir_ehr_base_url` is set, fetch
    resources straight from the EHR's `/fhir` API (`fhirgw.ehr_client`). Hospilot
    treats CarerOS as an external FHIR source. ICU membership is resolved from the
    Location graph (bed -> `partOf` ward whose name is "ICU").
  * **Local Hasura projection** (fallback) -- run the existing `hasura.get_*`
    clinical queries and map rows to FHIR via `fhirgw.mappers`.

Scope: the ICU agent's reads. The critical-vital write stays Hospilot-side
(`is_critical` is Hospilot-owned enrichment; the external EHR is read-only here).
"""

from fhir.resources.encounter import Encounter
from fhir.resources.location import Location
from fhir.resources.observation import Observation
from fhir.resources.task import Task
from fhir.resources.servicerequest import ServiceRequest

from db.hasura import hasura
from fhirgw import ehr_client
from fhirgw.mappers import (
    encounter as enc_map, location as loc_map, observation as obs_map,
    task as task_map, servicerequest as sr_map,
)
from fhirgw.mappers._common import ref_id, bare_id


# --- external EHR (CarerOS) helpers -------------------------------------------
def _form_code(loc: Location) -> str | None:
    return loc.form.coding[0].code if (loc.form and loc.form.coding) else None


async def _carer_location_index() -> tuple[dict[str, Location], set[str]]:
    """Fetch active Locations -> (beds_by_id, icu_bed_ids). ICU = bed whose
    `partOf` ward has 'icu' in its name/id."""
    locs = await ehr_client.search_locations({"status": "active"})
    beds, wards = {}, {}
    for loc in locs:
        code = _form_code(loc)
        if code == "bd":
            beds[loc.id] = loc
        elif code == "wa":
            wards[loc.id] = loc
    icu_ward_ids = {
        wid for wid, w in wards.items()
        if "icu" in f"{w.name or ''}{wid}".lower()
    }
    icu_bed_ids = {bid for bid, b in beds.items() if ref_id(b.partOf) in icu_ward_ids}
    return beds, icu_bed_ids


def _enc_bed_id(enc: Encounter) -> str | None:
    return ref_id(enc.location[0].location) if enc.location else None


async def _carer_ward_by_bed() -> dict[str, str]:
    """bed_id -> ward name, from the CarerOS Location graph (bed.partOf -> ward.name)."""
    locs = await ehr_client.search_locations({"status": "active"})
    beds, wards = {}, {}
    for loc in locs:
        code = _form_code(loc)
        if code == "bd":
            beds[loc.id] = loc
        elif code == "wa":
            wards[loc.id] = loc
    out = {}
    for bid, b in beds.items():
        w = wards.get(ref_id(b.partOf))
        out[bid] = w.name if w else None
    return out


async def _carer_admissions(want_icu: bool) -> tuple[list[Encounter], dict[str, Location]]:
    beds, icu_bed_ids = await _carer_location_index()
    encs = await ehr_client.search_encounters({"class": "IMP", "status": "in-progress"})
    out, bed_by_id = [], {}
    for e in encs:
        bid = _enc_bed_id(e)
        is_icu = bid in icu_bed_ids
        if is_icu == want_icu:
            out.append(e)
            if bid and bid in beds:
                bed_by_id[bid] = beds[bid]
    return out, bed_by_id


# --- Hasura projection helpers (fallback) -------------------------------------
def _admission_encounter(row: dict) -> Encounter:
    return enc_map.admission_to_fhir({
        "id":            row["id"],
        "patient_token": row.get("patient_token"),
        "bed_id":        row.get("bed_id"),
        "admitted_at":   row.get("admitted_at"),
        "status":        "admitted",
    })


def _bed_location(bed_id: str, bed: dict) -> Location:
    return loc_map.to_fhir({"id": bed_id, **(bed or {})})


def _from_rows(rows: list[dict]) -> tuple[list[Encounter], dict[str, Location]]:
    encounters, beds_by_id = [], {}
    for r in rows:
        encounters.append(_admission_encounter(r))
        if r.get("bed_id") and r.get("bed"):
            beds_by_id[r["bed_id"]] = _bed_location(r["bed_id"], r["bed"])
    return encounters, beds_by_id


# --- public repository API (backend-agnostic) ---------------------------------
async def icu_admissions() -> tuple[list[Encounter], dict[str, Location]]:
    """Current ICU patients as Encounters + their bed Locations by bed id."""
    if ehr_client.configured():
        return await _carer_admissions(want_icu=True)
    return _from_rows(await hasura.get_icu_admissions())


async def non_icu_admissions() -> tuple[list[Encounter], dict[str, Location]]:
    """Non-ICU admitted patients (escalation candidates) + their bed Locations."""
    if ehr_client.configured():
        return await _carer_admissions(want_icu=False)
    return _from_rows(await hasura.get_non_icu_admissions())


async def _carer_wards_by_id() -> dict[str, Location]:
    locs = await ehr_client.search_locations({"status": "active"})
    return {loc.id: loc for loc in locs if _form_code(loc) == "wa"}


async def dirty_beds(icu_only: bool = False) -> list[dict]:
    """Dirty/cleaning beds as flat {id, ward, bed_number, status} dicts (for the
    bed-turnover feature). Sourced from CarerOS `Location?status=suspended`."""
    if ehr_client.configured():
        locs = await ehr_client.search_locations({"status": "suspended"})
        wards = await _carer_wards_by_id()
        out = []
        for loc in locs:
            if _form_code(loc) != "bd":
                continue
            ward = wards.get(ref_id(loc.partOf))
            ward_name = ward.name if ward else None
            if icu_only and "icu" not in (ward_name or "").lower():
                continue
            op = loc.operationalStatus.code if loc.operationalStatus else None
            out.append({
                "id": loc.id, "bed_number": loc.name, "ward": ward_name,
                "status": {"K": "Dirty", "H": "Cleaning"}.get(op, "suspended"),
            })
        return out
    return await (hasura.get_dirty_icu_beds() if icu_only else hasura.get_dirty_beds())


async def available_icu_beds() -> list[Location]:
    """Available (unoccupied) ICU beds as FHIR Locations."""
    if ehr_client.configured():
        beds, icu_bed_ids = await _carer_location_index()
        return [
            beds[bid] for bid in icu_bed_ids
            if (beds[bid].operationalStatus and beds[bid].operationalStatus.code == "U")
        ]
    rows = await hasura.get_available_icu_beds()
    return [loc_map.to_fhir({**r, "status": "Available", "is_active": True}) for r in rows]


async def all_admissions() -> tuple[list[Encounter], dict[str, str]]:
    """All inpatient admissions (any ward) + a bed_id -> ward-name map (for staff)."""
    if ehr_client.configured():
        encs = await ehr_client.search_encounters({"class": "IMP"})
        return encs, await _carer_ward_by_bed()
    rows = await hasura.get_admissions_with_wards()
    encs, ward_by_bed = [], {}
    for r in rows:
        encs.append(_admission_encounter(r))
        if r.get("bed_id"):
            ward_by_bed[r["bed_id"]] = (r.get("bed") or {}).get("ward")
    return encs, ward_by_bed


async def incomplete_tasks() -> list[Task]:
    """All open nursing tasks as FHIR Task (requested|in-progress)."""
    if ehr_client.configured():
        return await ehr_client.search_tasks({"status": "requested,in-progress"})
    return [task_map.to_fhir(r) for r in await hasura.get_all_incomplete_tasks()]


async def overdue_tasks() -> list[Task]:
    """Overdue nursing tasks (past due, not completed) as FHIR Task."""
    if ehr_client.configured():
        return await ehr_client.search_tasks({"overdue": "true"})
    return [task_map.to_fhir(r) for r in await hasura.get_overdue_nursing_tasks()]


def _encounter_ref(admission_id: str) -> str:
    """Cache admissions carry bare uuids; CarerOS Encounter ids are `ipd-<uuid>`."""
    aid = str(admission_id)
    return f"Encounter/{aid}" if aid.startswith(("ipd-", "em-")) else f"Encounter/ipd-{aid}"


async def nursing_tasks_for(admission_id: str, statuses: str = "requested,in-progress") -> list[Task]:
    """Open nursing tasks for one admission as FHIR Task."""
    if ehr_client.configured():
        return await ehr_client.search_tasks({"for": _encounter_ref(admission_id), "status": statuses})
    return [task_map.to_fhir(r) for r in await hasura.get_pending_nursing_tasks(admission_id)]


async def completed_task_count(admission_id: str) -> int:
    """Count of completed nursing tasks for one admission."""
    if ehr_client.configured():
        return len(await ehr_client.search_tasks({"for": _encounter_ref(admission_id), "status": "completed"}))
    return await hasura.get_completed_nursing_task_count(admission_id)


async def lab_orders() -> list[ServiceRequest]:
    """Pending/in-progress lab orders as FHIR ServiceRequest."""
    if ehr_client.configured():
        return await ehr_client.search_service_requests(
            {"category": "laboratory", "status": "active,on-hold"}
        )
    return [sr_map.to_fhir(r) for r in await hasura.get_pending_lab_orders()]


async def er_visits() -> list[Encounter]:
    """ER visits as FHIR Encounters (class=EMER). From CarerOS when configured
    (all current EMER encounters), else all ER visits via Fabric."""
    if ehr_client.configured():
        return await ehr_client.search_encounters({"class": "EMER"})
    rows = await hasura.get_er_visits()
    return [enc_map.visit_to_fhir(r) for r in rows]


async def latest_vitals(patient_token: str) -> list[Observation]:
    """Latest vitals reading for a patient as a set of FHIR Observations (empty if none)."""
    if ehr_client.configured():
        return await ehr_client.search_observations(
            {"patient": patient_token, "category": "vital-signs"}
        )
    row = await hasura.get_latest_vitals(patient_token)
    if not row:
        return []
    return obs_map.vitals_to_fhir({**row, "patient_token": patient_token})


async def mark_observation_critical(vital_id: str) -> None:
    """Record a vital as clinically critical (Observation.interpretation = AA).

    `is_critical` is Hospilot-owned enrichment, persisted in the Hospilot DB. The
    external EHR is read-only here, so this is a no-op against it (the update by
    pk simply affects 0 rows if the id isn't a local vital)."""
    await hasura.flag_critical_vitals(bare_id(vital_id))
