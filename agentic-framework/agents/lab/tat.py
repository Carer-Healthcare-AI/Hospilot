import logging
from datetime import datetime, timezone

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_tat_optimization"

TAT_LIMITS = {"stat": 60, "urgent": 90, "routine": 120}


def _age_minutes(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int((datetime.now(timezone.utc) - dt).total_seconds() / 60)
    except Exception:
        return 0


@activity.defn
async def check_tat_threshold(session_id: str) -> dict:
    """Check pending/in-progress orders against SLA thresholds; flag overdue ones."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})

    orders  = await hasura.lab_get_orders()
    active  = [o for o in orders if o.get("status") in ("Pending", "In Progress")]
    overdue, stat_overdue = [], []

    for o in active:
        priority = (o.get("priority") or "Routine").lower()
        age      = _age_minutes(o.get("ordered_at"))
        if age > TAT_LIMITS.get(priority, 120):
            overdue.append({**o, "age_minutes": age, "limit": TAT_LIMITS.get(priority, 120)})
            if priority == "stat":
                stat_overdue.append(o)

    if overdue:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"{len(overdue)} order(s) exceed TAT threshold â€” review processing queue.",
        })

    result = {
        "active_count":  len(active),
        "overdue_count": len(overdue),
        "stat_overdue":  len(stat_overdue),
        "tat_exceeded":  1 if overdue else 0,
        "overdue_orders": [{"id": o.get("id"), "priority": o.get("priority"), "age_minutes": o.get("age_minutes")} for o in overdue[:10]],
    }
    logger.info("check_tat_threshold  session=%s  active=%d  overdue=%d", session_id, len(active), len(overdue))
    return result


@activity.defn
async def analyze_tat_bottleneck(session_id: str) -> dict:
    """Identify the processing stage where TAT delays are concentrated."""
    orders = await hasura.lab_get_orders()

    pending     = [o for o in orders if o.get("status") == "Pending"]
    in_progress = [o for o in orders if o.get("status") == "In Progress"]

    bottleneck_stage = "Pre-Analytical" if len(pending) > len(in_progress) else "Analytical"
    bottleneck_count = max(len(pending), len(in_progress))

    await broadcast(session_id, {
        "type": "alert", "severity": "warning",
        "message": f"TAT bottleneck identified at {bottleneck_stage} stage â€” {bottleneck_count} sample(s) held.",
    })

    result = {
        "bottleneck_stage": bottleneck_stage,
        "bottleneck_count": bottleneck_count,
        "pending_count":    len(pending),
        "in_progress_count": len(in_progress),
    }
    logger.info("analyze_tat_bottleneck  session=%s  stage=%s  count=%d", session_id, bottleneck_stage, bottleneck_count)
    return result


@activity.defn
async def prioritize_stat_queue(session_id: str) -> dict:
    """Move STAT samples to the front of the processing queue."""
    orders  = await hasura.lab_get_orders()
    pending = [o for o in orders if o.get("status") == "Pending"]
    stats   = [o for o in pending if (o.get("priority") or "").upper() == "STAT"]

    for o in stats[:5]:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"STAT sample prioritized: order {str(o.get('id', ''))[:8]} â€” moved to front of queue.",
        })

    result = {
        "reprioritized_count": len(stats),
        "stat_orders": [str(o.get("id", ""))[:8] for o in stats[:10]],
    }
    logger.info("prioritize_stat_queue  session=%s  reprioritized=%d", session_id, len(stats))
    return result


@activity.defn
async def escalate_tat_supervisor(session_id: str) -> dict:
    """Escalate to Lab Supervisor when TAT has not been restored."""
    orders = await hasura.lab_get_orders()
    active = [o for o in orders if o.get("status") in ("Pending", "In Progress")]

    await broadcast(session_id, {
        "type": "alert", "severity": "critical",
        "message": f"TAT escalation to Lab Supervisor â€” {len(active)} active order(s) with unresolved delays.",
    })

    result = {"escalated": 1, "active_orders_count": len(active)}
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("escalate_tat_supervisor  session=%s  active=%d", session_id, len(active))
    return result
