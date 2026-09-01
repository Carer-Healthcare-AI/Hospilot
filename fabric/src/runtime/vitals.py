"""Patient vitals — latest readings, critical flags, raw Observations.

Vitals are NOT polled by diff_poller (they change too fast to diff usefully) — the
change feed publishes them and these routes fetch on demand.

Static sub-paths are declared before `/{vital_id}` so they aren't shadowed.
"""

from fastapi import APIRouter, Query

from runtime._common import _or_404
from clients import fhir_client as fc
from input_transform import clinical
from writeback import proposals

router = APIRouter()


@router.get("/vitals/latest", summary="Latest vitals for one patient token")
async def vitals_latest(patient: str = Query(...)):
    return await clinical.latest_vitals(patient)


@router.get("/vitals/latest-bulk", summary="Latest vitals for MANY patients in one call")
async def vitals_latest_bulk(
    patients: str | None = Query(
        None, description="Optional comma-separated patient tokens to restrict the "
                          "result to. Omit for every patient with vitals."),
):
    """One upstream read instead of one per patient.

    `patient` is the only patient-scoping search param this upstream FHIR server
    supports, so /vitals/latest cannot take a list -- callers needing N patients
    fired N calls. This does a single unfiltered vital-signs search and groups by
    subject, which is how /vitals/critical already reads.

    The response carries `complete`. It is False when the upstream reported more
    Observations than it returned (this server ignores _offset and emits no
    `next` link, so a big hospital can silently get a prefix). On complete=False
    the caller MUST fall back to per-patient /vitals/latest for the tokens it
    still needs -- treating a short map as authoritative would hide a patient's
    critical reading.
    """
    toks = [t.strip() for t in patients.split(",") if t.strip()] if patients else None
    return await clinical.latest_vitals_bulk(toks)


@router.get("/vitals/critical", summary="Vitals currently flagged critical, across all patients")
async def vitals_critical():
    return await clinical.critical_vitals()


@router.get("/vitals/observations/{observation_id}",
            summary="One raw FHIR Observation by id (untransformed)")
async def vital_observation(observation_id: str):
    obs = await fc.read_observation(observation_id)
    return await _or_404(obs, f"Observation {observation_id}")


@router.post("/vitals/{vital_id}/critical", summary="Flag a vital as critical (queued as a pending change)")
async def flag_vital_critical(vital_id: str):
    await proposals.flag_critical_vital(vital_id)
    return {"ok": True, "id": vital_id, "is_critical": True}
