"""User management + new-user approval queue (multi-tenancy RBAC).

Approval chain: org admins approve/reject their OWN org's pending doctors and
approvers; pending ADMINS are super_admin's job (org admins don't even see
them in the queue). super_admin can act anywhere and optionally scope with
?org_id=.
"""
import logging

from fastapi import APIRouter, HTTPException, Depends

from api.routes.auth import AuthContext, require_role
from db.hasura import hasura
from schemas.models import UserUpdateRequest

logger = logging.getLogger("users")
router = APIRouter()


def _scope_org(ctx: AuthContext, org_id: str | None) -> str | None:
    """Org filter for list endpoints: admins are pinned to their org;
    super_admin may pass ?org_id= or see all (None)."""
    return (org_id if ctx.is_super() else ctx.org_id)


async def _get_target(user_id: str, ctx: AuthContext) -> dict:
    """Load a target user and enforce cross-org / privilege boundaries."""
    target = await hasura.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if not ctx.is_super():
        if target.get("org_id") != ctx.org_id:
            raise HTTPException(status_code=404, detail="User not found")  # don't leak
        if target.get("role") in ("admin", "super_admin"):
            raise HTTPException(status_code=403,
                                detail="Only a super admin can manage admin accounts")
    return target


@router.get("/users")
async def list_users(
    org_id: str | None = None,
    status: str | None = None,
    ctx: AuthContext = Depends(require_role("admin")),
):
    users = await hasura.list_users(org_id=_scope_org(ctx, org_id), status=status)
    if not ctx.is_super():
        users = [u for u in users if u.get("role") != "super_admin"]
    return {"users": users}


@router.get("/users/pending")
async def list_pending_users(
    org_id: str | None = None,
    ctx: AuthContext = Depends(require_role("admin")),
):
    """The new-user approval queue."""
    users = await hasura.list_users(org_id=_scope_org(ctx, org_id), status="pending")
    if not ctx.is_super():
        # Pending admins are approved by super_admin only -- keep them out of
        # the org admin's queue entirely so the responsibility is unambiguous.
        users = [u for u in users if u.get("role") not in ("admin", "super_admin")]
    return {"users": users}


@router.post("/users/{user_id}/approve")
async def approve_user(
    user_id: str,
    ctx: AuthContext = Depends(require_role("admin")),
):
    target = await _get_target(user_id, ctx)
    if target.get("status") != "pending":
        raise HTTPException(status_code=409, detail="User is not pending approval")
    updated = await hasura.update_user_status(
        user_id, "active",
        approved_by=ctx.user_id,
        org_id=(None if ctx.is_super() else ctx.org_id),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    logger.info("user approved  username=%s  by=%s", updated["username"], ctx.username)
    return updated


@router.post("/users/{user_id}/reject")
async def reject_user(
    user_id: str,
    ctx: AuthContext = Depends(require_role("admin")),
):
    target = await _get_target(user_id, ctx)
    if target.get("status") != "pending":
        raise HTTPException(status_code=409, detail="User is not pending approval")
    updated = await hasura.update_user_status(
        user_id, "rejected",
        org_id=(None if ctx.is_super() else ctx.org_id),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    logger.info("user rejected  username=%s  by=%s", updated["username"], ctx.username)
    return updated


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    body: UserUpdateRequest,
    ctx: AuthContext = Depends(require_role("admin")),
):
    """Change a user's role (doctor <-> approver) or status (active/disabled)."""
    if body.role is None and body.status is None:
        raise HTTPException(status_code=400, detail="Nothing to update")
    if user_id == ctx.user_id:
        raise HTTPException(status_code=403, detail="You cannot modify your own account here")

    target = await _get_target(user_id, ctx)
    org_guard = None if ctx.is_super() else ctx.org_id
    updated = target

    if body.role is not None:
        allowed_roles = ("doctor", "approver", "admin") if ctx.is_super() else ("doctor", "approver")
        if body.role not in allowed_roles:
            raise HTTPException(status_code=403,
                                detail=f"Role must be one of: {', '.join(allowed_roles)}")
        updated = await hasura.update_user_role(user_id, body.role, org_id=org_guard)
        if not updated:
            raise HTTPException(status_code=404, detail="User not found")

    if body.status is not None:
        if body.status not in ("active", "disabled"):
            raise HTTPException(status_code=400, detail="Status must be active or disabled")
        if target.get("status") == "pending":
            raise HTTPException(status_code=409,
                                detail="Use the approve/reject endpoints for pending users")
        updated = await hasura.update_user_status(user_id, body.status, org_id=org_guard)
        if not updated:
            raise HTTPException(status_code=404, detail="User not found")

    logger.info("user updated  username=%s  role=%s  status=%s  by=%s",
                updated["username"], body.role, body.status, ctx.username)
    return updated
