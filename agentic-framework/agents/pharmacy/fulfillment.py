import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_medication_fulfillment"


@activity.defn
async def check_prescription_received(session_id: str) -> dict:
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})
    orders = await hasura.pharmacy_get_orders()
    pending = [o for o in orders if o.get("status") == "pending"]
    if pending:
        await broadcast(session_id, {
            "type": "alert", "severity": "info",
            "message": f"{len(pending)} prescription(s) received and awaiting dispensing.",
        })
    result = {
        "prescription_count": len(orders),
        "pending_count": len(pending),
        "pending_orders": [{"id": str(o.get("id")), "medication": o.get("medication_name"),
                            "dept": o.get("department"), "type": o.get("order_type")} for o in pending[:10]],
    }
    logger.info("check_prescription_received  session=%s  total=%d  pending=%d",
                session_id, len(orders), len(pending))
    return result


@activity.defn
async def check_medication_availability(session_id: str) -> dict:
    orders = await hasura.pharmacy_get_pending_orders()
    inventory = await hasura.pharmacy_get_inventory()
    stock_map: dict[str, int] = {}
    for item in inventory:
        name = (item.get("medication_name") or "").lower()
        stock_map[name] = stock_map.get(name, 0) + (item.get("stock_quantity") or 0)

    available, unavailable = [], []
    for o in orders:
        med = (o.get("medication_name") or "").lower()
        if stock_map.get(med, 0) > 0:
            available.append(o)
        else:
            unavailable.append(o)
            await broadcast(session_id, {
                "type": "alert", "severity": "warning",
                "message": f"Medication unavailable: {o.get('medication_name')} â€” trigger substitution check.",
            })
    result = {
        "available_count": len(available),
        "unavailable_count": len(unavailable),
        "unavailable_meds": [o.get("medication_name") for o in unavailable[:10]],
    }
    logger.info("check_medication_availability  session=%s  available=%d  unavailable=%d",
                session_id, len(available), len(unavailable))
    return result


@activity.defn
async def track_dispensing_progress(session_id: str) -> dict:
    orders = await hasura.pharmacy_get_orders()
    dispensing = [o for o in orders if o.get("status") == "dispensing"]

    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    delayed = []
    for o in dispensing:
        prescribed_at = o.get("prescribed_at")
        if prescribed_at:
            try:
                if isinstance(prescribed_at, str):
                    from datetime import datetime as dt, timezone
                    prescribed_dt = dt.fromisoformat(prescribed_at.replace("Z", "+00:00"))
                else:
                    prescribed_dt = prescribed_at
                elapsed = (now - prescribed_dt).total_seconds() / 60
                limit = 30 if o.get("order_type") == "STAT" else 120
                if elapsed > limit:
                    delayed.append(o)
            except Exception:
                pass

    for o in delayed:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Dispensing delay: {o.get('medication_name')} ({o.get('department')}) â€” escalate to pharmacist.",
        })
    result = {
        "dispensing_count": len(dispensing),
        "delayed_count": len(delayed),
        "delayed_meds": [o.get("medication_name") for o in delayed[:10]],
    }
    logger.info("track_dispensing_progress  session=%s  dispensing=%d  delayed=%d",
                session_id, len(dispensing), len(delayed))
    return result


@activity.defn
async def close_fulfilled_orders(session_id: str) -> dict:
    orders = await hasura.pharmacy_get_orders()
    dispensed = [o for o in orders if o.get("status") == "dispensed"]
    result = {
        "closed_count": len(dispensed),
        "dispensed_meds": [o.get("medication_name") for o in dispensed[:10]],
    }
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("close_fulfilled_orders  session=%s  dispensed=%d", session_id, len(dispensed))
    return result
