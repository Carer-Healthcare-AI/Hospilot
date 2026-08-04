"""HTTP client for the DB's FHIR R5 API (the upstream Fabric transforms).

Fabric is the FHIR *client*. It GETs canonical FHIR R5 resources from the DB's
FHIR server (`settings.ehr_fhir_base_url`) and hands the parsed `fhir.resources`
models to the transform layer. For writes it sends FHIR back (PATCH/PUT).

`_normalize` smooths over real-world R5 quirks so `fhir.resources` parses upstream
payloads:
  • naive datetimes (no timezone) → assume UTC (`…Z`)
  • `Encounter.reason[].value` sent as an object → wrap in the R5 list
  • resource-level `identifier` sent as an object → wrap in the R5 list
  • `ServiceRequest.code` sent as a bare CodeableConcept → wrap as CodeableReference
  • `Composition.subject`/`author` sent as a single object → wrap in the R5 list
Unparseable resources are skipped (logged) so one bad row can't fail a census.
"""

import logging
import re

import httpx

from config import settings
from fhir.resources.encounter import Encounter
from fhir.resources.location import Location
from fhir.resources.observation import Observation
from fhir.resources.organization import Organization
from fhir.resources.patient import Patient
from fhir.resources.task import Task
from fhir.resources.servicerequest import ServiceRequest
from fhir.resources.composition import Composition
from fhir.resources.specimen import Specimen
from fhir.resources.device import Device
from fhir.resources.medicationrequest import MedicationRequest
try:
    from fhir.resources.inventoryitem import InventoryItem as _InventoryItem
    InventoryItem = _InventoryItem
except ImportError:
    InventoryItem = None

logger = logging.getLogger("fhir_client")

_NAIVE_DT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$")  # no Z / offset


def configured() -> bool:
    return bool(settings.ehr_fhir_base_url)


def _headers(extra: dict | None = None) -> dict:
    h = {"Accept": "application/fhir+json"}
    if settings.ehr_fhir_api_key:
        h["Authorization"] = f"Bearer {settings.ehr_fhir_api_key}"
    if extra:
        h.update(extra)
    return h


def _normalize(node):
    """Smooth over the upstream server's R5 deviations so fhir.resources parses."""
    if isinstance(node, dict):
        rt = node.get("resourceType")
        reason = node.get("reason")
        if isinstance(reason, list):
            for r in reason:
                if isinstance(r, dict) and isinstance(r.get("value"), dict):
                    r["value"] = [r["value"]]
        if rt:
            if isinstance(node.get("identifier"), dict):
                node["identifier"] = [node["identifier"]]
            if rt == "ServiceRequest":
                code = node.get("code")
                if isinstance(code, dict) and "concept" not in code and "reference" not in code:
                    node["code"] = {"concept": code}
                node.pop("orderDetail", None)
                # R5 requires ServiceRequest.subject; the DB currently omits it, which
                # would make every lab order fail validation and get skipped. Inject a
                # placeholder so the order still parses (patient_token stays empty until
                # the DB adds subject = Patient/{patient_token}).
                if not node.get("subject"):
                    node["subject"] = {"display": "unknown"}
            if rt == "Composition":
                for fld in ("subject", "author"):
                    if isinstance(node.get(fld), dict):
                        node[fld] = [node[fld]]
        return {k: _normalize(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_normalize(x) for x in node]
    if isinstance(node, str) and _NAIVE_DT.match(node):
        return node + "Z"
    return node


async def _get(path: str, params: dict | None = None) -> dict:
    base = settings.ehr_fhir_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=settings.upstream_timeout) as client:
        resp = await client.get(f"{base}/{path}", params=params, headers=_headers())
        resp.raise_for_status()
        return resp.json()


async def _get_or_none(path: str) -> dict | None:
    """GET a single resource, returning None on 404 (so a missing id is a clean
    404 for the caller, not a 500)."""
    try:
        return await _get(path)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


async def _search(resource_type: str, params: dict) -> list[dict]:
    bundle = await _get(resource_type, params)
    return [e["resource"] for e in (bundle.get("entry") or []) if e.get("resource")]


def _parse(model_cls, raw_list: list[dict], label: str) -> list:
    out = []
    for raw in raw_list:
        try:
            out.append(model_cls.model_validate(_normalize(raw)))
        except Exception as exc:  # tolerate per-resource R5 deviations
            logger.warning("skip unparseable %s: %s", label, str(exc)[:160])
    return out


def _parse_one(model_cls, raw: dict | None, label: str):
    if not raw:
        return None
    try:
        return model_cls.model_validate(_normalize(raw))
    except Exception as exc:
        logger.warning("skip unparseable %s: %s", label, str(exc)[:160])
        return None


# ─── searches ─────────────────────────────────────────────────────────────────
async def search_encounters(params: dict) -> list[Encounter]:
    return _parse(Encounter, await _search("Encounter", params), "Encounter")


async def search_locations(params: dict) -> list[Location]:
    return _parse(Location, await _search("Location", params), "Location")


async def search_observations(params: dict) -> list[Observation]:
    return _parse(Observation, await _search("Observation", params), "Observation")


async def search_organizations(params: dict) -> list[Organization]:
    return _parse(Organization, await _search("Organization", params), "Organization")


async def search_patients(params: dict) -> list[Patient]:
    return _parse(Patient, await _search("Patient", params), "Patient")


async def search_patients_by_phone(normalized_phone: str) -> list[Patient]:
    """Search Patient by phone number, trying ?phone= first then ?telecom= as fallback.

    The FHIR adapter may support only one of the two params. `phone` (standard FHIR
    ContactPoint system=phone search) is tried first; on 400/404/501 (param not
    supported) or an empty result, we retry with the generic `telecom` alias.
    """
    for param in ("phone", "telecom"):
        try:
            results = _parse(Patient, await _search("Patient", {param: normalized_phone}), "Patient")
            if results or param == "telecom":
                return results
            # phone returned empty — retry with telecom (adapter may store under different param)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 404, 501) and param == "phone":
                logger.info(
                    "FHIR ?phone= returned %s, retrying with ?telecom=",
                    exc.response.status_code,
                )
                continue
            raise
    return []


async def search_tasks(params: dict) -> list[Task]:
    return _parse(Task, await _search("Task", params), "Task")


async def search_service_requests(params: dict) -> list[ServiceRequest]:
    return _parse(ServiceRequest, await _search("ServiceRequest", params), "ServiceRequest")


async def search_compositions(params: dict) -> list[Composition]:
    return _parse(Composition, await _search("Composition", params), "Composition")


async def search_specimens(params: dict) -> list[Specimen]:
    return _parse(Specimen, await _search("Specimen", params), "Specimen")


async def search_devices(params: dict) -> list[Device]:
    return _parse(Device, await _search("Device", params), "Device")


async def search_medication_requests(params: dict) -> list[MedicationRequest]:
    return _parse(MedicationRequest, await _search("MedicationRequest", params), "MedicationRequest")


async def search_inventory_items(params: dict) -> list:
    if InventoryItem is None:
        logger.warning("fhir.resources does not include InventoryItem on this install")
        return []
    return _parse(InventoryItem, await _search("InventoryItem", params), "InventoryItem")


# ─── reads ────────────────────────────────────────────────────────────────────
async def read_encounter(rid: str) -> Encounter | None:
    return _parse_one(Encounter, await _get_or_none(f"Encounter/{rid}"), "Encounter")


async def read_location(rid: str) -> Location | None:
    return _parse_one(Location, await _get_or_none(f"Location/{rid}"), "Location")


async def read_observation(rid: str) -> Observation | None:
    return _parse_one(Observation, await _get_or_none(f"Observation/{rid}"), "Observation")


async def read_patient(rid: str) -> Patient | None:
    return _parse_one(Patient, await _get_or_none(f"Patient/{rid}"), "Patient")


# ─── writes (normalized → FHIR is done in writeback.proposals; this is transport) ────
async def patch(resource_type: str, resource_id: str, ops: list[dict]) -> dict:
    """FHIR JSON-Patch (RFC 6902) against {base}/{Type}/{id}.

    `ops` example: [{"op": "add", "path": "/interpretation", "value": [...]}].
    The DB's write contract is documented in docs/INTEGRATION.md §writes.
    """
    base = settings.ehr_fhir_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=settings.upstream_timeout) as client:
        resp = await client.patch(
            f"{base}/{resource_type}/{resource_id}",
            json=ops,
            headers=_headers({"Content-Type": "application/json-patch+json"}),
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}


async def try_patch(resource_type: str, resource_id: str, ops: list[dict]) -> bool:
    """PATCH that tolerates a 404 (resource id doesn't exist) by returning False.
    Used for vitals, where one reading is several measure-prefixed Observations and
    only some measures exist. Other HTTP errors still raise."""
    base = settings.ehr_fhir_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=settings.upstream_timeout) as client:
        resp = await client.patch(
            f"{base}/{resource_type}/{resource_id}",
            json=ops,
            headers=_headers({"Content-Type": "application/json-patch+json"}),
        )
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True


async def put(resource_type: str, resource_id: str, resource: dict) -> dict:
    """Full FHIR update — PUT {base}/{Type}/{id} with a complete resource."""
    base = settings.ehr_fhir_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=settings.upstream_timeout) as client:
        resp = await client.put(
            f"{base}/{resource_type}/{resource_id}",
            json=resource,
            headers=_headers({"Content-Type": "application/fhir+json"}),
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}


# ─── change feed (DB → Fabric incremental sync) ─────────────────────────────────
_MODEL_BY_TYPE = {
    "Encounter": Encounter, "Location": Location, "Observation": Observation,
    "Organization": Organization, "Patient": Patient, "Task": Task,
    "ServiceRequest": ServiceRequest, "Composition": Composition,
    "Specimen": Specimen, "Device": Device, "MedicationRequest": MedicationRequest,
    **({} if InventoryItem is None else {"InventoryItem": InventoryItem}),
}


def parse_resource(raw: dict):
    """Parse a raw FHIR resource dict (from the change feed) into its model, or None
    if the type is unknown or the payload is unparseable."""
    cls = _MODEL_BY_TYPE.get(raw.get("resourceType")) if isinstance(raw, dict) else None
    if cls is None:
        return None
    return _parse_one(cls, raw, raw.get("resourceType"))


async def get_changed_resources() -> dict:
    """GET the DB's incremental change feed (a FHIR `collection` Bundle of changed
    resources since the last acknowledgment)."""
    return await _get("Bundle/$changed-resources")


async def ack_changed_resources() -> dict:
    """Acknowledge the current change-feed snapshot so the DB clears it."""
    base = settings.ehr_fhir_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=settings.upstream_timeout) as client:
        resp = await client.post(
            f"{base}/Bundle/$changed-resources/$acknowledge",
            json={},
            headers=_headers({"Content-Type": "application/fhir+json"}),
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}
