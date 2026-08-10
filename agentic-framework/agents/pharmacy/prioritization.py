import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_medication_prioritization"
_ICU_ER_DEPTS = {"icu", "er", "emergency", "intensive care", "a&e"}


@activity.defn
async def check_stat_medication_orders(session_id: str) -> dict:
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})
    orders = await hasura.pharmacy_get_stat_orders()
    if orders:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"{len(orders)} STAT medication order(s) pending â€” priority processing required.",
        })
    result = {
        "stat_count": len(orders),
        "stat_orders": [{"id": str(o.get("id")), "medication": o.get("medication_name"),
                         "dept": o.get("department"), "status": o.get("status")} for o in orders[:10]],
    }
    logger.info("check_stat_medication_orders  session=%s  stat_count=%d", session_id, len(orders))
    return result


@activity.defn
async def apply_critical_patient_priority(session_id: str) -> dict:
    orders = await hasura.pharmacy_get_stat_orders()
    critical = [o for o in orders
                if (o.get("department") or "").lower() in _ICU_ER_DEPTS
                or o.get("order_type") == "STAT"]
    for o in critical:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"Critical priority: {o.get('medication_name')} for {o.get('department')} â€” expedite dispensing.",
        })
    result = {
        "critical_patient_count": len(critical),
        "prioritized_count": len(critical),
        "critical_orders": [{"id": str(o.get("id")), "medication": o.get("medication_name"),
                              "dept": o.get("department")} for o in critical[:10]],
    }
    logger.info("apply_critical_patient_priority  session=%s  critical=%d", session_id, len(critical))
    return result


@activity.defn
async def check_stat_availability(session_id: str) -> dict:
    stat_orders = await hasura.pharmacy_get_stat_orders()
    inventory = await hasura.pharmacy_get_inventory()
    stock_map: dict[str, int] = {}
    for item in inventory:
        name = (item.get("medication_name") or "").lower()
        stock_map[name] = stock_map.get(name, 0) + (item.get("stock_quantity") or 0)

    available, unavailable = [], []
    for o in stat_orders:
        med = (o.get("medication_name") or "").lower()
        if stock_map.get(med, 0) > 0:
            available.append(o)
        else:
            unavailable.append(o)
            await broadcast(session_id, {
                "type": "alert", "severity": "critical",
                "message": f"STAT medication unavailable: {o.get('medication_name')} â€” check alternate stock.",
            })
    result = {
        "stat_available_count": len(available),
        "stat_unavailable_count": len(unavailable),
        "unavailable_meds": [o.get("medication_name") for o in unavailable[:10]],
    }
    logger.info("check_stat_availability  session=%s  available=%d  unavailable=%d",
                session_id, len(available), len(unavailable))
    return result


@activity.defn
async def escalate_stat_shortage(session_id: str) -> dict:
    stat_orders = await hasura.pharmacy_get_stat_orders()
    inventory = await hasura.pharmacy_get_inventory()
    stock_map: dict[str, int] = {}
    for item in inventory:
        name = (item.get("medication_name") or "").lower()
        stock_map[name] = stock_map.get(name, 0) + (item.get("stock_quantity") or 0)

    shortages = [o for o in stat_orders
                 if stock_map.get((o.get("medication_name") or "").lower(), 0) == 0]

    escalated = 0
    if shortages:
        escalated = 1
        meds = ", ".join(set(o.get("medication_name", "") for o in shortages[:5]))
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"STAT shortage escalated to pharmacy lead: {meds} â€” contact procurement immediately.",
        })
    result = {"escalated": escalated, "shortage_count": len(shortages)}
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("escalate_stat_shortage  session=%s  escalated=%d", session_id, escalated)
    return result
