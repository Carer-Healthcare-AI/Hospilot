"""Patient vitals — latest readings, critical flags, raw Observations.

Vitals are NOT polled by diff_poller (they change too fast to diff usefully) — the
change feed publishes them and these routes fetch on demand.

Static sub-paths are declared before `/{vital_id}` so they aren't shadowed.
"""

from fastapi import APIRouter, Query

from runtime._common import _or_404
from clients import fhir_client as fc
from service import clinical
from writeback import proposals

router = APIRouter()


@router.get("/vitals/latest", summary="Latest vitals for one patient token")
async def vitals_latest(patient: str = Query(...)):
    return await clinical.latest_vitals(patient)


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
