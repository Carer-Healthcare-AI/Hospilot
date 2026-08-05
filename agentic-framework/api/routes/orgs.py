"""Organization management (multi-tenancy control plane).

Creating / listing / updating orgs is super_admin-only. `GET /orgs/public` is
the one open endpoint: the signup form needs an org picker before any account
exists.

POST /orgs creates the registry row (status 'provisioning') and then provisions
the tenant database in the background -- creating the DB, wiring the Hasura
source, and flipping the org to 'active'. If that background step fails the org
stays 'provisioning'; scripts/provision_org.py re-runs it (idempotent).
"""
import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Depends

from api.routes.auth import AuthContext, require_role
from db.hasura import hasura
from db.provisioning import provision_org
from schemas.models import OrgCreateRequest, OrgUpdateRequest

logger = logging.getLogger("orgs")
router = APIRouter()


async def _provision_in_background(slug: str) -> None:
    """Run the (synchronous) tenant provisioning off the request path, then
    refresh the org registry cache so the app routes to the new source. A
    failure leaves the org 'provisioning' for a manual re-run -- we log, we
    don't crash the worker."""
    try:
        result = await asyncio.to_thread(provision_org, slug=slug)
        await hasura.load_org_registry()
        logger.info("org provisioned  slug=%s  source=%s", slug, result["hasura_source"])
    except Exception:
        logger.exception(
            "org provisioning FAILED  slug=%s -- org stays 'provisioning'; "
            "re-run: python scripts/provision_org.py --slug %s", slug, slug,
        )


@router.get("/orgs/public")
async def list_orgs_public():
    """Active orgs (id + name only) for the signup picker. Unauthenticated."""
    return {"organizations": await hasura.list_active_orgs_public()}


@router.post("/orgs", status_code=201)
async def create_org(
    body: OrgCreateRequest,
    background_tasks: BackgroundTasks,
    ctx: AuthContext = Depends(require_role("super_admin")),
):
    existing = [o for o in await hasura.list_orgs()
                if o["slug"] == body.slug or o["name"] == body.name]
    if existing:
        raise HTTPException(status_code=409, detail="Organization name or slug already taken")
    org = await hasura.create_org(name=body.name, slug=body.slug, created_by=ctx.user_id)
    logger.info("org created  slug=%s  by=%s -- provisioning in background", org["slug"], ctx.username)
    # Provision the tenant DB after the response is sent; the org flips to
    # 'active' when it completes (clients poll GET /orgs to observe it).
    background_tasks.add_task(_provision_in_background, org["slug"])
    return org


@router.get("/orgs")
async def list_orgs(_ctx: AuthContext = Depends(require_role("super_admin"))):
    return {"organizations": await hasura.list_orgs()}


@router.patch("/orgs/{org_id}")
async def update_org(
    org_id: str,
    body: OrgUpdateRequest,
    ctx: AuthContext = Depends(require_role("super_admin")),
):
    set_fields: dict = {}
    if body.name is not None:
        set_fields["name"] = body.name
    if body.status is not None:
        set_fields["status"] = body.status
    if not set_fields:
        raise HTTPException(status_code=400, detail="Nothing to update")
    org = await hasura.update_org(org_id, set_fields)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    logger.info("org updated  id=%s  set=%s  by=%s", org_id, set_fields, ctx.username)
    return org
