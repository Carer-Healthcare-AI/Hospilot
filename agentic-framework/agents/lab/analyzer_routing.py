import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_analyzer_routing"
_OVERLOAD_PCT = 90


@activity.defn
async def check_analyzer_overload(session_id: str) -> dict:
    """Check whether the primary chemistry analyzer is overloaded."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})

    analyzers = await hasura.lab_get_analyzers()
    primary   = [a for a in analyzers if not a.get("is_backup") and a.get("status") == "Online"]
    overloaded = [a for a in primary if (a.get("current_load_pct") or 0) >= _OVERLOAD_PCT]
    max_load   = max((a.get("current_load_pct") or 0 for a in primary), default=0)

    result = {
        "overloaded":  1 if overloaded else 0,
        "load_pct":    max_load,
        "overloaded_analyzers": [
            {"id": str(a.get("id", "")), "name": a.get("name"), "load": a.get("current_load_pct")}
            for a in overloaded
        ],
    }
    logger.info("check_analyzer_overload  session=%s  overloaded=%d  max=%d%%", session_id, len(overloaded), max_load)
    return result


@activity.defn
async def validate_alternate_analyzer(session_id: str) -> dict:
    """Validate that the backup analyzer is certified for the required test types."""
    analyzers = await hasura.lab_get_analyzers()
    backups   = [a for a in analyzers if a.get("is_backup") and a.get("status") == "Online"]
    validated = [a for a in backups if a.get("validated_tests")]

    best = min(validated, key=lambda a: a.get("current_load_pct") or 0, default=None)

    result = {
        "validated":    1 if best else 0,
        "alternate_id": str(best.get("id", "")) if best else "",
        "alternate_name": best.get("name", "") if best else "",
        "validated_tests": best.get("validated_tests", []) if best else [],
    }
    if best:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Validated backup analyzer: {best.get('name')} â€” ready for routing ({best.get('current_load_pct')}% load).",
        })
    logger.info("validate_alternate_analyzer  session=%s  validated=%s", session_id, bool(best))
    return result


@activity.defn
async def execute_sample_routing(session_id: str) -> dict:
    """Route pending samples from overloaded analyzers to the validated alternate."""
    analyzers  = await hasura.lab_get_analyzers()
    overloaded = [a for a in analyzers if not a.get("is_backup") and (a.get("current_load_pct") or 0) >= _OVERLOAD_PCT]
    backups    = [a for a in analyzers if a.get("is_backup") and a.get("status") == "Online"]

    routed = 0
    if overloaded and backups:
        routed = len(overloaded)
        for a in overloaded:
            await broadcast(session_id, {
                "type": "alert", "severity": "warning",
                "message": f"Routing: {a.get('name')} overflow â†’ {backups[0].get('name')}.",
            })

    result = {"routed_count": routed}
    logger.info("execute_sample_routing  session=%s  routed=%d", session_id, routed)
    return result


@activity.defn
async def restore_routing_capacity(session_id: str) -> dict:
    """Confirm capacity has normalised and close the routing workflow."""
    analyzers = await hasura.lab_get_analyzers()
    primary   = [a for a in analyzers if not a.get("is_backup") and a.get("status") == "Online"]
    normalised = [a for a in primary if (a.get("current_load_pct") or 0) < _OVERLOAD_PCT]

    restored = 1 if len(normalised) == len(primary) else 0
    if restored:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": "Analyzer capacity normalised â€” routing workflow closed.",
        })

    result = {"restored": restored, "normalised_count": len(normalised)}
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("restore_routing_capacity  session=%s  restored=%d", session_id, restored)
    return result
