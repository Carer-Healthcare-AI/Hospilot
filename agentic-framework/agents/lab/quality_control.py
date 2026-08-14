import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_quality_control"


@activity.defn
async def check_qc_status(session_id: str) -> dict:
    """Check QC pass/fail status for all active analyzers in the last 24 hours."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})

    logs   = await hasura.lab_get_qc_logs(hours=24)
    failed = [l for l in logs if l.get("qc_status") == "Fail"]
    passed = [l for l in logs if l.get("qc_status") == "Pass"]

    for l in failed:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": (
                f"QC FAILED: analyzer {str(l.get('analyzer_id', ''))[:8]} "
                f"shift={l.get('shift')} deviation={l.get('deviation_pct')}% â€” result release stopped."
            ),
        })

    result = {
        "total_qc_runs": len(logs),
        "passed_count":  len(passed),
        "failed_count":  len(failed),
        "qc_failed":     1 if failed else 0,
        "failed_logs":   [
            {"analyzer_id": str(l.get("analyzer_id", ""))[:8], "shift": l.get("shift"),
             "deviation_pct": l.get("deviation_pct")}
            for l in failed[:5]
        ],
    }
    logger.info("check_qc_status  session=%s  passed=%d  failed=%d", session_id, len(passed), len(failed))
    return result


@activity.defn
async def trigger_recalibration(session_id: str) -> dict:
    """Initiate recalibration for analyzers with failing QC."""
    logs   = await hasura.lab_get_qc_logs(hours=24)
    failed = [l for l in logs if l.get("qc_status") == "Fail" and not l.get("recalibration_triggered")]

    triggered = 0
    for l in failed:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"Recalibration triggered for analyzer {str(l.get('analyzer_id', ''))[:8]} â€” testing halted.",
        })
        triggered += 1

    result = {"recalibration_triggered": triggered}
    logger.info("trigger_recalibration  session=%s  triggered=%d", session_id, triggered)
    return result


@activity.defn
async def repeat_qc_check(session_id: str) -> dict:
    """Evaluate whether QC passed after recalibration; allow resumption if passed."""
    logs = await hasura.lab_get_qc_logs(hours=8)

    recalibrated_passed = [
        l for l in logs
        if l.get("qc_status") == "Pass" and l.get("recalibration_triggered")
    ]
    recalibrated_failed = [
        l for l in logs
        if l.get("qc_status") == "Fail" and l.get("recalibration_triggered")
    ]

    for l in recalibrated_passed:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"QC restored after calibration: analyzer {str(l.get('analyzer_id', ''))[:8]} â€” testing resumed.",
        })

    result = {
        "passed":        len(recalibrated_passed),
        "repeat_passed": 1 if recalibrated_passed else 0,
        "still_failing": len(recalibrated_failed),
    }
    logger.info("repeat_qc_check  session=%s  passed=%d  still_failing=%d", session_id, len(recalibrated_passed), len(recalibrated_failed))
    return result


@activity.defn
async def compliance_alert(session_id: str) -> dict:
    """Generate accreditation compliance alert when QC failures exceed tolerance."""
    logs   = await hasura.lab_get_qc_logs(hours=24)
    failed = [l for l in logs if l.get("qc_status") == "Fail"]

    alert_sent = 0
    if len(failed) >= 2:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"ACCREDITATION RISK: {len(failed)} QC failure(s) in 24h â€” compliance team notified.",
        })
        alert_sent = 1

    result = {"alerted": alert_sent, "qc_failures_24h": len(failed)}
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("compliance_alert  session=%s  failures=%d  alerted=%d", session_id, len(failed), alert_sent)
    return result
