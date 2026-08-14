import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_medication_substitution"


@activity.defn
async def check_unavailable_medications(session_id: str) -> dict:
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})
    orders = await hasura.pharmacy_get_pending_orders()
    inventory = await hasura.pharmacy_get_inventory()
    stock_map: dict[str, int] = {}
    for item in inventory:
        name = (item.get("medication_name") or "").lower()
        stock_map[name] = stock_map.get(name, 0) + (item.get("stock_quantity") or 0)
    unavailable = [o for o in orders
                   if stock_map.get((o.get("medication_name") or "").lower(), 0) == 0]
    for o in unavailable:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Medication unavailable: {o.get('medication_name')} â€” checking substitution rules.",
        })
    result = {
        "unavailable_count": len(unavailable),
        "unavailable_meds": [o.get("medication_name") for o in unavailable[:10]],
    }
    logger.info("check_unavailable_medications  session=%s  unavailable=%d", session_id, len(unavailable))
    return result


@activity.defn
async def search_formulary_alternatives(session_id: str) -> dict:
    orders = await hasura.pharmacy_get_pending_orders()
    inventory = await hasura.pharmacy_get_inventory()
    rules = await hasura.pharmacy_get_substitution_rules()
    stock_map: dict[str, int] = {}
    for item in inventory:
        name = (item.get("medication_name") or "").lower()
        stock_map[name] = stock_map.get(name, 0) + (item.get("stock_quantity") or 0)
    unavailable_meds = {(o.get("medication_name") or "").lower() for o in orders
                        if stock_map.get((o.get("medication_name") or "").lower(), 0) == 0}
    rule_map = {(r.get("original_drug") or "").lower(): r for r in rules}
    matches = []
    for med in unavailable_meds:
        rule = rule_map.get(med)
        if not rule:
            continue
        sub = (rule.get("substitute_drug") or "").lower()
        if stock_map.get(sub, 0) > 0:
            matches.append(rule)
            await broadcast(session_id, {
                "type": "alert", "severity": "info",
                "message": f"Substitute available: {rule.get('original_drug')} â†’ {rule.get('substitute_drug')} ({rule.get('therapeutic_class')}).",
            })
    result = {
        "substitute_available": 1 if matches else 0,
        "substitute_count": len(matches),
        "substitutes": [{"original": r.get("original_drug"), "substitute": r.get("substitute_drug"),
                         "needs_approval": r.get("requires_physician_approval")} for r in matches[:10]],
    }
    logger.info("search_formulary_alternatives  session=%s  substitutes=%d", session_id, len(matches))
    return result


@activity.defn
async def request_physician_approval(session_id: str) -> dict:
    rules = await hasura.pharmacy_get_substitution_rules()
    orders = await hasura.pharmacy_get_pending_orders()
    inventory = await hasura.pharmacy_get_inventory()
    stock_map: dict[str, int] = {}
    for item in inventory:
        name = (item.get("medication_name") or "").lower()
        stock_map[name] = stock_map.get(name, 0) + (item.get("stock_quantity") or 0)
    rule_map = {(r.get("original_drug") or "").lower(): r for r in rules}
    needs_approval = []
    for o in orders:
        med = (o.get("medication_name") or "").lower()
        if stock_map.get(med, 0) == 0:
            rule = rule_map.get(med)
            if rule and rule.get("requires_physician_approval"):
                needs_approval.append({"order": o, "rule": rule})
    for item in needs_approval:
        await broadcast(session_id, {
            "type": "alert", "severity": "info",
            "message": f"Physician approval requested: substitute {item['rule'].get('substitute_drug')} for {item['rule'].get('original_drug')}.",
        })
    result = {
        "approved_count": len(needs_approval),
        "pending_approval": [{"medication": n["order"].get("medication_name"),
                               "substitute": n["rule"].get("substitute_drug")} for n in needs_approval[:10]],
    }
    logger.info("request_physician_approval  session=%s  approvals=%d", session_id, len(needs_approval))
    return result


@activity.defn
async def update_substitution_order(session_id: str) -> dict:
    rules = await hasura.pharmacy_get_substitution_rules()
    orders = await hasura.pharmacy_get_pending_orders()
    inventory = await hasura.pharmacy_get_inventory()
    stock_map: dict[str, int] = {}
    for item in inventory:
        name = (item.get("medication_name") or "").lower()
        stock_map[name] = stock_map.get(name, 0) + (item.get("stock_quantity") or 0)
    rule_map = {(r.get("original_drug") or "").lower(): r for r in rules}
    auto_substituted = [o for o in orders
                        if stock_map.get((o.get("medication_name") or "").lower(), 0) == 0
                        and (r := rule_map.get((o.get("medication_name") or "").lower()))
                        and not r.get("requires_physician_approval")
                        and stock_map.get((r.get("substitute_drug") or "").lower(), 0) > 0]
    result = {
        "substitution_updated": len(auto_substituted),
        "substituted_meds": [o.get("medication_name") for o in auto_substituted[:10]],
    }
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("update_substitution_order  session=%s  substituted=%d", session_id, len(auto_substituted))
    return result
