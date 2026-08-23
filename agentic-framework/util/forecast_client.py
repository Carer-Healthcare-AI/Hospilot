"""
Thin async HTTP client for the Hospilot ML forecast service (:18000).

Mirrors the proxy in api/routes/simulation.py, but is meant to be called from
inside agent prediction activities rather than from a route. Crucially it NEVER
raises on a service problem: it returns None instead, so a forecast task can
degrade gracefully (fall back to its heuristic / reactive path) when the model
service is unconfigured, unreachable, slow, or returns a non-2xx status.
"""

import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


async def forecast(path: str, payload: dict) -> dict | None:
    """POST `payload` to the forecast service at `path` (e.g. "/pharmacy/demand").

    Returns the parsed JSON response on success, or None if the service is
    unconfigured, unreachable, times out, returns a non-2xx status, or the body
    is not JSON. Callers should treat None as "no forecast available" and fall
    back rather than error.
    """
    if not settings.forecast_base_url:
        logger.debug("forecast skipped: FORECAST_BASE_URL not configured (path=%s)", path)
        return None

    url = f"{settings.forecast_base_url}{path}"
    # The forecast service authenticates with an X-API-Key header (a bare
    # Authorization: Bearer is rejected with 401 "Invalid or missing X-API-Key").
    headers = {}
    if settings.forecast_api_key:
        headers["X-API-Key"] = settings.forecast_api_key

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.TimeoutException:
            logger.warning("forecast %s timed out after %.0fs", path, _TIMEOUT)
            return None
        except httpx.HTTPStatusError as exc:
            logger.warning("forecast %s -> HTTP %s: %s",
                           path, exc.response.status_code, exc.response.text[:200])
            return None
        except httpx.RequestError as exc:
            logger.warning("forecast %s unreachable: %s", path, exc)
            return None

    try:
        return resp.json()
    except ValueError as exc:
        logger.warning("forecast %s returned non-JSON body: %s", path, exc)
        return None
