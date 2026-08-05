"""Live monitoring queues for background flows (Phase 2+).

The Execution queue is the running + queued half of the autonomous-mode picture:
every flow that is currently executing in the background, plus every flow that has
reached execution but is waiting for a concurrency slot. It is backed by the
`sessions:running` / `sessions:queued` Redis index sets maintained by the runner's
bounded-drive wrapper -- read here without any KEYS scan.

The Paused queue (Phase 4) is the other half: every flow waiting on a human --
approval-waiting, patient-identification/registration input-waiting, and user-paused
flows -- surfaced through the `hospilot_app_approval_tasks` table (`kind` discriminates
them), reconciled against the `sessions:paused` Redis index set.
"""

import logging
import time

from fastapi import APIRouter, Depends

from api.routes.auth import AuthContext, require_active_user
from cache import redis as cache
from db.hasura import hasura

logger = logging.getLogger("queues")
router = APIRouter()


async def _row(session_id: str, state: str, sessions: dict, now: float) -> dict:
    """One Execution-queue row: goal + current step + elapsed + autonomous flag."""
    sess = sessions.get(session_id) or {}
    started = await cache.get_exec_started(session_id)
    current_step = await cache.get_current_step(session_id)
    return {
        "session_id":      session_id,
        "goal":            sess.get("goal"),
        "status":          sess.get("status"),
        "state":           state,                     # "running" | "queued"
        "current_step":    current_step,              # humanized {title,status,...} or None
        "elapsed_seconds": round(now - started, 1) if started else None,
        "autonomous":      bool(sess.get("autonomous")),
    }


@router.get("/queues/execution")
async def execution_queue(
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Flows executing (or queued for a slot) in the background, with current step.

    Reads the two index sets, batch-fetches session metadata in one query, and
    returns running flows first (longest-running first), then queued flows.

    Multi-tenant: the Redis index sets are global, but session enrichment is
    routed at the caller's tenant source -- other orgs' session ids don't
    resolve there and are dropped from the response."""
    scope = org_id if ctx.is_super() else ctx.org_id
    running = await cache.get_running_session_ids()
    queued = await cache.get_queued_session_ids()
    # A session momentarily in both sets during the queued->running transition
    # counts as running.
    running_set = set(running)
    queued = [s for s in queued if s not in running_set]

    ids = list(running_set | set(queued))
    sessions = await hasura.get_sessions_min(ids, org_id=scope)
    # Tenant filter: keep only sessions that resolved in the caller's source.
    # (super_admin without ?org_id= resolves against the default source; pass
    # ?org_id= to inspect a specific tenant's queue.)
    running = [sid for sid in running if sid in sessions]
    queued = [sid for sid in queued if sid in sessions]
    now = time.time()

    rows = [await _row(sid, "running", sessions, now) for sid in running]
    rows.sort(key=lambda r: -(r["elapsed_seconds"] or 0.0))
    rows += [await _row(sid, "queued", sessions, now) for sid in queued]

    return {"running": len(running), "queued": len(queued), "flows": rows}


@router.get("/queues/paused")
async def paused_queue(
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_active_user),
):
    """Flows waiting on a human: approval-waiting, patient-input-waiting, and user-paused.

    Primary source is the pending `hospilot_app_approval_tasks` rows (all `kind`s, incl.
    user_paused), enriched with session goal / autonomous flag / display name in two batch
    queries (no N+1). The `sessions:paused` index set is reconciled in defensively so a
    user-paused flow still shows even if its DB row write lost a race -- and never via a
    KEYS scan.

    Multi-tenant: rows come from the caller's tenant source; the globally-shared
    Redis paused set is filtered through the same source so another org's paused
    flows can't leak in via the reconcile path."""
    scope = org_id if ctx.is_super() else ctx.org_id
    rows = await hasura.fetch_paused_queue(org_id=scope)
    paused_set = set(await cache.get_paused_session_ids())

    row_session_ids = {r["session_id"] for r in rows}
    ids = list(row_session_ids | paused_set)
    sessions = await hasura.get_sessions_min(ids, org_id=scope)
    names = await hasura.get_sessions_user_info(ids, org_id=scope)
    # Tenant filter for the reconcile path: only sessions that resolved in the
    # caller's source may appear.
    paused_set = {sid for sid in paused_set if sid in sessions}
    now = time.time()

    async def _paused_row(r: dict) -> dict:
        sid = r["session_id"]
        sess = sessions.get(sid) or {}
        started = await cache.get_exec_started(sid)
        return {
            "session_id":        sid,
            "approval_id":       r.get("id"),
            "goal":              sess.get("goal"),
            "autonomous":        bool(sess.get("autonomous")),
            "kind":              r.get("kind"),
            "action_type":       r.get("action_type"),
            "agent_id":          r.get("agent_id"),
            "payload":           r.get("payload"),
            "current_step":      await cache.get_current_step(sid),
            "elapsed_seconds":   round(now - started, 1) if started else None,
            "user_display_name": names.get(sid, "Unknown"),
            "created_at":        r.get("created_at"),
        }

    flows = [await _paused_row(r) for r in rows]

    # Reconcile: a session in sessions:paused with no DB row (row write lost a race)
    # still deserves a queue entry.
    missing = paused_set - row_session_ids
    for sid in missing:
        sess = sessions.get(sid) or {}
        flows.append({
            "session_id":        sid,
            "approval_id":       None,
            "goal":              sess.get("goal"),
            "autonomous":        bool(sess.get("autonomous")),
            "kind":              "user_paused",
            "action_type":       "user_paused",
            "agent_id":          None,
            "payload":           None,
            "current_step":      await cache.get_current_step(sid),
            "elapsed_seconds":   None,
            "user_display_name": names.get(sid, "Unknown"),
            "created_at":        None,
        })

    return {"paused": len(flows), "flows": flows}
