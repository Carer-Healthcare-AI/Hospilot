import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from temporalio import activity

from api.routes.ws import broadcast
from cache import redis as cache
from db.hasura import hasura
from fhirgw import repository as repo

logger = logging.getLogger(__name__)


def _authored_tat_minutes(authored, now) -> int:
    if authored is None:
        return 0
    try:
        a = authored if not isinstance(authored, str) else datetime.fromisoformat(authored.replace("Z", "+00:00"))
        if getattr(a, "tzinfo", None) is None:
            a = a.replace(tzinfo=timezone.utc)
        return int((now - a).total_seconds() / 60)
    except Exception:
        return 0


@activity.defn
async def get_lab_tat_status(session_id: str) -> dict:
    """
    Check pending and in-progress lab orders. Flag overdue ones.
    STAT overdue > 60 min, Urgent overdue > 90 min, Routine overdue > 120 min.
    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_lab_tat"})

    orders = await repo.lab_orders()                    # FHIR ServiceRequest (category=laboratory)
    now = datetime.now(timezone.utc)

    # FHIR priority codes -> TAT limit (minutes)
    TAT_LIMITS = {"stat": 60, "urgent": 90, "routine": 120}
    overdue = pending = in_progress = stat_overdue = 0

    for sr in orders:
        tat = _authored_tat_minutes(sr.authoredOn, now)
        priority = (sr.priority or "routine")
        if tat > TAT_LIMITS.get(priority, 120):
            overdue += 1
            if priority == "stat":
                stat_overdue += 1
        if sr.status == "active":                       # active = ordered/pending
            pending += 1
        else:                                           # on-hold = processing
            in_progress += 1

    if overdue:
        await broadcast(session_id, {
            "type": "alert",
            "severity": "warning",
            "message": f"{overdue} lab order(s) overdue -- results not yet available."
        })

    result = {
        "total_pending": len(orders),
        "pending_count": pending,
        "in_progress_count": in_progress,
        "overdue_count": overdue,
        "stat_overdue": stat_overdue,
    }

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_lab_tat", "result": result})
    logger.info("lab tat  session=%s  pending=%d  overdue=%d", session_id, len(orders), overdue)
    return result


@activity.defn
async def get_critical_lab_results(session_id: str) -> dict:
    """
    Pull recent lab results. Flag Critical values and raise alerts.

    """
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": "sa_lab_critical"})

    results = await cache.get_all_lab_results()

    critical = [r for r in results if str(r.get("flag", "")).lower() == "critical"]
    high     = [r for r in results if str(r.get("flag", "")).lower() == "high"]
    low      = [r for r in results if str(r.get("flag", "")).lower() == "low"]
    normal   = [r for r in results if str(r.get("flag", "")).lower() == "normal"]

    for r in critical:
        await broadcast(session_id, {
            "type": "alert",
            "severity": "critical",
            "message": f"Critical lab value: {r.get('test_name', 'Unknown test')} = {r.get('result_value')} {r.get('unit', '')} -- immediate clinician review required."
        })

    result = {
        "total_results": len(results),
        "critical_count": len(critical),
        "high_count": len(high),
        "low_count": len(low),
        "normal_count": len(normal),
        "critical_tests": [{"test": r.get("test_name"), "value": r.get("result_value"), "unit": r.get("unit")} for r in critical[:5]],
    }

    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": "sa_lab_critical", "result": result})
    logger.info("lab critical  session=%s  critical=%d  high=%d  low=%d", session_id, len(critical), len(high), len(low))
    return result
