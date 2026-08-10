import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_drug_availability"


@activity.defn
async def check_stock_levels(session_id: str) -> dict:
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})
    items = await hasura.pharmacy_get_inventory()
    out_of_stock = [i for i in items if i.get("stock_quantity", 0) == 0]
    low_stock = [i for i in items if 0 < i.get("stock_quantity", 0) <= i.get("reorder_level", 0)]
    adequate = [i for i in items if i.get("stock_quantity", 0) > i.get("reorder_level", 0)]

    for i in out_of_stock:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"Out of stock: {i.get('medication_name')} at {i.get('location')} â€” search alternate.",
        })
    for i in low_stock:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Low stock: {i.get('medication_name')} ({i.get('stock_quantity')} {i.get('unit')}) â€” reorder needed.",
        })
    result = {
        "out_of_stock_count": len(out_of_stock),
        "low_stock_count": len(low_stock),
        "adequate_count": len(adequate),
        "out_of_stock_meds": [i.get("medication_name") for i in out_of_stock[:10]],
    }
    logger.info("check_stock_levels  session=%s  out=%d  low=%d  ok=%d",
                session_id, len(out_of_stock), len(low_stock), len(adequate))
    return result


@activity.defn
async def search_alternate_location(session_id: str) -> dict:
    items = await hasura.pharmacy_get_inventory()
    out_of_stock_names = {i.get("medication_name", "").lower()
                          for i in items if i.get("stock_quantity", 0) == 0
                          and i.get("location") == "main"}
    alternates = [i for i in items
                  if i.get("medication_name", "").lower() in out_of_stock_names
                  and i.get("location") != "main"
                  and i.get("stock_quantity", 0) > 0]

    for a in alternates:
        await broadcast(session_id, {
            "type": "alert", "severity": "info",
            "message": f"Alternate stock found: {a.get('medication_name')} at {a.get('location')} ({a.get('stock_quantity')} {a.get('unit')}).",
        })
    result = {
        "alternate_found": 1 if alternates else 0,
        "alternate_count": len(alternates),
        "alternate_location": alternates[0].get("location") if alternates else None,
        "alternates": [{"name": a.get("medication_name"), "location": a.get("location"),
                        "qty": a.get("stock_quantity")} for a in alternates[:10]],
    }
    logger.info("search_alternate_location  session=%s  found=%d", session_id, len(alternates))
    return result


@activity.defn
async def reserve_inventory(session_id: str) -> dict:
    stat_orders = await hasura.pharmacy_get_stat_orders()
    items = await hasura.pharmacy_get_inventory()
    available = {i.get("medication_name", "").lower(): i
                 for i in items if i.get("stock_quantity", 0) > 0}
    reserved = [o for o in stat_orders
                if (o.get("medication_name") or "").lower() in available]
    if reserved:
        await broadcast(session_id, {
            "type": "alert", "severity": "info",
            "message": f"Inventory reserved for {len(reserved)} STAT order(s) â€” dispensing queue updated.",
        })
    result = {
        "reserved_count": len(reserved),
        "reserved_meds": [o.get("medication_name") for o in reserved[:10]],
    }
    logger.info("reserve_inventory  session=%s  reserved=%d", session_id, len(reserved))
    return result


@activity.defn
async def escalate_critical_shortage(session_id: str) -> dict:
    items = await hasura.pharmacy_get_inventory()
    critical = [i for i in items
                if i.get("stock_quantity", 0) == 0 and i.get("location") == "main"]
    # Only escalate if no satellite alternate exists
    satellite_names = {i.get("medication_name", "").lower()
                       for i in items if i.get("location") != "main"
                       and i.get("stock_quantity", 0) > 0}
    true_shortages = [i for i in critical
                      if i.get("medication_name", "").lower() not in satellite_names]
    escalated = 0
    if true_shortages:
        escalated = 1
        meds = ", ".join(i.get("medication_name", "") for i in true_shortages[:5])
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"Critical shortage escalated: {meds} â€” no alternate stock. Contact procurement immediately.",
        })
    result = {
        "escalated": escalated,
        "shortage_medications": [i.get("medication_name") for i in true_shortages[:10]],
    }
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("escalate_critical_shortage  session=%s  escalated=%d  shortages=%d",
                session_id, escalated, len(true_shortages))
    return result
