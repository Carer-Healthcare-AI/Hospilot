"""HTTP client for an external FHIR R5 EHR (e.g. CarerOS).

When `settings.fhir_ehr_base_url` is set, the agent data layer (`fhirgw.repository`)
reads canonical FHIR resources from this server instead of the local Hasura
projection -- i.e. Hospilot treats the EHR as an external FHIR source.

`_normalize` smooths over real-world R5 quirks observed from the CarerOS server
so `fhir.resources` parses them:
  * naive datetimes (no timezone) -> assume UTC (`…Z`)
  * `Encounter.reason[].value` sent as an object -> wrap in the R5 list
Unparseable resources are skipped (logged), so one bad row can't fail a census.
"""

import logging
import re

import httpx

from config import settings
from fhir.resources.encounter import Encounter
from fhir.resources.location import Location
from fhir.resources.observation import Observation
from fhir.resources.task import Task
from fhir.resources.servicerequest import ServiceRequest
from fhir.resources.composition import Composition

logger = logging.getLogger("ehr_client")

_NAIVE_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$")  # no Z / offset


def configured() -> bool:
    return bool(settings.fhir_ehr_base_url) and not bool(settings.fabric_base_url)


def _normalize(node):
    """Smooth over the CarerOS server's R5 deviations so fhir.resources parses them."""
    if isinstance(node, dict):
        rt = node.get("resourceType")
        # Encounter.reason[].value sent as object -> R5 list
        reason = node.get("reason")
        if isinstance(reason, list):
            for r in reason:
                if isinstance(r, dict) and isinstance(r.get("value"), dict):
                    r["value"] = [r["value"]]
        if rt:
            # resource-level identifier sent as object -> R5 list
            if isinstance(node.get("identifier"), dict):
                node["identifier"] = [node["identifier"]]
            if rt == "ServiceRequest":
                # R5 code is CodeableReference; CarerOS sends a bare CodeableConcept
                code = node.get("code")
                if isinstance(code, dict) and "concept" not in code and "reference" not in code:
                    node["code"] = {"concept": code}
                node.pop("orderDetail", None)   # R5 requires orderDetail.parameter; CarerOS omits it (unused)
            if rt == "Composition":
                for fld in ("subject", "author"):  # R5 0..* ; CarerOS sends single object
                    if isinstance(node.get(fld), dict):
                        node[fld] = [node[fld]]
        return {k: _normalize(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_normalize(x) for x in node]
    if isinstance(node, str) and _NAIVE_DT.match(node):
        return node + "Z"
    return node


async def _search(resource_type: str, params: dict) -> list[dict]:
    base = settings.fhir_ehr_base_url.rstrip("/")
    headers = {"Accept": "application/fhir+json"}
    if settings.fhir_ehr_api_key:
        headers["Authorization"] = f"Bearer {settings.fhir_ehr_api_key}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{base}/{resource_type}", params=params, headers=headers)
        resp.raise_for_status()
        bundle = resp.json()
    return [e["resource"] for e in (bundle.get("entry") or []) if e.get("resource")]


def _parse(model_cls, raw_list: list[dict], label: str) -> list:
    out = []
    for raw in raw_list:
        try:
            out.append(model_cls.model_validate(_normalize(raw)))
        except Exception as exc:  # tolerate per-resource R5 deviations
            logger.warning("skip unparseable %s: %s", label, str(exc)[:160])
    return out


async def search_encounters(params: dict) -> list[Encounter]:
    return _parse(Encounter, await _search("Encounter", params), "Encounter")


async def search_locations(params: dict) -> list[Location]:
    return _parse(Location, await _search("Location", params), "Location")


async def search_observations(params: dict) -> list[Observation]:
    return _parse(Observation, await _search("Observation", params), "Observation")


async def search_tasks(params: dict) -> list[Task]:
    return _parse(Task, await _search("Task", params), "Task")


async def search_service_requests(params: dict) -> list[ServiceRequest]:
    return _parse(ServiceRequest, await _search("ServiceRequest", params), "ServiceRequest")


async def search_compositions(params: dict) -> list[Composition]:
    return _parse(Composition, await _search("Composition", params), "Composition")
