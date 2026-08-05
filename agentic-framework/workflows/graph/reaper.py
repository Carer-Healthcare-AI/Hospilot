"""Approval timeout reaper.

LangGraph's interrupt() has no built-in timeout (Temporal's wait_condition did).
This periodic job finds approval tasks that have been pending longer than the
timeout window, marks them expired, and resumes the parked session graph with
decision="timeout" -- the agent body's non-approved branch then releases any held
bed/Redis locks and returns a timeout result, exactly as the old 30-min
wait_condition path did.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from config import settings
from db.hasura import hasura

logger = logging.getLogger(__name__)

APPROVAL_TIMEOUT_MIN = 30
_SCAN_INTERVAL_SEC = 60

# Scoped to kind="approval" (Phase 4): the approval_tasks table now also carries
# patient_identification / patient_registration / user_paused rows, which resume down
# DIFFERENT paths (resume_patient_* / resume_paused_session) and must NOT be timed out
# here via resume_session(sid, "timeout"). Patient registration has its own reaper
# (reap_stale_registrations); patient_identification and user_paused have no timeout.
_STALE_QUERY = """
query StalePendingApprovals($cutoff: timestamptz!) {
  hospilot_app_approval_tasks: {P}hospilot_app_approval_tasks(
    where: {status: {_eq: "pending"}, kind: {_eq: "approval"}, created_at: {_lt: $cutoff}}
  ) { id session_id created_at }
}
"""

_EXPIRE_MUTATION = """
mutation ExpireApproval($id: uuid!) {
  update_hospilot_app_approval_tasks_by_pk: update_{P}hospilot_app_approval_tasks_by_pk(
    pk_columns: {id: $id}
    _set: {status: "rejected", decision: "timeout"}
  ) { id }
}
"""


async def reap_stale_approvals() -> int:
    """Scan once for stale pending approvals and time them out. Returns count reaped.

    Multi-tenant: approval_tasks live in one Hasura source per org, so the scan
    loops over every active org's source. If the org registry isn't available
    (migration 050 not applied yet), it falls back to the default source only."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=APPROVAL_TIMEOUT_MIN)).isoformat()
    try:
        await hasura.ensure_org_registry()
        org_ids: list[str | None] = [o["id"] for o in hasura.active_orgs()] or [None]
    except Exception:  # noqa: BLE001
        org_ids = [None]

    from workflows.graph.runner import resume_session  # late import to avoid cycle

    reaped = 0
    for org_id in org_ids:
        try:
            data = await hasura.query(_STALE_QUERY, {"cutoff": cutoff}, org_id=org_id)
        except Exception:  # noqa: BLE001
            logger.exception("reaper query failed  org=%s", org_id)
            continue

        for row in data.get("hospilot_app_approval_tasks", []):
            sid = row.get("session_id")
            if not sid:
                continue
            logger.info("reaping stale approval  id=%s  session=%s  created=%s",
                        row.get("id"), sid, row.get("created_at"))
            try:
                await hasura.query(_EXPIRE_MUTATION, {"id": row["id"]}, org_id=org_id)
            except Exception:  # noqa: BLE001
                logger.warning("could not mark approval expired  id=%s", row.get("id"))
            try:
                await resume_session(sid, "timeout")
                reaped += 1
            except Exception:  # noqa: BLE001
                logger.exception("reaper resume failed  session=%s", sid)
    return reaped


async def reap_stale_registrations() -> int:
    """Scan once for patient-registration pauses older than the timeout window and
    resume them with a timeout outcome + escalation alert. Returns count reaped.

    The flow is parked on the patient_registration interrupt (graph.patient) waiting for
    the hospital staff to create the patient. The pause is unbounded by itself (no
    approval-task row, so reap_stale_approvals never touches it); this is the bound. On
    resume the node re-resolves from Fabric -- if staff DID register the patient just in
    time it still binds; otherwise it proceeds with the provisional identity."""
    timeout_hours = settings.patient_registration_timeout_hours
    try:
        from workflows.graph import patient  # late import to avoid cycle
        stale = await patient.find_stale_registrations(timeout_hours)
    except Exception:  # noqa: BLE001
        logger.exception("registration reaper scan failed")
        return 0
    if not stale:
        return 0

    from workflows.graph.runner import resume_patient_registration  # late import
    from api.routes.ws import broadcast

    reaped = 0
    for sid, mobiles in stale:
        logger.info("reaping stale patient registration  session=%s  mobiles=%s  after=%dh",
                    sid, mobiles, timeout_hours)
        try:
            await broadcast(sid, {
                "type": "alert", "severity": "warning",
                "message": (f"Patient registration was not completed within {timeout_hours}h for "
                            f"{len(mobiles)} incoming patient(s) -- the flow has resumed with a "
                            f"provisional identity. Register the patient(s) and re-run if needed."),
            })
            await resume_patient_registration(sid, {"status": "registration_timeout", "mobiles": mobiles})
            reaped += 1
        except Exception:  # noqa: BLE001
            logger.exception("registration reaper resume failed  session=%s", sid)
    return reaped


async def start_reaper() -> None:
    """Run the reaper loop forever (launched as a background task at startup)."""
    logger.info("[ok] reaper started  interval=%ds  approval_timeout=%dmin  registration_timeout=%dh",
                _SCAN_INTERVAL_SEC, APPROVAL_TIMEOUT_MIN, settings.patient_registration_timeout_hours)
    while True:
        await asyncio.sleep(_SCAN_INTERVAL_SEC)
        try:
            await reap_stale_approvals()
        except Exception:  # noqa: BLE001
            logger.exception("reaper iteration failed")
        try:
            await reap_stale_registrations()
        except Exception:  # noqa: BLE001
            logger.exception("registration reaper iteration failed")
