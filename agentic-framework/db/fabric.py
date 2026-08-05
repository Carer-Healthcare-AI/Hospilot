"""Fabric HTTP client -- all clinical and financial agent data routes here.

Fabric is the transformation layer that reads from the DB's FHIR/financial APIs
and returns the same dict shapes the agents already expect. Only the fetch
changes; downstream logic is untouched.

Usage:
    from db.fabric import fget, fpost

    data = await fget("/beds")
    await fpost(f"/beds/{bed_id}/status", {"status": "Available"})
"""

import logging

import httpx
from config import settings

logger = logging.getLogger("db.fabric")


def _headers() -> dict:
    headers: dict = {}
    if settings.fabric_api_key:
        headers["Authorization"] = f"Bearer {settings.fabric_api_key}"
    # Multi-tenancy: forward the executing session's org so Fabric CAN scope
    # clinical data per tenant once it becomes org-aware (it filters nothing
    # today -- clinical data is still shared; this header is the hook).
    try:
        from workflows.graph.exec_context import get_exec_ctx
        org_id = (get_exec_ctx() or {}).get("org_id")
        if org_id:
            headers["X-Org-Id"] = org_id
    except Exception:  # noqa: BLE001
        pass
    return headers


async def fget(path: str, **params):
    url = settings.fabric_base_url.rstrip("/") + path
    clean = {k: v for k, v in params.items() if v is not None}
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(url, params=clean, headers=_headers())
        r.raise_for_status()
        data = r.json()
        logger.info("GET %s  -> %d item(s)", path, len(data) if isinstance(data, list) else 1)
        return data


async def fpost(path: str, body: dict | None = None):
    url = settings.fabric_base_url.rstrip("/") + path
    logger.info("POST %s", path)
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(url, json=body, headers=_headers())
        r.raise_for_status()
        return r.json()


async def fpatch(path: str, body: dict | None = None):
    url = settings.fabric_base_url.rstrip("/") + path
    logger.info("PATCH %s", path)
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.patch(url, json=body, headers=_headers())
        r.raise_for_status()
        return r.json()
