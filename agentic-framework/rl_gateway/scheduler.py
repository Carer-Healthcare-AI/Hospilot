"""Background reward loop — polls due pending_observation rows and scores them.

Launched from main.py's lifespan like the other background loops (advisory engine, dispatch),
gated on ALLOCATION_ENABLED so it is inert unless the gateway is turned on.
"""

from __future__ import annotations

import asyncio
import logging

from db.hasura import hasura

from rl_gateway.db import tenant_transaction
from rl_gateway.reward import run_due_observations

log = logging.getLogger("rl_gateway.scheduler")

_INTERVAL_SECONDS = 300  # 5 min; the reward window is 4 h, so this is ample


async def reward_tick(org_slug: str = "default") -> int:
    async with tenant_transaction(org_slug) as execute:
        return await run_due_observations(execute, hasura)


async def start_reward_loop(org_slug: str = "default", interval: int = _INTERVAL_SECONDS) -> None:
    log.info("allocation reward loop started (org=%s, every %ss)", org_slug, interval)
    while True:
        try:
            await reward_tick(org_slug)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("reward tick failed: %s", exc)
        await asyncio.sleep(interval)
