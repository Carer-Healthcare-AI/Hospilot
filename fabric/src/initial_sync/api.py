"""Initial-sync endpoints exposed to the main backend.

One-time, keyset-paginated full-table dumps used to seed the backend's internal DB from
scratch
before the Kafka change feed takes over for incremental updates. Each Fabric
endpoint forwards to the DB's matching /api/sync/<table>, passing the cursor /
sync_id / limit through and returning the DB's pagination envelope unchanged.

See docs/INITIAL_SYNC_INTEGRATION.md for the consumer contract.
"""

import logging
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query

from initial_sync import registry

router = APIRouter()
logger = logging.getLogger("sync_api")


@router.get("/sync/tables", summary="List the tables available for initial sync")
async def sync_tables():
    return {"tables": registry.TABLES, "sources": registry.TABLE_SOURCES}


@router.get(
    "/sync/{table}",
    summary="Fetch one keyset-paginated page of a table for the initial internal-DB sync",
)
async def sync_table(
    table: str,
    limit: Optional[int] = Query(
        None,
        ge=1,
        le=1000,
        description="Rows per page. Clamped to [1, 1000] upstream. Default 200.",
    ),
    cursor: Optional[str] = Query(
        None,
        description="Opaque cursor from the previous response's pagination.next_cursor. Omit on the first call.",
    ),
    sync_id: Optional[str] = Query(
        None,
        description="Correlation id returned on the first call. Pass it back on every subsequent call.",
    ),
):
    if not registry.is_valid_table(table):
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Unknown sync table '{table}'",
                "valid_tables": registry.TABLES,
            },
        )
    try:
        return await registry.page(
            table, limit=limit, cursor=cursor, sync_id=sync_id
        )
    except httpx.HTTPStatusError as exc:
        # surface the DB's status + body (400 invalid cursor, 401, 500 DB error, ...)
        detail = exc.response.json() if exc.response.content else str(exc)
        raise HTTPException(status_code=exc.response.status_code, detail=detail)
    except httpx.HTTPError as exc:
        logger.warning("initial-sync upstream error table=%s: %s", table, str(exc)[:160])
        raise HTTPException(status_code=502, detail="Initial-sync upstream unavailable")
