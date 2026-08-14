import logging
from datetime import datetime, timedelta, timezone

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_pharmacy_queue"
_QUEUE_THRESHOLD = 20
_TAT_STAT_LIMIT_MINUTES = 30
_TAT_ROUTINE_LIMIT_MINUTES = 120


@activity.defn
async def check_queue_length(session_id: str) -> dict:
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})
    queue = await hasura.pharmacy_get_dispensing_log(hours=8)
    pending = [e for e in queue if e.get("status") == "pending"]
    stat_waiting = [e for e in pending if e.get("is_stat")]
    queue_above_threshold = 1 if len(pending) >= _QUEUE_THRESHOLD else 0
    if queue_above_threshold:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Pharmacy queue overloaded: {len(pending)} orders pending â€” optimise dispensing workflow.",
        })
    result = {
        "queue_length": len(pending),
        "stat_waiting_count": len(stat_waiting),
        "queue_above_threshold": queue_above_threshold,
        "threshold": _QUEUE_THRESHOLD,
    }
    logger.info("check_queue_length  session=%s  queue=%d  stat=%d",
                session_id, len(pending), len(stat_waiting))
    return result


@activity.defn
async def analyze_queue_bottleneck(session_id: str) -> dict:
    queue = await hasura.pharmacy_get_dispensing_log(hours=8)
    in_progress = [e for e in queue if e.get("status") == "dispensing"]
    depts: dict[str, int] = {}
    for e in in_progress:
        dept = e.get("department") or "unknown"
        depts[dept] = depts.get(dept, 0) + 1
    bottleneck_dept = max(depts, key=lambda k: depts[k]) if depts else None
    if bottleneck_dept and depts[bottleneck_dept] >= 5:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Bottleneck detected at {bottleneck_dept}: {depts[bottleneck_dept]} orders in dispensing â€” request additional staff.",
        })
    result = {
        "in_progress_count": len(in_progress),
        "bottleneck_dept": bottleneck_dept,
        "dept_breakdown": depts,
    }
    logger.info("analyze_queue_bottleneck  session=%s  in_progress=%d  bottleneck=%s",
                session_id, len(in_progress), bottleneck_dept)
    return result


@activity.defn
async def prioritize_stat_medications(session_id: str) -> dict:
    queue = await hasura.pharmacy_get_dispensing_log(hours=8)
    stat_pending = [e for e in queue if e.get("is_stat") and e.get("status") == "pending"]
    stat_pending.sort(key=lambda e: e.get("created_at") or "")
    for e in stat_pending[:5]:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"STAT priority: {e.get('medication_name')} for {e.get('department')} â€” move to front of queue.",
        })
    result = {
        "stat_prioritized": len(stat_pending),
        "stat_orders": [{"id": str(e.get("id")), "medication": e.get("medication_name"),
                         "dept": e.get("department")} for e in stat_pending[:10]],
    }
    logger.info("prioritize_stat_medications  session=%s  stat=%d", session_id, len(stat_pending))
    return result


@activity.defn
async def escalate_tat_breach(session_id: str) -> dict:
    queue = await hasura.pharmacy_get_dispensing_log(hours=8)
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    tat_breaches = []
    for e in queue:
        if e.get("tat_minutes") is not None:
            limit = _TAT_STAT_LIMIT_MINUTES if e.get("is_stat") else _TAT_ROUTINE_LIMIT_MINUTES
            if (e.get("tat_minutes") or 0) > limit:
                tat_breaches.append(e)
    escalated = 0
    if tat_breaches:
        escalated = 1
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"TAT breach escalated: {len(tat_breaches)} dispense(s) exceeded time limit â€” notify pharmacy manager.",
        })
    result = {
        "tat_breach_count": len(tat_breaches),
        "escalated": escalated,
        "breach_orders": [{"id": str(e.get("id")), "medication": e.get("medication_name"),
                           "tat": e.get("tat_minutes")} for e in tat_breaches[:10]],
    }
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("escalate_tat_breach  session=%s  breaches=%d", session_id, len(tat_breaches))
    return result
