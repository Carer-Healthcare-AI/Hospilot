"""Bed occupancy and housekeeping status.

`bed` is a streamed entity, so hospilot-backend holds each bed's current state in its
internal DB. These routes exist for the questions a per-record lookup can't answer —
filtered subsets (dirty, available ICU, post-op) and the ward-graph joins behind them.

Static sub-paths are declared before `/{bed_id}` so they aren't shadowed.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from runtime._common import _or_404
from clients import fhir_client as fc
from service import clinical, transform as tx
from writeback import proposals

router = APIRouter()


@router.get("/beds", summary="All beds with ward, status and feature attributes")
async def beds():
    return await clinical.beds()


@router.get("/beds/available-icu", summary="ICU beds currently free")
async def beds_available_icu():
    return await clinical.available_icu_beds()


@router.get("/beds/dirty", summary="Beds awaiting housekeeping")
async def beds_dirty():
    return await clinical.dirty_beds()


@router.get("/beds/dirty-icu", summary="ICU beds awaiting housekeeping")
async def beds_dirty_icu():
    return await clinical.dirty_beds(icu_only=True)


@router.get("/beds/postop", summary="Free non-ICU beds, for post-operative placement")
async def beds_postop():
    return [b for b in await clinical.beds()
            if not tx.is_icu_bed(b) and b.get("status") == "Available"]


@router.get("/beds/summary", summary="Bed counts aggregated by ward and status")
async def beds_summary():
    return await clinical.beds_summary()


@router.get("/beds/{bed_id}", summary="One bed by id (with or without the bed- prefix)")
async def bed(bed_id: str):
    rid = bed_id if bed_id.startswith("bed-") else f"bed-{bed_id}"   # DB ids are bed-{uuid}
    loc = await fc.read_location(rid)
    return await _or_404(tx.bed(loc) if loc else None, f"Bed {bed_id}")


class BedStatus(BaseModel):
    status: str


@router.post("/beds/{bed_id}/status", summary="Set a bed's status (queued as a pending change)")
async def set_bed_status(bed_id: str, body: BedStatus):
    await proposals.update_bed_status(bed_id, body.status)
    return {"ok": True, "id": bed_id, "status": body.status}
