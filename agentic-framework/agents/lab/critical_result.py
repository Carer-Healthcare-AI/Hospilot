import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_critical_result_escalation"
_CRITICAL_FLAGS = {"critical", "panic"}


@activity.defn
async def detect_critical_results(session_id: str) -> dict:
    """Scan recent lab results for critical/panic values requiring immediate action."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})

    results  = await hasura.lab_get_results()
    critical = [r for r in results if str(r.get("flag", "")).lower() in _CRITICAL_FLAGS]

    for r in critical[:5]:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": (
                f"Critical value: {r.get('test_name')} = {r.get('result_value')} {r.get('unit', '')} "
                f"â€” immediate clinician review required."
            ),
        })

    result = {
        "critical_count":   len(critical),
        "critical_results": [
            {"test": r.get("test_name"), "value": r.get("result_value"),
             "unit": r.get("unit"), "order_id": str(r.get("order_id", ""))[:8]}
            for r in critical[:10]
        ],
    }
    logger.info("detect_critical_results  session=%s  critical=%d", session_id, len(critical))
    return result


@activity.defn
async def notify_physician_critical(session_id: str) -> dict:
    """Generate physician notification alerts for each unacknowledged critical result."""
    escalations = await hasura.lab_get_critical_escalations()
    unacked     = [e for e in escalations if not e.get("physician_acknowledged_at")]

    for e in unacked[:5]:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": (
                f"Physician notification required: {e.get('test_name')} = {e.get('result_value')} "
                f"â€” awaiting acknowledgment."
            ),
        })

    result = {
        "notified_count":    len(unacked),
        "unacked_escalations": [
            {"test": e.get("test_name"), "value": e.get("result_value")} for e in unacked[:10]
        ],
    }
    logger.info("notify_physician_critical  session=%s  notified=%d", session_id, len(unacked))
    return result


@activity.defn
async def escalate_icu_er_critical(session_id: str) -> dict:
    """Trigger urgent escalation for critical results in ICU/ER patients."""
    escalations = await hasura.lab_get_critical_escalations()
    icu_er      = [e for e in escalations if e.get("is_icu_er_patient")]

    for e in icu_er[:5]:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": (
                f"URGENT ICU/ER escalation: {e.get('test_name')} = {e.get('result_value')} "
                f"â€” bed-side response required."
            ),
        })
        await hasura.lab_upsert_escalation({
            "id":               e.get("id"),
            "test_name":        e.get("test_name"),   # required (NOT NULL) on insert
            "escalation_level": "Urgent",
            "physician_notified": True,
        })

    result = {"escalated_count": len(icu_er)}
    logger.info("escalate_icu_er_critical  session=%s  escalated=%d", session_id, len(icu_er))
    return result


@activity.defn
async def log_critical_action(session_id: str) -> dict:
    """Log physician acknowledgment and mark documented escalations as closed."""
    escalations = await hasura.lab_get_critical_escalations()
    acked       = [e for e in escalations if e.get("physician_acknowledged_at") and not e.get("closed_at")]

    for e in acked:
        await hasura.lab_upsert_escalation({
            "id":               e.get("id"),
            "test_name":        e.get("test_name"),   # required (NOT NULL) on insert
            "action_documented": True,
        })

    result = {"logged": len(acked)}
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("log_critical_action  session=%s  logged=%d", session_id, len(acked))
    return result
