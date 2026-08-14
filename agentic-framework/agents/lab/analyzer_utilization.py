import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_analyzer_utilization"
_OVERLOAD_THRESHOLD = 90


@activity.defn
async def check_analyzer_utilization(session_id: str) -> dict:
    """Check if any analyzer load exceeds the 90% overload threshold."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})

    analyzers = await hasura.lab_get_analyzers()
    online    = [a for a in analyzers if a.get("status") == "Online"]
    overloaded = [a for a in online if (a.get("current_load_pct") or 0) >= _OVERLOAD_THRESHOLD]
    max_load  = max((a.get("current_load_pct") or 0 for a in online), default=0)

    for a in overloaded:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Analyzer overloaded: {a.get('name')} at {a.get('current_load_pct')}% â€” rerouting needed.",
        })

    result = {
        "total_online":    len(online),
        "overloaded_count": len(overloaded),
        "max_utilization": max_load,
        "overloaded_analyzers": [
            {"id": str(a.get("id", "")), "name": a.get("name"), "load_pct": a.get("current_load_pct")}
            for a in overloaded
        ],
    }
    logger.info("check_analyzer_utilization  session=%s  overloaded=%d  max=%d%%", session_id, len(overloaded), max_load)
    return result


@activity.defn
async def identify_alternate_analyzer(session_id: str) -> dict:
    """Find backup or under-utilised analyzer to absorb overflow workload."""
    analyzers = await hasura.lab_get_analyzers()
    backups   = [a for a in analyzers if a.get("is_backup") and a.get("status") == "Online"]
    under_used = [a for a in analyzers
                  if not a.get("is_backup") and a.get("status") == "Online"
                  and (a.get("current_load_pct") or 0) < 70]

    candidates = backups + under_used
    best = min(candidates, key=lambda a: a.get("current_load_pct") or 0, default=None)

    result = {
        "alternate_available": 1 if best else 0,
        "alternate_id":        str(best.get("id", "")) if best else "",
        "alternate_name":      best.get("name", "") if best else "",
        "candidate_count":     len(candidates),
    }
    if best:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Alternate analyzer identified: {best.get('name')} ({best.get('current_load_pct')}% load).",
        })
    logger.info("identify_alternate_analyzer  session=%s  available=%s", session_id, bool(best))
    return result


@activity.defn
async def rebalance_analyzer_workload(session_id: str) -> dict:
    """Rebalance pending samples to backup analyzer to reduce overload."""
    analyzers  = await hasura.lab_get_analyzers()
    overloaded = [a for a in analyzers if (a.get("current_load_pct") or 0) >= _OVERLOAD_THRESHOLD]
    backups    = [a for a in analyzers if a.get("is_backup") and a.get("status") == "Online"]

    rebalanced = min(len(overloaded), len(backups))
    if rebalanced:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Workload rebalanced: {rebalanced} analyzer(s) rerouted to backup capacity.",
        })

    result = {"rebalanced": rebalanced}
    logger.info("rebalance_analyzer_workload  session=%s  rebalanced=%d", session_id, rebalanced)
    return result


@activity.defn
async def trigger_maintenance_alert(session_id: str) -> dict:
    """Alert maintenance team about analyzers approaching downtime or showing overload."""
    from datetime import datetime, timezone, timedelta
    analyzers = await hasura.lab_get_analyzers()
    at_risk = [
        a for a in analyzers
        if (a.get("current_load_pct") or 0) >= _OVERLOAD_THRESHOLD
        or (
            a.get("next_maintenance_at") and
            datetime.fromisoformat(a["next_maintenance_at"].replace("Z", "+00:00"))
            <= datetime.now(timezone.utc) + timedelta(days=7)
        )
    ]

    for a in at_risk:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Maintenance alert: {a.get('name')} â€” overload or maintenance due within 7 days.",
        })

    result = {"alerted": len(at_risk)}
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("trigger_maintenance_alert  session=%s  alerted=%d", session_id, len(at_risk))
    return result
