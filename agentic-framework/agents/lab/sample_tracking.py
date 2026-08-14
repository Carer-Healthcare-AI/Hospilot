import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_sample_tracking"


@activity.defn
async def check_sample_collection(session_id: str) -> dict:
    """Identify how many samples are collected vs still pending collection."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})

    samples = await hasura.lab_get_samples()
    collected = [s for s in samples if s.get("collection_status") == "Collected"]
    pending   = [s for s in samples if s.get("collection_status") == "Pending"]

    if pending:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"{len(pending)} sample(s) not yet collected â€” notify collection team.",
        })

    result = {
        "total_samples":   len(samples),
        "collected_count": len(collected),
        "pending_count":   len(pending),
        "pending_samples": [{"barcode": s.get("barcode"), "dept": s.get("department")} for s in pending[:10]],
    }
    logger.info("check_sample_collection  session=%s  collected=%d  pending=%d", session_id, len(collected), len(pending))
    return result


@activity.defn
async def check_sample_transport(session_id: str) -> dict:
    """Check transport status for collected samples; flag delayed ones."""
    samples    = await hasura.lab_get_samples()
    collected  = [s for s in samples if s.get("collection_status") == "Collected"]
    in_transit = [s for s in collected if s.get("transport_status") == "In-Transit"]
    delayed    = [s for s in collected if s.get("transport_status") == "Delayed"]

    for s in delayed:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Transport delay: sample {s.get('barcode')} from {s.get('department')} â€” escalate transport team.",
        })

    result = {
        "in_transit":    len(in_transit),
        "delayed_count": len(delayed),
        "delayed_samples": [{"barcode": s.get("barcode"), "dept": s.get("department")} for s in delayed[:10]],
    }
    logger.info("check_sample_transport  session=%s  in_transit=%d  delayed=%d", session_id, len(in_transit), len(delayed))
    return result


@activity.defn
async def verify_sample_receipt(session_id: str) -> dict:
    """Verify which samples have been received at the lab; surface missing ones."""
    samples = await hasura.lab_get_samples()
    received = [s for s in samples if s.get("lab_receipt_status") == "Received"]
    missing  = [s for s in samples if s.get("lab_receipt_status") == "Missing"]

    for s in missing:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"Sample missing at lab: {s.get('barcode')} ({s.get('department')}) â€” initiate search.",
        })

    result = {
        "received_count": len(received),
        "missing_count":  len(missing),
        "missing_samples": [{"barcode": s.get("barcode"), "dept": s.get("department")} for s in missing[:10]],
    }
    logger.info("verify_sample_receipt  session=%s  received=%d  missing=%d", session_id, len(received), len(missing))
    return result


@activity.defn
async def trigger_sample_search(session_id: str) -> dict:
    """Trigger a search workflow for misplaced or missing samples."""
    samples   = await hasura.lab_get_samples()
    misplaced = [s for s in samples if s.get("is_misplaced") or s.get("lab_receipt_status") == "Missing"]

    for s in misplaced:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"Search workflow triggered: sample {s.get('barcode')} â€” last known dept: {s.get('department')}.",
        })

    result = {
        "search_triggered": 1 if misplaced else 0,
        "misplaced_count":  len(misplaced),
        "searched_barcodes": [s.get("barcode") for s in misplaced[:10]],
    }
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("trigger_sample_search  session=%s  misplaced=%d", session_id, len(misplaced))
    return result
