"""Build a FHIR R5 transaction Bundle from the in-memory change queue.

Each pending change becomes EXACTLY one Bundle entry — a FHIRPath Patch (Parameters
resource) for PATCH operations, or a full FHIR resource for POST operations — carrying
`fullUrl = urn:hospilot:change:<change_id>` so the DB can reference each change back in
its $confirm response. Changes are NOT merged per resource: the per-change identity is
what the two-phase confirm protocol keys on.

Changes whose resource_id is unknown at queue time (critical_vital, ai_discharge_note)
are resolved with a live DB lookup (resolve_changes) before the snapshot is committed.
"""

import json
import logging
from datetime import datetime, timezone

from clients import fhir_client as fc
from fhirgw import extensions as X, terminology as T
from writeback.change_store import PendingChange

logger = logging.getLogger("bundle")

_CHANGE_URN = "urn:hospilot:change:"   # fullUrl prefix carrying the change_id

_VITAL_MEASURES = ("hr-", "temp-", "spo2-", "rr-", "bp-", "gcs-")

_EXT_ADM_TRANSFER_PENDING = X.EXT_BASE + "admission-transfer-pending"
_EXT_DISCHARGE_AI_NOTE    = X.EXT_BASE + "discharge-ai-note"
_EXT_RAW_APPOINTMENT      = X.EXT_BASE + "raw-appointment-data"
_EXT_RAW_SURGERY_RESCHEDULE = X.EXT_BASE + "surgery-reschedule-data"


# ─── FHIRPath Patch helpers ────────────────────────────────────────────────────

def _fhirpath_op(op: dict) -> dict:
    """Convert an op descriptor to a FHIR FHIRPath Patch operation parameter."""
    parts: list[dict] = [{"name": "type", "valueCode": op["type"]}]
    if op["type"] == "add":
        parts.append({"name": "path", "valueString": op["parent"]})
        parts.append({"name": "name", "valueString": op["name"]})
        val_part: dict = {"name": "value"}
        val_part[op["value_key"]] = op["value"]
        parts.append(val_part)
    elif op["type"] == "replace":
        parts.append({"name": "path", "valueString": op["path"]})
        val_part = {"name": "value"}
        val_part[op["value_key"]] = op["value"]
        parts.append(val_part)
    elif op["type"] == "delete":
        parts.append({"name": "path", "valueString": op["path"]})
    return {"name": "operation", "part": parts}


def _parameters(ops: list[dict]) -> dict:
    return {
        "resourceType": "Parameters",
        "parameter": [_fhirpath_op(op) for op in ops],
    }


# ─── Op descriptors per change type ───────────────────────────────────────────

def _ops_for(change: PendingChange) -> list[dict]:
    p = change.payload
    match change.change_type:
        case "bed_status":
            # Two ops, because the FHIR field alone is lossy: reserved / vacating /
            # occupied all collapse to "O". The extension carries Hospilot's own word so
            # the HIS receives what we actually meant, and so a read-back returns it
            # (location.to_internal prefers bed-raw-status over operationalStatus).
            ops = [{
                "type": "replace",
                "path": "Location.operationalStatus",
                "value_key": "valueCoding",
                "value": {
                    "system": T.SYS_LOCATION_OPER_STATUS,
                    "code": p["code"],
                    "display": p["display"],
                },
            }]
            if p.get("raw"):
                ops.append({
                    "type": "add", "parent": "Location", "name": "extension",
                    "value_key": "valueExtension",
                    "value": X.ext_string(X.EXT_BED_RAW_STATUS, p["raw"]),
                })
            return ops
        case "triage_score":
            return [{
                "type": "add", "parent": "Encounter", "name": "extension",
                "value_key": "valueExtension",
                "value": X.ext_int(X.EXT_VISIT_TRIAGE_SCORE, p["score"]),
            }]
        case "discharge_ready":
            ops = [{
                "type": "add", "parent": "Encounter", "name": "extension",
                "value_key": "valueExtension",
                "value": X.ext_bool(X.EXT_ADM_DISCHARGE_READY, p["ready"]),
            }]
            if p.get("blocked_reason") is not None:
                ops.append({
                    "type": "add", "parent": "Encounter", "name": "extension",
                    "value_key": "valueExtension",
                    "value": X.ext_string(X.EXT_ADM_DISCHARGE_BLOCKED_REASON, p["blocked_reason"]),
                })
            return ops
        case "transfer_pending":
            return [{
                "type": "add", "parent": "Encounter", "name": "extension",
                "value_key": "valueExtension",
                "value": X.ext_bool(_EXT_ADM_TRANSFER_PENDING, True),
            }]
        case "critical_vital":
            return [
                {"type": "delete", "path": "Observation.interpretation"},
                {
                    "type": "add", "parent": "Observation", "name": "interpretation",
                    "value_key": "valueCodeableConcept",
                    "value": {"coding": [{
                        "system": T.SYS_INTERPRETATION,
                        "code": T.CRITICAL_INTERP[0],
                        "display": T.CRITICAL_INTERP[1],
                    }]},
                },
            ]
        case "ai_discharge_note":
            return [{
                "type": "add", "parent": "Composition", "name": "extension",
                "value_key": "valueExtension",
                "value": X.ext_string(_EXT_DISCHARGE_AI_NOTE, p["note"]),
            }]
        case "slot_book":
            return [{
                "type": "replace",
                "path": "Slot.status",
                "value_key": "valueCode",
                "value": "busy",
            }]
        case "surgery_reschedule":
            # Self-describing update: the raw {table_name, operation, record_id, fields}
            # rides in one extension; the PATCH url (ot_surgeries/{id}) names the target.
            return [{
                "type": "add", "parent": change.resource_type, "name": "extension",
                "value_key": "valueExtension",
                "value": X.ext_string(_EXT_RAW_SURGERY_RESCHEDULE, json.dumps(p)),
            }]
        case _:
            return []


# ─── Bundle entry builders ─────────────────────────────────────────────────────

def _patch_entry(resource_type: str, resource_id: str, ops: list[dict]) -> dict:
    return {
        "request": {"method": "PATCH", "url": f"{resource_type}/{resource_id}"},
        "resource": _parameters(ops),
    }


def _appointment_entry(body: dict) -> dict:
    resource: dict = {"resourceType": "Appointment", "status": "proposed"}
    participants: list[dict] = []
    _seen_refs: set[str] = set()
    for field, ref_prefix in (
        ("patient_id", "Patient"),
        ("patient",    "Patient"),
        ("provider_id", "Practitioner"),
        ("provider",    "Practitioner"),
        ("department_id", "Organization"),
        ("department",    "Organization"),
    ):
        if val := body.get(field):
            ref = f"{ref_prefix}/{val}"
            if ref not in _seen_refs:
                _seen_refs.add(ref)
                participants.append({"actor": {"reference": ref}, "status": "accepted"})
    if participants:
        resource["participant"] = participants
    if start := body.get("start") or body.get("date") or body.get("scheduled_at"):
        resource["start"] = start
    if end := body.get("end"):
        resource["end"] = end
    resource["extension"] = [{"url": _EXT_RAW_APPOINTMENT, "valueString": json.dumps(body)}]
    return {
        "request": {"method": "POST", "url": "Appointment"},
        "resource": resource,
    }


# ─── ID resolution ─────────────────────────────────────────────────────────────

def _with_resource_id(change: PendingChange, resource_type: str, resource_id: str) -> PendingChange:
    """Clone a change with its resolved resource_id, preserving identity fields
    (change_id/entity/record_id) so the in-flight snapshot and the Bundle agree."""
    return PendingChange(
        change_type=change.change_type,
        resource_type=resource_type,
        resource_id=resource_id,
        http_method=change.http_method,
        payload=change.payload,
        timestamp=change.timestamp,
        change_id=change.change_id,
        entity=change.entity,
        record_id=change.record_id,
        approval_needed=change.approval_needed,
    )


async def resolve_changes(changes: list[PendingChange]) -> list[PendingChange]:
    """Fill in resource_id for changes that couldn't know it at queue time.

    Changes whose target can't be resolved (the Observation/Composition doesn't exist)
    are DROPPED here (logged) — they never enter the in-flight snapshot, so every change
    the DB receives carries a change_id it can confirm. Called by the GET handler BEFORE
    committing the snapshot."""
    resolved: list[PendingChange] = []
    for change in changes:
        if change.change_type == "critical_vital":
            vital_id = change.payload["vital_id"]
            hit = None
            for measure in _VITAL_MEASURES:
                rid = f"{measure}{vital_id}"
                if await fc.read_observation(rid) is not None:
                    hit = rid
                    break
            if hit:
                resolved.append(_with_resource_id(change, "Observation", hit))
            else:
                logger.warning("drop critical_vital change %s: no Observation for vital %s",
                               change.change_id, vital_id)
        elif change.change_type == "ai_discharge_note":
            admission_id = change.payload["admission_id"]
            comps = await fc.search_compositions({
                "subject": f"Encounter/{admission_id}",
                # FHIR token search (system|code); CarerOS's own Composition builder
                # (fhirCompositionBuilder.js) tags discharge summaries with this LOINC
                # code. The CarerOS search route doesn't actually filter on `type` today
                # (subject/patient/_count only) — sent anyway for correctness/future-proofing.
                "type": "http://loinc.org|18842-5",
            })
            if comps:
                resolved.append(_with_resource_id(change, "Composition", comps[0].id))
            else:
                logger.warning("drop ai_discharge_note change %s: no Composition for %s",
                               change.change_id, admission_id)
        else:
            resolved.append(change)
    return resolved


# ─── Public API ────────────────────────────────────────────────────────────────

def build_snapshot_bundle(
    changes: list[PendingChange], snapshot_id: str, include_approval: bool = True
) -> dict:
    """Build a FHIR R5 transaction Bundle from an already-resolved change set.

    One entry per change (no per-resource merging), each tagged with
    `fullUrl = urn:hospilot:change:<change_id>` so the DB echoes change ids back in
    $confirm. `Bundle.id` and `Bundle.identifier` carry the snapshot id. Resolution is
    done by the caller (resolve_changes) before the snapshot is committed, so `changes`
    here already have a concrete resource_id.

    `include_approval` controls the non-standard `entry.approvalNeeded` key: the HTTP
    pull path keeps it (True) for the DB's existing applier; the kafka write leg passes
    False so the emitted Bundle is spec-clean and approval rides in the message envelope
    instead (see writeback.kafka_write_publisher)."""
    entries: list[dict] = []
    for change in changes:
        if change.http_method == "POST":
            entry = _appointment_entry(change.payload["body"])
        else:
            ops = _ops_for(change)
            if not ops:
                continue
            entry = _patch_entry(change.resource_type, change.resource_id, ops)
        entry["fullUrl"] = _CHANGE_URN + change.change_id
        if include_approval:
            entry["approvalNeeded"] = change.approval_needed
        entries.append(entry)

    return {
        "resourceType": "Bundle",
        "id": snapshot_id,
        "identifier": {"system": _CHANGE_URN + "snapshot", "value": snapshot_id},
        "type": "transaction",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry": entries,
    }
