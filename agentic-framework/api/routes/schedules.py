"""Scheduled recurring queries API (autonomous mode, Phase 6).

CRUD + pause/resume + run-now + run-history for saved queries the scheduler
(workflows/graph/scheduler.py) re-runs on a cadence. Each fire spawns an autonomous
session on the normal pipeline, so runs show up in GET /api/queues/execution and are
listed here via GET /api/schedules/{id}/runs.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends

from agents._shared.guardrail import validate_prompt
from api.routes.auth import AuthContext, require_active_user, require_role
from db.hasura import hasura
from schemas.models import CreateScheduledQueryRequest, UpdateScheduledQueryRequest
from workflows.graph.schedule_util import next_fire_time
from workflows.graph.scheduler import spawn_run, _session_active

logger = logging.getLogger("schedules")
router = APIRouter()


def _org_for(ctx: AuthContext, org_id: str | None = None) -> str | None:
    """Effective tenant for hasura routing: org users are pinned to their own org;
    super_admin may target another via ?org_id= (mirrors sessions.py)."""
    return org_id if ctx.is_super() else ctx.org_id


async def _authorized_schedule(
    schedule_id: str, ctx: AuthContext, org_id: str | None,
) -> dict:
    """Fetch a schedule through the caller's tenant source + enforce ownership.
    404 (unknown/foreign) or 403 (exists but not the caller's, non-admin)."""
    row = await hasura.get_scheduled_query(schedule_id, org_id=org_id)
    if not row:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if ctx.role not in ("admin", "super_admin") and row.get("user_id") != ctx.user_id:
        raise HTTPException(status_code=403, detail="Not your schedule")
    return row


@router.post("/schedules")
async def create_schedule(
    body: CreateScheduledQueryRequest,
    ctx: AuthContext = Depends(require_role("doctor", "admin")),
):
    """Register a saved query to re-run on a cadence (interval or cron). The goal is
    guardrail-checked here (once) so scheduled fires can skip it."""
    if ctx.is_super() and not ctx.org_id:
        raise HTTPException(status_code=400,
                            detail="super_admin must act within an org to create schedules")

    guard = await validate_prompt(body.goal, body.constraints)
    if not guard["valid"]:
        raise HTTPException(status_code=400, detail={"error": guard["reason"], "blocked": True})

    now = datetime.now(timezone.utc)
    next_run = next_fire_time(
        schedule_kind=body.schedule_kind, interval_seconds=body.interval_seconds,
        cron_expr=body.cron, tz=body.timezone, from_dt=now,
    )
    row = await hasura.create_scheduled_query(
        goal=body.goal, constraints=body.constraints or None, name=body.name,
        schedule_kind=body.schedule_kind, interval_seconds=body.interval_seconds,
        cron_expr=body.cron, timezone=body.timezone, next_run_at=next_run.isoformat(),
        user_id=ctx.user_id, org_id=ctx.org_id,
    )
    logger.info("[ok] schedule created  id=%s  kind=%s  next=%s  user=%s",
                row.get("id"), body.schedule_kind, next_run.isoformat(), ctx.username)
    return row


@router.get("/schedules")
async def list_schedules(
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Doctors/approvers see their own schedules; admins the whole org; super_admin
    one org via ?org_id=."""
    if ctx.is_super():
        rows = await hasura.list_scheduled_queries(org_id=org_id)
    elif ctx.role == "admin":
        rows = await hasura.list_scheduled_queries(org_id=ctx.org_id)
    else:
        rows = await hasura.list_scheduled_queries(user_id=ctx.user_id, org_id=ctx.org_id)
    return {"schedules": rows}


@router.get("/schedules/{schedule_id}")
async def get_schedule(
    schedule_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    return await _authorized_schedule(schedule_id, ctx, _org_for(ctx, org_id))


@router.patch("/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: str, body: UpdateScheduledQueryRequest,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Edit goal/constraints/name/cadence or pause/resume (enabled). next_run_at is
    recomputed when the cadence changes or the schedule is (re)enabled."""
    org = _org_for(ctx, org_id)
    row = await _authorized_schedule(schedule_id, ctx, org)

    set_fields: dict = {}
    if body.goal is not None:
        set_fields["goal"] = body.goal
    if body.constraints is not None:
        set_fields["constraints"] = body.constraints
    if body.name is not None:
        set_fields["name"] = body.name
    if body.enabled is not None:
        set_fields["enabled"] = body.enabled

    # Cadence change: body._resolve sets schedule_kind when a cadence field was given.
    cadence_changed = body.schedule_kind is not None
    if cadence_changed:
        set_fields["schedule_kind"] = body.schedule_kind
        if body.schedule_kind == "cron":
            set_fields["cron_expr"] = body.cron
            set_fields["timezone"] = body.timezone or "UTC"
            set_fields["interval_seconds"] = None
        else:
            set_fields["interval_seconds"] = body.interval_seconds
            set_fields["cron_expr"] = None

    # Recompute next_run_at when the cadence changed OR when resuming a paused schedule
    # (so a long-disabled schedule doesn't fire a backlog on resume).
    resuming = body.enabled is True and not row.get("enabled")
    if cadence_changed or resuming:
        merged = {**row, **set_fields}
        next_run = next_fire_time(
            schedule_kind=merged["schedule_kind"], interval_seconds=merged.get("interval_seconds"),
            cron_expr=merged.get("cron_expr"), tz=merged.get("timezone") or "UTC",
            from_dt=datetime.now(timezone.utc),
        )
        set_fields["next_run_at"] = next_run.isoformat()

    if not set_fields:
        return row
    updated = await hasura.update_scheduled_query(schedule_id, set_fields, org_id=org)
    logger.info("[ok] schedule updated  id=%s  fields=%s  user=%s",
                schedule_id, list(set_fields), ctx.username)
    return updated


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    org = _org_for(ctx, org_id)
    await _authorized_schedule(schedule_id, ctx, org)
    await hasura.delete_scheduled_query(schedule_id, org_id=org)
    logger.info("[ok] schedule deleted  id=%s  user=%s", schedule_id, ctx.username)
    return {"deleted": True, "schedule_id": schedule_id}


@router.post("/schedules/{schedule_id}/run-now")
async def run_schedule_now(
    schedule_id: str,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Fire an ad-hoc run immediately, off-cadence. 409 if a previous run is still
    active (never stack runs). Leaves next_run_at untouched; records run bookkeeping."""
    org = _org_for(ctx, org_id)
    row = await _authorized_schedule(schedule_id, ctx, org)

    last = row.get("last_session_id")
    if last and await _session_active(last, org):
        raise HTTPException(status_code=409, detail="A previous run is still active")

    session_id = await spawn_run(row, org, schedule_id)
    now = datetime.now(timezone.utc)
    await hasura.update_scheduled_query(schedule_id, {
        "last_session_id": session_id, "last_run_at": now.isoformat(),
        "run_count": (row.get("run_count") or 0) + 1,
    }, org_id=org)
    logger.info("[ok] schedule run-now  id=%s  session=%s  user=%s",
                schedule_id, session_id, ctx.username)
    return {"schedule_id": schedule_id, "session_id": session_id, "status": "planning"}


@router.get("/schedules/{schedule_id}/runs")
async def list_schedule_runs(
    schedule_id: str,
    limit: int = 50,
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Sessions this schedule has spawned, newest first (run history)."""
    org = _org_for(ctx, org_id)
    await _authorized_schedule(schedule_id, ctx, org)
    runs = await hasura.list_scheduled_query_runs(schedule_id, limit=limit, org_id=org)
    return {"schedule_id": schedule_id, "runs": runs}
