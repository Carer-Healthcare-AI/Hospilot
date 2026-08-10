import logging
from dataclasses import dataclass

from temporalio import activity

from cache import redis as cache
from db.hasura import hasura
from api.routes.ws import broadcast

logger = logging.getLogger(__name__)


@dataclass
class HousekeepingDispatchInput:
    session_id: str
    beds: list


@activity.defn
async def clean_vacated_beds(session_id: str) -> dict:
    """Census + dispatch in one step. Broadcasts as sa_dirty_bed_recovery."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_dirty_bed_recovery"})

    discharged = await hasura.get_recently_discharged_beds()

    all_beds = await cache.get_all_beds()
    bed_lookup = {(b.get("id") or b.get("bed_id")): b for b in all_beds}

    beds = []
    for d in discharged:
        bed_id  = d.get("id")
        bed_rec = bed_lookup.get(bed_id) or {}
        beds.append({
            "id":           bed_id,
            "ward":         bed_rec.get("ward") or d.get("ward") or "Unknown Ward",
            "bed_number":   bed_rec.get("bed_number") or bed_rec.get("name") or bed_id,
            "admission_id": d.get("admission_id"),
        })

    for bed in beds:
        ward    = bed.get("ward", "Unknown Ward")
        bed_num = bed.get("bed_number", bed.get("id", "?"))
        await broadcast(session_id, {
            "type": "alert", "severity": "info",
            "message": f"{ward} -- Bed {bed_num}: patient discharged, room ready for cleaning.",
        })

    if beds:
        await hasura.write_audit(
            session_id=session_id,
            agent_id="bed_agent",
            event_type="bed_cleaning_dispatched",
            payload={"beds_dispatched": len(beds), "beds": beds},
        )

    result = {"dispatched": len(beds), "vacated_count": len(beds), "beds": beds}
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_dirty_bed_recovery", "result": {"dispatched": len(beds), "vacated_count": len(beds)}})
    logger.info("bed cleaning  session=%s  dispatched=%d", session_id, len(beds))
    return result


@activity.defn
async def get_vacated_beds(session_id: str) -> list:
    """Find beds from recently discharged admissions that need cleaning."""
    await broadcast(session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_hk_census",
    })

    discharged = await hasura.get_recently_discharged_beds()

    # Enrich with ward/bed_number from Redis bed cache
    all_beds = await cache.get_all_beds()
    bed_lookup = {
        (b.get("id") or b.get("bed_id")): b
        for b in all_beds
    }

    beds = []
    for d in discharged:
        bed_id  = d.get("id")
        bed_rec = bed_lookup.get(bed_id) or {}
        beds.append({
            "id":           bed_id,
            "ward":         bed_rec.get("ward") or d.get("ward") or "Unknown Ward",
            "bed_number":   bed_rec.get("bed_number") or bed_rec.get("name") or bed_id,
            "admission_id": d.get("admission_id"),
        })

    await broadcast(session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_hk_census",
        "result": {"vacated_count": len(beds)},
    })
    logger.info("housekeeping census  session=%s  vacated=%d", session_id, len(beds))
    return beds


@activity.defn
async def dispatch_housekeeping(inp: HousekeepingDispatchInput) -> dict:
    """Broadcast cleaning tasks for each vacated bed and write audit log."""
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_hk_dispatch",
    })

    for bed in inp.beds:
        ward     = bed.get("ward", "Unknown Ward")
        bed_num  = bed.get("bed_number", bed.get("id", "?"))
        await broadcast(inp.session_id, {
            "type":     "alert",
            "severity": "info",
            "message":  f"{ward} -- Bed {bed_num}: patient discharged, room ready for cleaning.",
        })

    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="housekeeping_agent",
        event_type="housekeeping_dispatched",
        payload={"beds_dispatched": len(inp.beds), "beds": inp.beds},
    )

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_hk_dispatch",
        "result": {"dispatched": len(inp.beds)},
    })
    logger.info("housekeeping dispatched  session=%s  beds=%d", inp.session_id, len(inp.beds))
    return {"dispatched": len(inp.beds)}
