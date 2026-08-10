import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_controlled_drug_compliance"


@activity.defn
async def identify_controlled_orders(session_id: str) -> dict:
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})
    orders = await hasura.pharmacy_get_orders()
    controlled = [o for o in orders if o.get("is_controlled")]
    if controlled:
        await broadcast(session_id, {
            "type": "alert", "severity": "info",
            "message": f"{len(controlled)} controlled substance order(s) detected â€” compliance verification required.",
        })
    result = {
        "controlled_count": len(controlled),
        "controlled_orders": [{"id": str(o.get("id")), "medication": o.get("medication_name"),
                               "dept": o.get("department"), "status": o.get("status")}
                              for o in controlled[:10]],
    }
    logger.info("identify_controlled_orders  session=%s  controlled=%d", session_id, len(controlled))
    return result


@activity.defn
async def verify_controlled_authorization(session_id: str) -> dict:
    logs = await hasura.pharmacy_get_controlled_logs(hours=24)
    incomplete = [e for e in logs if not e.get("documentation_complete")]
    no_witness = [e for e in logs
                  if e.get("documentation_complete") and not e.get("witness")]
    for e in incomplete:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"Incomplete controlled drug documentation: {e.get('medication_name')} â€” record must be completed before release.",
        })
    for e in no_witness:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Missing witness signature: {e.get('medication_name')} â€” assign witness immediately.",
        })
    result = {
        "documentation_incomplete": len(incomplete),
        "missing_witness": len(no_witness),
        "incomplete_orders": [{"id": str(e.get("id")), "medication": e.get("medication_name")}
                              for e in (incomplete + no_witness)[:10]],
    }
    logger.info("verify_controlled_authorization  session=%s  incomplete=%d  no_witness=%d",
                session_id, len(incomplete), len(no_witness))
    return result


@activity.defn
async def check_inventory_variance(session_id: str) -> dict:
    logs = await hasura.pharmacy_get_controlled_logs(hours=24)
    variances = [e for e in logs if e.get("variance_detected")]
    if variances:
        for e in variances:
            await broadcast(session_id, {
                "type": "alert", "severity": "critical",
                "message": f"Controlled drug variance: {e.get('medication_name')} â€” physical count mismatch. Initiate investigation.",
            })
    result = {
        "variance_detected": 1 if variances else 0,
        "variance_count": len(variances),
        "variance_items": [{"id": str(e.get("id")), "medication": e.get("medication_name"),
                            "wastage_qty": e.get("wastage_quantity")} for e in variances[:10]],
    }
    logger.info("check_inventory_variance  session=%s  variances=%d", session_id, len(variances))
    return result


@activity.defn
async def escalate_compliance_issue(session_id: str) -> dict:
    logs = await hasura.pharmacy_get_controlled_logs(hours=24)
    variances = [e for e in logs if e.get("variance_detected")]
    incomplete = [e for e in logs if not e.get("documentation_complete")]
    escalated = 0
    critical_issues = variances + incomplete
    if critical_issues:
        escalated = 1
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"Controlled drug compliance escalated: {len(critical_issues)} issue(s) â€” notify pharmacy director and DEA liaison.",
        })
    result = {
        "escalated": escalated,
        "total_issues": len(critical_issues),
        "variance_count": len(variances),
        "incomplete_count": len(incomplete),
    }
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("escalate_compliance_issue  session=%s  escalated=%d  issues=%d",
                session_id, escalated, len(critical_issues))
    return result
