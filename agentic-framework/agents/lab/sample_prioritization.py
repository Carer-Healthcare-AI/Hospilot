import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_sample_prioritization"
_ICU_ER_DEPTS = {"icu", "er", "emergency", "intensive care", "a&e"}


@activity.defn
async def check_stat_status(session_id: str) -> dict:
    """Identify STAT-priority orders and samples requiring immediate processing."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})

    orders = await hasura.lab_get_orders()
    stats  = [o for o in orders if (o.get("priority") or "").upper() == "STAT"
               and o.get("status") in ("Pending", "In Progress")]

    result = {
        "stat_count":   len(stats),
        "stat_samples": [str(o.get("id", ""))[:8] for o in stats[:10]],
    }
    logger.info("check_stat_status  session=%s  stat=%d", session_id, len(stats))
    return result


@activity.defn
async def apply_icu_er_priority(session_id: str) -> dict:
    """Apply highest-priority flag to samples from ICU or ER patients."""
    samples = await hasura.lab_get_samples()
    icu_er  = [s for s in samples if (s.get("department") or "").lower() in _ICU_ER_DEPTS]
    pending = [s for s in icu_er if s.get("collection_status") == "Pending"
               or s.get("transport_status") in ("In-Transit", "Pending")]

    for s in pending[:5]:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"ICU/ER priority applied: sample {s.get('barcode')} from {s.get('department')} â€” highest priority queue.",
        })

    result = {
        "prioritized_count": len(pending),
        "icu_er_total":       len(icu_er),
    }
    logger.info("apply_icu_er_priority  session=%s  prioritized=%d", session_id, len(pending))
    return result


@activity.defn
async def check_analyzer_available(session_id: str) -> dict:
    """Check whether a capable analyzer is currently available for immediate processing."""
    analyzers = await hasura.lab_get_analyzers()
    available = [
        a for a in analyzers
        if a.get("status") == "Online" and (a.get("current_load_pct") or 0) < 90
    ]

    result = {
        "available_count": len(available),
        "analyzers": [
            {"name": a.get("name"), "load_pct": a.get("current_load_pct")}
            for a in available[:5]
        ],
    }
    logger.info("check_analyzer_available  session=%s  available=%d", session_id, len(available))
    return result


@activity.defn
async def escalate_tat_risk(session_id: str) -> dict:
    """Escalate to supervisor when no analyzer is available for STAT processing."""
    orders = await hasura.lab_get_orders()
    stats  = [o for o in orders if (o.get("priority") or "").upper() == "STAT"
               and o.get("status") in ("Pending", "In Progress")]

    await broadcast(session_id, {
        "type": "alert", "severity": "critical",
        "message": f"TAT risk escalation: {len(stats)} STAT order(s) pending â€” no analyzer available.",
    })

    result = {"escalated": 1 if stats else 0, "at_risk_count": len(stats)}
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("escalate_tat_risk  session=%s  at_risk=%d", session_id, len(stats))
    return result
