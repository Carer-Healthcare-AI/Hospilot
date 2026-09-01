"""Fabric HTTP client -- all clinical and financial agent data routes here.

Fabric is the transformation layer that reads from the DB's FHIR/financial APIs
and returns the same dict shapes the agents already expect. Only the fetch
changes; downstream logic is untouched.

Usage:
    from db.fabric import fget, fpost

    data = await fget("/beds")
    await fpost(f"/beds/{bed_id}/status", {"status": "Available"})
"""

import asyncio
import logging
import time

import httpx
from config import settings

logger = logging.getLogger("db.fabric")


def _rows(data) -> int:
    return len(data) if isinstance(data, (list, dict)) else 1


def _who() -> str:
    """Attribute a call to the agent/task that fired it (blank outside a node)."""
    try:
        from workflows.graph.exec_context import get_exec_ctx
        ctx = get_exec_ctx() or {}
    except Exception:  # noqa: BLE001
        return ""
    sid = (ctx.get("session_id") or "")[:8]
    parts = [p for p in (f"sess={sid}" if sid else "",
                         f"agent={ctx.get('agent_id')}" if ctx.get("agent_id") else "",
                         f"task={ctx.get('task')}" if ctx.get("task") else "") if p]
    return ("  " + " ".join(parts)) if parts else ""


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


# One shared client so connections are pooled and reused across calls. A new
# client per request opened a fresh connection every time and fell over under
# light concurrency. Headers stay per-call -- they carry the tenant's X-Org-Id.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _client


async def aclose_client() -> None:
    """Close the shared client (call on worker shutdown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ── Per-flow read memo ────────────────────────────────────────────────────────
# Agents re-read the same endpoints constantly within one flow, because nothing
# shares fetched data between steps: `ta_results` is a function-local dict, and
# should_run_task() gates on the plan only -- never on "did this task already
# run". Three measured consequences, all on the same 10-admission dataset:
#
#   * discharge.batch_assess_discharges fires 2 GETs x N admissions, and every
#     re-run of the body costs the full 20 calls again (20 / 20 / 20 over three
#     passes). HITL interrupts re-run the body from the top (graph.nodes), and
#     the resume driver loops up to max_passes=8 -> up to 160 calls for 10
#     admissions.
#   * pharmacy/lab subagents each re-fetch their own inputs: /pharmacy/orders/
#     pending and /pharmacy/inventory twice over just two activities.
#   * per-entity paths (/vitals/latest?patient=, /tasks?admission=) have no bulk
#     form upstream and no Redis cache, so each one is a live 120-160ms round
#     trip to a remote host.
#
# So: memoize GETs for the lifetime of one drive pass, keyed by the executing
# session. Scoped per session_id (NOT stored in the exec_context ContextVar --
# set_exec_ctx() is called per node and overwrites it, which would dedup only
# within a single agent and miss exactly the cross-agent repeats above).
#
# Deliberately NOT a TTL cache: a flow wants one consistent snapshot, and
# runner._drive clears the memo at the end of every pass, so a resumed flow
# (which can sit parked for the 30min approval timeout) re-reads live data
# rather than serving a stale clinical snapshot.
_flow_memo: dict[str, dict] = {}          # session_id -> {key: (event, box)}
_MEMO_MAX_ENTRIES = 2000                  # per-session guard against unbounded growth


def _memo_session() -> str:
    """The session whose memo applies, or "" to bypass (outside a flow)."""
    try:
        from workflows.graph.exec_context import get_exec_ctx
        return (get_exec_ctx() or {}).get("session_id") or ""
    except Exception:  # noqa: BLE001
        return ""


def _memo_key(path: str, clean: dict) -> tuple:
    return (path, tuple(sorted((k, str(v)) for k, v in clean.items())))


def clear_flow_memo(session_id: str | None = None) -> None:
    """Drop memoized reads for one session (or all). Called by runner._drive at
    the end of each pass; safe to call for a session that never cached."""
    if session_id is None:
        _flow_memo.clear()
    else:
        _flow_memo.pop(session_id, None)


def _invalidate_on_write(path: str) -> None:
    """A write makes this flow's cached reads stale. Drop the whole session's
    memo rather than guessing which read paths a write affects -- e.g. discharge
    POSTs an AI note and later activities re-read the same admission, and
    /beds/{id}/status changes what every /beds* filter returns. Coarse, but a
    wrong-but-fast snapshot is the failure mode worth avoiding here."""
    sid = _memo_session()
    if sid:
        _flow_memo.pop(sid, None)


async def fget(path: str, **params):
    url = settings.fabric_base_url.rstrip("/") + path
    clean = {k: v for k, v in params.items() if v is not None}
    filt = f"?[{','.join(sorted(clean))}]" if clean else ""  # filter keys only, no PII values

    sid = _memo_session()
    if not sid:                      # outside a flow (poller, advisory scan, API route)
        return await _fget_live(path, url, clean, filt)

    memo = _flow_memo.setdefault(sid, {})
    key = _memo_key(path, clean)
    hit = memo.get(key)
    if hit is not None:
        event, box = hit
        if not event.is_set():
            # Single-flight: an identical GET is already in flight for this flow
            # (the agents fan out with asyncio.gather, so this is the common
            # case, not a rarity). Wait for it instead of duplicating the fetch.
            await event.wait()
        if "data" in box:
            logger.info("GET %s%s -> memo hit%s", path, filt, _who())
            return box["data"]
        raise box["exc"]             # the in-flight fetch failed; surface it to this caller too

    event, box = asyncio.Event(), {}
    if len(memo) < _MEMO_MAX_ENTRIES:
        memo[key] = (event, box)
    try:
        box["data"] = await _fget_live(path, url, clean, filt)
        return box["data"]
    except BaseException as exc:
        # Do not memoize failures -- a retry should get a real attempt. Keep the
        # entry only long enough to hand this error to anyone already waiting.
        box["exc"] = exc
        memo.pop(key, None)
        raise
    finally:
        event.set()


async def _fget_live(path: str, url: str, clean: dict, filt: str):
    t0 = time.perf_counter()
    r = await _get_client().get(url, params=clean, headers=_headers())
    r.raise_for_status()
    data = r.json()
    logger.info("GET %s%s -> %d row(s) %dms%s",
                path, filt, _rows(data), int((time.perf_counter() - t0) * 1000), _who())
    return data


async def fpost(path: str, body: dict | None = None):
    url = settings.fabric_base_url.rstrip("/") + path
    t0 = time.perf_counter()
    r = await _get_client().post(url, json=body, headers=_headers())
    r.raise_for_status()
    _invalidate_on_write(path)
    logger.info("POST %s %dms%s", path, int((time.perf_counter() - t0) * 1000), _who())
    return r.json()


async def fpatch(path: str, body: dict | None = None):
    url = settings.fabric_base_url.rstrip("/") + path
    t0 = time.perf_counter()
    r = await _get_client().patch(url, json=body, headers=_headers())
    r.raise_for_status()
    _invalidate_on_write(path)
    logger.info("PATCH %s %dms%s", path, int((time.perf_counter() - t0) * 1000), _who())
    return r.json()
