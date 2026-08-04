"""Inpatient admissions, discharge readiness, and discharge summaries.

`admission` and `discharge_ready` are streamed entities, so per-record state lives
in the backend's internal DB. What's here are the cohort questions — who is discharge
eligible, how many will clear within N hours — plus the proposals.

Static sub-paths are declared before `/{admission_id}` so they aren't shadowed.
"""

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Query
from pydantic import BaseModel

from runtime._common import _or_404
from clients import fhir_client as fc
from service import clinical, transform as tx
from writeback import proposals

router = APIRouter()


@router.get("/admissions", summary="All current inpatient admissions")
async def admissions():
    return await clinical.all_admissions()


@router.get("/admissions/icu", summary="Admissions currently in an ICU bed")
async def admissions_icu():
    return await clinical.icu_admissions()


@router.get("/admissions/non-icu", summary="Admissions outside the ICU")
async def admissions_non_icu():
    return await clinical.non_icu_admissions()


@router.get("/admissions/with-wards", summary="Admissions joined to their ward via the bed graph")
async def admissions_with_wards():
    return await clinical.admissions_with_wards()


@router.get("/admissions/discharge-eligible", summary="Admissions still open, so eligible for discharge review")
async def admissions_discharge_eligible():
    return [a for a in await clinical.all_admissions() if (a.get("status") in (None, "admitted"))]


@router.get("/admissions/discharge-ready", summary="Admissions already flagged discharge-ready")
async def admissions_discharge_ready():
    return [a for a in await clinical.all_admissions() if a.get("discharge_ready")]


@router.get("/admissions/discharge-ready-count", summary="Count of discharge-ready admissions")
async def admissions_discharge_ready_count():
    return {"count": await clinical.discharge_ready_count()}


@router.get("/admissions/discharge-horizon",
            summary="Count of admissions discharge-ready or expected to clear within N hours")
async def admissions_discharge_horizon(hours: int = Query(24)):
    cutoff = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    n = sum(1 for a in await clinical.all_admissions()
            if a.get("discharge_ready") or (a.get("expected_discharge_at") or "") and (a["expected_discharge_at"] <= cutoff))
    return {"hours": hours, "count": n}


class TransferPending(BaseModel):
    ids: list[str]


@router.post("/admissions/transfer-pending", summary="Flag admissions as transfer-pending (queued as pending changes)")
async def admissions_transfer_pending(body: TransferPending):
    await proposals.set_admissions_transfer_pending(body.ids)
    return {"ok": True, "count": len(body.ids)}


@router.get("/admissions/{admission_id}", summary="One admission by id (with or without the ipd- prefix)")
async def admission(admission_id: str):
    rid = admission_id if admission_id.startswith("ipd-") else f"ipd-{admission_id}"   # DB ids are ipd-{uuid}
    enc = await fc.read_encounter(rid)
    return await _or_404(tx.admission(enc) if enc else None, f"Admission {admission_id}")


class DischargeReady(BaseModel):
    ready: bool
    blocked_reason: str | None = None


@router.post("/admissions/{admission_id}/discharge-ready",
             summary="Set discharge readiness, with an optional blocked reason (queued as a pending change)")
async def set_discharge_ready(admission_id: str, body: DischargeReady):
    await proposals.update_discharge_ready(admission_id, body.ready, body.blocked_reason)
    return {"ok": True, "id": admission_id, "ready": body.ready}


# ─── discharge summaries ────────────────────────────────────────────────────────
class AINote(BaseModel):
    note: str


@router.post("/discharge-summaries/{admission_id}/ai-note",
             summary="Attach an AI-drafted discharge note to an admission (queued as a pending change)")
async def set_ai_note(admission_id: str, body: AINote):
    await proposals.set_ai_discharge_note(admission_id, body.note)
    return {"ok": True, "admission_id": admission_id}
