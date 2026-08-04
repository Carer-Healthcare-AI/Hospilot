"""Operating theatre — the queries and writes a per-record lookup can't cover.

Deliberately small. ot_room, ot_room_status, ot_schedule and ot_surgery are all
streamed to hospilot-backend and cached, so agents read theatre state from the internal DB;
those HTTP reads existed with no callers and were removed. What's left is the one
uncached table and the reschedule write. See service/ot.py.
"""

from fastapi import APIRouter

from service import ot as ot_svc
from writeback import proposals

router = APIRouter()


@router.get("/ot/equipment-usage", summary="OT equipment usage records (empty if none)")
async def get_equipment_usage():
    return await ot_svc.equipment_usage()


@router.post(
    "/ot/surgery-schedule/{surgery_id}/reschedule",
    summary="Reschedule a surgery to a new theatre slot (queued as a pending change)",
    status_code=202,
)
async def reschedule_surgery(surgery_id: str, body: dict):
    fields = {k: body.get(k) for k in
              ("scheduled_date", "scheduled_start_time", "scheduled_end_time", "ot_room_id", "status")}
    return await proposals.reschedule_surgery(surgery_id, fields)
