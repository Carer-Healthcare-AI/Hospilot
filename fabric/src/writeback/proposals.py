"""Writes — queues normalized changes from the main app for the DB to pull.

Instead of PATCHing the DB directly, each write adds a PendingChange to the
in-memory store. The DB calls GET /fhir/Bundle/$pending-changes to receive all
queued changes as a FHIR R5 transaction Bundle and acknowledges receipt via
POST /fhir/Bundle/$pending-changes/$acknowledge, after which the queue is cleared.
"""

from fhirgw import terminology as T
from writeback.change_store import PendingChange, get_change_store, new_change_id, now_iso

# Logical Kafka entity each change_type acknowledges against (drives the ack event's
# `entity`). Mirrors the entities the read-direction feed publishes, so the backend can
# correlate an ack to the same record it optimistically updated.
CHANGE_TYPE_ENTITY = {
    "bed_status": "bed",
    "triage_score": "visit",
    "discharge_ready": "admission",
    "transfer_pending": "admission",
    "critical_vital": "vital",
    "ai_discharge_note": "discharge_summary",
    "slot_book": "doctor_slot",
    "appointment_create": "appointment",
    "surgery_reschedule": "ot_surgery",
}

# Whether each change_type must be routed through the DB-side approval workflow before it
# is written to the HIS (surfaced as `approvalNeeded` on each snapshot Bundle entry — see
# the Diff_Engine ApprovalWrite2HIS spec). bed_status is overridden per status value in
# update_bed_status, since one change_type covers both reservation (approval) and cleaning
# (no approval). Actions absent from the spec table default to False.
CHANGE_TYPE_APPROVAL = {
    "bed_status": False,        # overridden per status in update_bed_status
    "triage_score": False,
    "discharge_ready": True,
    "transfer_pending": True,
    "critical_vital": False,
    "ai_discharge_note": True,
    "slot_book": False,
    "appointment_create": False,
    "surgery_reschedule": True,   # surgery moves are high-impact -> hospital approves in fabric_approval_queue
}

# Bed statuses that require approval (bed reservation); cleaning/occupancy/etc. do not. The
# raw status is read here because terminology.operational_status is lossy (reserved and
# occupied collapse to the same code), so the distinction is gone by bundle-build time.
_BED_STATUS_NEEDS_APPROVAL = {"reserved"}


def _pref(prefix: str, rid: str) -> str:
    rid = str(rid)
    return rid if rid.startswith(prefix) else prefix + rid


async def update_bed_status(bed_id: str, status: str):
    """Queue a bed status change for the HIS.

    `raw` carries Hospilot's own word alongside the FHIR code, because
    terminology.operational_status is lossy: reserved, vacating and occupied all map to
    "O". Sending only the code would tell the HIS "Occupied" when we mean "reserved",
    so the bundle also writes the bed-raw-status extension (see bundle._ops_for) — the
    same extension Fabric reads back in fhirgw.mappers.location.to_internal.
    """
    op = T.operational_status(status)
    if not op:
        raise ValueError(f"Unknown bed status: {status}")
    await get_change_store().add(PendingChange(
        change_type="bed_status",
        resource_type="Location",
        resource_id=_pref("bed-", bed_id),
        http_method="PATCH",
        payload={"code": op[0], "display": op[1], "raw": status},
        timestamp=now_iso(),
        change_id=new_change_id(),
        entity=CHANGE_TYPE_ENTITY["bed_status"],
        record_id=str(bed_id),
        approval_needed=status.lower() in _BED_STATUS_NEEDS_APPROVAL,
    ))
    return {"ok": True}


async def set_triage_score(visit_id: str, score: int):
    await get_change_store().add(PendingChange(
        change_type="triage_score",
        resource_type="Encounter",
        resource_id=_pref("em-", visit_id),
        http_method="PATCH",
        payload={"score": score},
        timestamp=now_iso(),
        change_id=new_change_id(),
        entity=CHANGE_TYPE_ENTITY["triage_score"],
        record_id=str(visit_id),
        approval_needed=CHANGE_TYPE_APPROVAL["triage_score"],
    ))
    return {"ok": True}


async def bulk_set_triage_scores(items: list[dict]):
    return [await set_triage_score(i["visit_id"], i["score"]) for i in items]


async def update_discharge_ready(admission_id: str, ready: bool, blocked_reason: str | None = None):
    await get_change_store().add(PendingChange(
        change_type="discharge_ready",
        resource_type="Encounter",
        resource_id=_pref("ipd-", admission_id),
        http_method="PATCH",
        payload={"ready": ready, "blocked_reason": blocked_reason},
        timestamp=now_iso(),
        change_id=new_change_id(),
        entity=CHANGE_TYPE_ENTITY["discharge_ready"],
        record_id=str(admission_id),
        approval_needed=CHANGE_TYPE_APPROVAL["discharge_ready"],
    ))
    return {"ok": True}


async def set_admissions_transfer_pending(ids: list[str]):
    for aid in ids:
        await get_change_store().add(PendingChange(
            change_type="transfer_pending",
            resource_type="Encounter",
            resource_id=_pref("ipd-", aid),
            http_method="PATCH",
            payload={},
            timestamp=now_iso(),
            change_id=new_change_id(),
            entity=CHANGE_TYPE_ENTITY["transfer_pending"],
            record_id=str(aid),
            approval_needed=CHANGE_TYPE_APPROVAL["transfer_pending"],
        ))
    return [{"ok": True}] * len(ids)


async def flag_critical_vital(vital_id: str):
    await get_change_store().add(PendingChange(
        change_type="critical_vital",
        resource_type="Observation",
        resource_id=None,  # resolved at bundle-build time by trying each measure prefix
        http_method="PATCH",
        payload={"vital_id": vital_id},
        timestamp=now_iso(),
        change_id=new_change_id(),
        entity=CHANGE_TYPE_ENTITY["critical_vital"],
        record_id=str(vital_id),
        approval_needed=CHANGE_TYPE_APPROVAL["critical_vital"],
    ))
    return {"ok": True, "id": vital_id}


async def reschedule_surgery(surgery_id: str, fields: dict):
    """Queue an executable reschedule of a surgery. The PATCH targets ot_surgeries
    directly (url = ot_surgeries/{id}), and the raw update rides in the bundle entry's
    extension so the DB-side applier can update the row generically. approval_needed=True
    routes it through fabric_approval_queue for the hospital to sign off before applying."""
    await get_change_store().add(PendingChange(
        change_type="surgery_reschedule",
        resource_type="ot_surgeries",          # -> transaction url ot_surgeries/{id} -> change_queue.table_name
        resource_id=str(surgery_id),
        http_method="PATCH",
        payload={"table_name": "ot_surgeries", "operation": "update",
                 "record_id": str(surgery_id), "fields": fields},
        timestamp=now_iso(),
        change_id=new_change_id(),
        entity=CHANGE_TYPE_ENTITY["surgery_reschedule"],
        record_id=str(surgery_id),
        approval_needed=CHANGE_TYPE_APPROVAL["surgery_reschedule"],
    ))
    return {"ok": True}


async def set_ai_discharge_note(admission_id: str, note: str):
    await get_change_store().add(PendingChange(
        change_type="ai_discharge_note",
        resource_type="Composition",
        resource_id=None,  # resolved at bundle-build time via Composition search
        http_method="PATCH",
        payload={"admission_id": _pref("ipd-", admission_id), "note": note},
        timestamp=now_iso(),
        change_id=new_change_id(),
        entity=CHANGE_TYPE_ENTITY["ai_discharge_note"],
        record_id=str(admission_id),
        approval_needed=CHANGE_TYPE_APPROVAL["ai_discharge_note"],
    ))
    return {"ok": True}
