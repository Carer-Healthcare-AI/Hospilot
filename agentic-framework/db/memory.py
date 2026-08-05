"""HTTP client for the isolated `memory` sidecar service.

langmem hard-requires langgraph>=0.6, which is incompatible with the backend's
pinned langgraph==0.2.74 (the whole workflow/HITL/checkpointer runtime). So
langmem lives in a separate service (own venv/langgraph 0.6) and the backend
talks to it over HTTP -- exactly like db.fabric talks to Fabric.

Two capabilities:
  - msummarize(...)  -> in-session running-summary (langmem.short_term.summarize_messages)
  - mextract(...)    -> cross-session fact extraction/consolidation (create_memory_manager)

Everything degrades gracefully: an unset memory_service_url or any transport
error returns a safe empty result so POST /api/ask never fails because of memory.
"""

import logging

import httpx
from config import settings

logger = logging.getLogger("db.memory")

_TIMEOUT = 30.0


def _enabled() -> bool:
    return bool(settings.memory_service_url)


def _headers() -> dict:
    headers: dict = {}
    if settings.memory_service_api_key:
        headers["Authorization"] = f"Bearer {settings.memory_service_api_key}"
    return headers


async def _post(path: str, body: dict) -> dict | None:
    if not _enabled():
        return None
    url = settings.memory_service_url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=body, headers=_headers())
            r.raise_for_status()
            return r.json()
    except Exception as exc:  # noqa: BLE001 -- memory is best-effort, never fatal
        logger.warning("memory service %s failed: %s", path, exc)
        return None


async def msummarize(
    messages: list[dict], running_summary: str | None, budget: int,
) -> dict | None:
    """Compress `messages` into a rolling summary + recent turns.

    Returns {"running_summary": str, "recent_messages": [...]} or None (caller
    then just uses the raw recent turns it already has)."""
    return await _post("/summarize", {
        "messages": messages,
        "running_summary": running_summary,
        "max_tokens_before_summary": budget,
    })


async def mextract(
    messages: list[dict], existing: list[dict],
) -> list[dict] | None:
    """Extract/consolidate durable per-user facts from a conversation.

    `existing` is the user's current fact set (so langmem can update/dedup).
    Returns the FULL consolidated active set [{kind, content, salience}, ...],
    or None on failure (caller leaves stored memories untouched)."""
    out = await _post("/memory/extract", {"messages": messages, "existing": existing})
    if out is None:
        return None
    return out.get("memories", [])
