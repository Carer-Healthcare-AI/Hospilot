"""Prefetch -- port of temporal.workflow.PreFetchWorkflow.

Warms Redis caches for prefetch-eligible TASKS so agent nodes can skip live
fetches. A task is prefetch-eligible iff it has an entry in PREFETCH_TASK_RUNNERS
below -- this dispatch table is the single source of truth for eligibility. Only
pure reads may be registered: session_id in, no persistent writes, no human-facing
notifications. Speculative writes/alerts (e.g. save_triage_scores) must never run
in prefetch. Best-effort: a cache miss just means the agent fetches live. Run as a
fire-and-forget asyncio task alongside the session graph.
"""

import asyncio
import logging
from typing import Awaitable, Callable

from cache import redis as cache
from agents.icu.activities import get_icu_census
from agents.er.activities import get_er_visits
from agents.staff.activities import get_ward_workload
from agents.bed.activities import find_available_beds
from agents.revenue.activities import identify_revenue_leakage

logger = logging.getLogger(__name__)


async def _cache(session_id: str, task_id: str, result: dict) -> None:
    await cache.set_session_result(session_id, f"prefetch:{task_id}", result or {})


# -- Shape-matching wrappers --------------------------------------------------
# Each runner must return EXACTLY the dict the agent body stores at
# ta_results[task_id]; should_run_task conditions read ta_results[task_id][field],
# so a wrong shape silently misfires conditions. Activities that already return
# that exact payload are registered directly below.

async def _er_visits(sid: str) -> dict:
    return {"visits": (await get_er_visits(sid)) or []}


async def _ward_workload(sid: str) -> dict:
    return {"workload": (await get_ward_workload(sid)) or []}


async def _query_beds(sid: str) -> dict:
    return {"candidates": (await find_available_beds(sid)) or []}


# task_id -> async fn(session_id) -> the ta_results[task_id] payload.
# Membership here IS the prefetch-eligibility list.
PREFETCH_TASK_RUNNERS: dict[str, Callable[[str], Awaitable[dict]]] = {
    "ta_get_icu_census":           get_icu_census,
    "ta_identify_revenue_leakage": identify_revenue_leakage,
    "ta_get_er_visits":            _er_visits,
    "ta_get_ward_workload":        _ward_workload,
    "ta_query_beds":               _query_beds,
}


async def run_prefetch(session_id: str, prefetch: list[dict]) -> None:
    """Warm caches for [{agent_id, subagent_id, task_id}, ...]. Best-effort, never raises."""
    if not prefetch:
        return
    task_ids: list[str] = []
    seen: set[str] = set()
    for item in prefetch:
        tid = item.get("task_id")
        if tid and tid not in seen and tid in PREFETCH_TASK_RUNNERS:
            seen.add(tid)
            task_ids.append(tid)
    if not task_ids:
        return

    async def _one(task_id: str):
        try:
            result = await PREFETCH_TASK_RUNNERS[task_id](session_id)
            await _cache(session_id, task_id, result or {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("prefetch task failed  task=%s  err=%s", task_id, exc)

    await asyncio.gather(*[_one(t) for t in task_ids], return_exceptions=True)
    logger.info("prefetch complete  session=%s  tasks=%s", session_id, task_ids)
