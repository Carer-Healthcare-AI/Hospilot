"""Scheduled recurring queries (autonomous mode, Phase 6).

A saved query (hospilot_app.scheduled_queries) is re-run on a cadence -- a fixed
interval (every 6h/24h) or a cron calendar -- as an unattended autonomous background
job. This loop mirrors the approval reaper (workflows/graph/reaper.py): a single
long-lived asyncio task, launched in main.py's lifespan, that scans every active
org's tenant source each tick for rows whose next_run_at is due and fires each one
down the EXISTING autonomous submission path -- exactly the two calls POST /api/sessions
makes:

    hasura.create_session(..., autonomous=True, scheduled_query_id=<id>)
    runner.start_planning(..., autonomous=True)

So fired sessions plan+execute in the background with no plan-approval wait, share the
Phase 2 concurrency semaphore, and show up live in GET /api/queues/execution. State
lives in Postgres (next_run_at), so schedules survive restarts.

Overlap: if a schedule's previous run is still active we SKIP this fire (never stack
runs of the same schedule) -- next_run_at is still advanced so it tries again next
cadence.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from cache import redis as cache
from config import settings
from db.hasura import hasura
from workflows.graph.schedule_util import next_fire_time

logger = logging.getLogger(__name__)


def _next_fire(row: dict, from_dt: datetime) -> datetime:
    return next_fire_time(
        schedule_kind=row["schedule_kind"],
        interval_seconds=row.get("interval_seconds"),
        cron_expr=row.get("cron_expr"),
        tz=row.get("timezone") or "UTC",
        from_dt=from_dt,
    )


async def _session_active(session_id: str, org_id: str | None) -> bool:
    """True while a prior run is still executing/queued/paused/pending -- the
    overlap-skip test. Checks the live Redis index sets first (cheap), then the
    authoritative DB status."""
    try:
        live = (set(await cache.get_running_session_ids())
                | set(await cache.get_queued_session_ids())
                | set(await cache.get_paused_session_ids()))
        if session_id in live:
            return True
    except Exception:  # noqa: BLE001
        pass  # Redis hiccup -- fall through to the DB check
    try:
        rows = await hasura.get_sessions_min([session_id], org_id=org_id)
        return (rows.get(session_id) or {}).get("status") in ("pending", "running")
    except Exception:  # noqa: BLE001
        return False


async def spawn_run(row: dict, org_id: str | None, scheduled_query_id: str) -> str:
    """Spawn one autonomous session for a schedule on the normal submission path and
    return its session_id. Does NOT run the guardrail (the goal was validated once at
    schedule-create time) and does NOT touch next_run_at -- the caller does run
    bookkeeping. Shared by the loop and the run-now endpoint."""
    from workflows.graph.runner import start_planning  # late import to avoid cycle

    goal = row["goal"]
    constraints = row.get("constraints") or ""
    session_id = str(uuid.uuid4())
    await hasura.create_session(
        session_id, goal, constraints, pipeline={},
        user_id=row.get("user_id"), autonomous=True,
        org_id=org_id, scheduled_query_id=scheduled_query_id,
    )
    await start_planning(session_id, goal, constraints, autonomous=True, org_id=org_id or "")
    return session_id


async def fire_scheduled_query(row: dict, org_id: str | None, now: datetime) -> str | None:
    """A cadence tick for one schedule: skip if the previous run is still active,
    otherwise spawn a run and advance next_run_at. Returns the spawned session_id, or
    None if skipped."""
    schedule_id = row["id"]
    next_at = _next_fire(row, now)

    last = row.get("last_session_id")
    if last and await _session_active(last, org_id):
        logger.info("scheduler skip (previous run still active)  schedule=%s  last_session=%s",
                    schedule_id, last)
        await hasura.bump_scheduled_query_next_run(schedule_id, next_at.isoformat(), org_id=org_id)
        return None

    session_id = await spawn_run(row, org_id, schedule_id)
    await hasura.mark_scheduled_query_fired(
        schedule_id, next_at.isoformat(), session_id,
        (row.get("run_count") or 0) + 1, now.isoformat(), org_id=org_id,
    )
    logger.info("scheduler fired  schedule=%s  session=%s  next_run=%s",
                schedule_id, session_id, next_at.isoformat())
    return session_id


async def scan_due_schedules() -> int:
    """Scan every active org's tenant source once for due schedules and fire them.
    Returns the number of runs spawned. Multi-tenant: mirrors the reaper -- loops the
    org registry, falling back to the default source only if it isn't loaded yet."""
    now = datetime.now(timezone.utc)
    try:
        await hasura.ensure_org_registry()
        org_ids: list[str | None] = [o["id"] for o in hasura.active_orgs()] or [None]
    except Exception:  # noqa: BLE001
        org_ids = [None]

    fired = 0
    for org_id in org_ids:
        try:
            due = await hasura.fetch_due_scheduled_queries(now.isoformat(), org_id=org_id)
        except Exception:  # noqa: BLE001
            logger.exception("scheduler scan query failed  org=%s", org_id)
            continue
        for row in due:
            try:
                if await fire_scheduled_query(row, org_id, now):
                    fired += 1
            except Exception:  # noqa: BLE001
                logger.exception("scheduler fire failed  schedule=%s  org=%s", row.get("id"), org_id)
    return fired


async def start_scheduler() -> None:
    """Run the scheduler loop forever (launched as a background task at startup)."""
    interval = settings.scheduler_scan_interval_seconds
    logger.info("[ok] query scheduler started  interval=%ds  min_query_interval=%ds",
                interval, settings.scheduled_query_min_interval_seconds)
    while True:
        await asyncio.sleep(interval)
        try:
            await scan_due_schedules()
        except Exception:  # noqa: BLE001
            logger.exception("scheduler iteration failed")
