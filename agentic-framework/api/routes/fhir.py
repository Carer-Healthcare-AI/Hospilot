"""
Outbound FHIR R5 REST API (mounted at /fhir).

  GET /fhir/metadata          -> CapabilityStatement
  GET /fhir/{Type}/{id}       -> read           (200 resource | 404 OperationOutcome)
  GET /fhir/{Type}?{params}   -> search         (searchset Bundle)

Resources are rebuilt on the fly from the agent projection (cache) via the
shared fhirgw mappers, so the API and the agents always agree. Sensitive
vital-sign Observations are fetched live through Fabric; labs come from Hasura.
"""

import logging

from fastapi import APIRouter, Request
from fhir.resources.observation import Observation

from config import settings
from cache import redis as cache
from db.hasura import hasura
from fhirgw import bundle, terminology as T
from fhirgw.capability import CAPABILITY_STATEMENT
from fhirgw.serialization import fhir_response
from fhirgw.outcomes import not_found, not_supported
from fhirgw.mappers import location, organization, encounter, observation, patient
from fhirgw.mappers._common import coding_code
from fhirgw import profiles

logger = logging.getLogger("fhir")
router = APIRouter()

SUPPORTED = {"Patient", "Encounter", "Observation", "Location", "Organization",
             "StructureDefinition"}


def _limit(params: dict) -> int:
    raw = params.get("_count")
    try:
        n = int(raw) if raw is not None else settings.fhir_default_count
    except (ValueError, TypeError):
        n = settings.fhir_default_count
    return max(1, min(n, settings.fhir_max_count))


def _bare_id(value: str | None) -> str | None:
    """'Patient/123' or '123' -> '123'; 'system|value' -> 'value'."""
    if not value:
        return None
    return value.split("/")[-1].split("|")[-1]


# --- search per resource ------------------------------------------------------
async def _search_patient(params: dict) -> list:
    ident = _bare_id(params.get("identifier"))
    if ident:
        return [patient.patient_token_to_patient(ident)]
    tokens: set[str] = set()
    for a in await cache.get_all_admissions():
        if a.get("patient_token"):
            tokens.add(a["patient_token"])
    for v in await cache.get_all_visits():
        if v.get("patient_token"):
            tokens.add(v["patient_token"])
    return [patient.patient_token_to_patient(t) for t in sorted(tokens)]


async def _search_location(params: dict) -> list:
    status = params.get("status")
    locs = [location.to_fhir(b) for b in await cache.get_all_beds()]
    if status:
        locs = [l for l in locs if l.status == status]
    return locs


async def _search_organization(params: dict) -> list:
    typ = params.get("type")
    orgs = [organization.to_fhir(d) for d in await cache.get_all_departments()]
    if typ:
        orgs = [o for o in orgs if o.type and (o.type[0].text == typ)]
    return orgs


async def _search_encounter(params: dict) -> list:
    cls = params.get("class")
    patient_token = _bare_id(params.get("patient"))
    status = params.get("status")

    encs = []
    if cls in (None, "IMP"):
        encs += [encounter.admission_to_fhir(a) for a in await cache.get_all_admissions()]
    if cls in (None, "EMER"):
        encs += [encounter.visit_to_fhir(v) for v in await cache.get_all_visits()]

    if patient_token:
        ref = f"Patient/{patient_token}"
        encs = [e for e in encs if e.subject and e.subject.reference == ref]
    if status:
        encs = [e for e in encs if e.status == status]
    return encs


async def _search_structuredefinition(params: dict) -> list:
    """The published extension definitions (so validators can list/resolve them)."""
    return profiles.all_structure_definitions()


async def _search_observation(params: dict) -> list:
    category = params.get("category")
    patient_token = _bare_id(params.get("patient"))
    code = params.get("code")

    out: list = []
    if category in (None, T.OBS_CAT_VITAL_SIGNS) and patient_token:
        v = await hasura.get_latest_vitals(patient_token)
        if v:
            obs = observation.vitals_to_fhir(v)
            if code:
                obs = [o for o in obs if coding_code(o.code) == code]
            out += obs
    if category in (None, T.OBS_CAT_LABORATORY):
        rows = await hasura.fhir_get_lab_results(
            patient_token=patient_token, test_code=code, limit=settings.fhir_max_count
        )
        out += [observation.lab_result_to_observation(r) for r in rows]
    return out


# --- read per resource ---------------------------------------------------------
async def _read_patient(rid: str):
    return patient.patient_token_to_patient(rid)


async def _read_location(rid: str):
    bed = await cache.get(f"bed:{rid}")
    return location.to_fhir(bed) if bed else None


async def _read_organization(rid: str):
    from fhirgw import identifiers as ID
    if rid == ID.source_organization_id():        # Observation.performer target
        return organization.source_organization()
    dept = await cache.get(f"dept:{rid}")
    return organization.to_fhir(dept) if dept else None


async def _read_encounter(rid: str):
    a = await cache.get(f"admission:{rid}")
    if a:
        return encounter.admission_to_fhir(a)
    v = await cache.get(f"visit:{rid}")
    if v:
        return encounter.visit_to_fhir(v)
    return None


async def _read_structuredefinition(rid: str):
    return profiles.get(rid)


async def _read_observation(rid: str):
    live = await hasura.get_vital_observation(rid)
    if live:
        return Observation.model_validate(live)
    row = await hasura.fhir_get_lab_result_by_id(rid)     # labs
    if row:
        return observation.lab_result_to_observation(row)
    return None


_SEARCH = {
    "Patient": _search_patient, "Location": _search_location,
    "Organization": _search_organization, "Encounter": _search_encounter,
    "Observation": _search_observation, "StructureDefinition": _search_structuredefinition,
}
_READ = {
    "Patient": _read_patient, "Location": _read_location,
    "Organization": _read_organization, "Encounter": _read_encounter,
    "Observation": _read_observation, "StructureDefinition": _read_structuredefinition,
}


# --- routes (declare /metadata before the parameterized routes) ---------------
@router.get("/metadata")
async def metadata():
    return fhir_response(CAPABILITY_STATEMENT)


@router.get("/{resource_type}")
async def search(resource_type: str, request: Request):
    if resource_type not in SUPPORTED:
        raise not_supported(f"Resource type '{resource_type}' is not supported")
    params = dict(request.query_params)
    resources = (await _SEARCH[resource_type](params))[: _limit(params)]
    return fhir_response(bundle.searchset(resources, self_url=str(request.url)), public=True)


@router.get("/{resource_type}/{resource_id}")
async def read(resource_type: str, resource_id: str):
    if resource_type not in SUPPORTED:
        raise not_supported(f"Resource type '{resource_type}' is not supported")
    resource = await _READ[resource_type](resource_id)
    if resource is None:
        raise not_found(f"{resource_type}/{resource_id} not found")
    return fhir_response(resource, public=True)
