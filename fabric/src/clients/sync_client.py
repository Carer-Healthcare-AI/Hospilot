"""HTTP client for the DB's initial-sync API (GET /api/sync/<table>).

Unlike the other plain-REST endpoints (financial/OT/ambulance), the sync API
returns a full keyset-pagination envelope — sync_id, table, schema, pagination,
rows — rather than {data:[...], total:N}. Fabric passes that envelope straight
through to the main backend, so this client returns the raw JSON unchanged.

Shares the same bearer key/auth as the plain-REST client (reuses its
`auth_headers`, which reads settings.financial_key).

Two callers with different needs, hence two functions:
  • fetch_page — one page. Used by initial_sync/ for the initial-sync API (the backend walks
    the pages itself) and by ingest/diff_poller for the lab_result keyset loop.
  • fetch_all  — every page, concatenated. Used by service/{staff,ventilator} to
    source entities the HIS exposes on no other endpoint.
"""

import logging

import httpx

from clients.rest_client import auth_headers
from config import settings

logger = logging.getLogger("sync_client")


async def fetch_page(
    table: str,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    sync_id: str | None = None,
) -> dict:
    """GET one keyset page from the DB's /api/sync/<table>.

    Returns the full upstream envelope unchanged. Raises httpx.HTTPStatusError
    on a non-2xx response so the caller can map the DB's status code through.
    """
    params = {
        k: v
        for k, v in {"limit": limit, "cursor": cursor, "sync_id": sync_id}.items()
        if v is not None
    }
    url = f"{settings.sync_api_base_url.rstrip('/')}/{table}"
    async with httpx.AsyncClient(timeout=settings.upstream_timeout) as client:
        resp = await client.get(url, params=params or None, headers=auth_headers())
        resp.raise_for_status()
        return resp.json()


async def fetch_all(table: str, *, page_size: int = 200, max_pages: int = 1000) -> list[dict]:
    """Walk every keyset page of `table` and return all rows.

    Lets the ingest pollers source entities that have no FHIR feed and no plain-REST
    list endpoint upstream. Raises httpx.HTTPStatusError if the DB hasn't registered
    /api/sync/<table> yet; the pollers catch per-entity and retry next cycle (so it's
    inert, not fatal).

    `max_pages` and the `seen` cursor set are both loop guards: a repeated or
    non-advancing cursor upstream would otherwise spin forever.
    """
    rows: list[dict] = []
    cursor: str | None = None
    sync_id: str | None = None
    seen: set[str] = set()
    for _ in range(max_pages):
        env = await fetch_page(table, limit=page_size, cursor=cursor, sync_id=sync_id)
        sync_id = sync_id or env.get("sync_id")
        rows.extend(env.get("rows") or [])
        pag = env.get("pagination") or {}
        nxt = pag.get("next_cursor")
        if not pag.get("has_more") or not nxt or nxt in seen:
            break
        seen.add(nxt)
        cursor = nxt
    return rows
