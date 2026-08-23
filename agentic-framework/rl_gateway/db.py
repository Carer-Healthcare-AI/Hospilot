"""Tenant-scoped async DB writes for the allocation.* schema — the executor seam filler.

Uses psycopg (async), the same driver db/provisioning.py uses, and the same DSN-swap trick to
reach a tenant's database. `tenant_transaction` yields an `execute(sql, params)` callable that
persist.py / reward.py consume; the whole block commits on clean exit or rolls back on error,
giving the engine's one-transaction audit guarantee.

Multi-tenant note: org_slug 'default' (Carer) lives in the control-plane DB (unprefixed);
every other org is hospilot_org_<slug>. Mirrors db/provisioning.py's naming.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

import psycopg
from psycopg.rows import dict_row

from config import settings

log = logging.getLogger("rl_gateway.db")


def _admin_dsn() -> str:
    dsn = settings.postgres_admin_dsn or getattr(settings, "database_url", "")
    if not dsn:
        raise RuntimeError("no postgres_admin_dsn / database_url configured for allocation writes")
    return dsn


def _swap_dbname(dsn: str, dbname: str) -> str:
    return re.sub(r"(postgres(?:ql)?://[^/]+/)[^?]*", rf"\g<1>{dbname}", dsn)


# The default/Carer org's data DB is NOT the admin DSN's db (that's the control-plane carerdb);
# it is the Hasura 'default' source's database (observed: carerehr). Org tenants follow the
# hospilot_org_<slug> naming used by provisioning. Override the default with ALLOCATION_DEFAULT_DB.
_DEFAULT_ORG_DB = os.getenv("ALLOCATION_DEFAULT_DB", "carerehr")


def tenant_dsn(org_slug: str | None) -> str:
    dbname = (
        _DEFAULT_ORG_DB
        if not org_slug or org_slug == "default"
        else f"hospilot_org_{org_slug.replace('-', '_')}"
    )
    return _swap_dbname(_admin_dsn(), dbname)


async def slug_for_org(org_id: str | None) -> str:
    """org_id (from AuthContext) -> tenant slug for DB routing. 'default' (Carer, unprefixed
    control-plane DB) when org_id is None or unknown."""
    if not org_id:
        return "default"
    from db.hasura import hasura
    try:
        await hasura.ensure_org_registry()
        for org in hasura.active_orgs():
            if org.get("id") == org_id:
                return org.get("slug") or "default"
    except Exception as exc:  # noqa: BLE001
        log.warning("slug_for_org(%s) failed, using default: %s", org_id, exc)
    return "default"


@asynccontextmanager
async def tenant_transaction(org_slug: str | None) -> AsyncIterator[Callable]:
    """One transaction against the tenant DB. Yields execute(sql, params, fetch=False);
    with fetch=True it returns the rows as dicts (for the reward loop's SELECTs)."""
    async with await psycopg.AsyncConnection.connect(
        tenant_dsn(org_slug), row_factory=dict_row
    ) as aconn:
        async def execute(sql: str, params: tuple, fetch: bool = False):
            cur = await aconn.execute(sql, params)
            return await cur.fetchall() if fetch else None
        yield execute
    # clean exit commits; an exception inside the block rolls back
