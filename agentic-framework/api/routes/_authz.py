"""Shared session-authorization helper (multi-tenancy + RBAC).

Tenant isolation is physical (DB-per-tenant): looking a session up through the
caller's org source means another org's session id simply doesn't resolve --
we answer 404 without revealing whether it exists elsewhere. On top of that,
`owner_or_admin` enforces WITHIN-org visibility: doctors/approvers may only
touch their own sessions, admins any session in their org, super_admin
anything (their lookups run unrouted or against ?org_id=).
"""
from fastapi import HTTPException

from api.routes.auth import AuthContext
from db.hasura import hasura


async def authorized_session(
    session_id: str,
    ctx: AuthContext,
    *,
    owner_or_admin: bool = False,
    org_id_hint: str | None = None,
) -> dict:
    """Fetch a session through the caller's tenant source and authorize access.

    Returns the session row. Raises 404 (unknown/foreign session) or 403
    (exists in the caller's org but ownership rules forbid it).

    `org_id_hint`: super_admin may pass ?org_id= to route the lookup at a
    specific tenant; other callers are always pinned to their own org.
    """
    lookup_org = (org_id_hint if ctx.is_super() else ctx.org_id)
    session = await hasura.get_session(session_id, org_id=lookup_org)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if owner_or_admin and ctx.role not in ("admin", "super_admin"):
        if session.get("user_id") != ctx.user_id:
            raise HTTPException(status_code=403, detail="Not your session")

    return session
