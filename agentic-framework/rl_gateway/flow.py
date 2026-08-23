"""Flow-participant bidding: the agents running in a flow are the bidders.

Model: when a flow runs several department agents (ICU / ER / OT …) that contend for the same
resource (a freeing bed), each participant drops a bid into a per-session pool while it runs.
After the parallel participants finish, the synthesis node calls resolve_flow_auctions(), which
turns the pooled bids into ONE auction per contested resource and lets the engine pick the
winner. So the flow's agents literally bid against each other for the bed.

The pool lives in Redis under one key per session; each bid names the department and the
patient it is bidding for. advise() re-reads that patient's clinical data and runs the §0
guard, so a bid only needs to say WHO is bidding, not carry the whole clinical picture.
"""

from __future__ import annotations

import logging
from typing import Any

from cache import redis as cache
from db.hasura import hasura

from rl_gateway.auction import advise
from rl_gateway.client import AllocationClient
from rl_gateway.db import slug_for_org, tenant_transaction
from rl_gateway.persist import persist
from rl_gateway.reward import enqueue_observation

log = logging.getLogger("rl_gateway.flow")

_POOL_TTL = 1800  # 30 min — a flow lives well under this


def _key(session_id: str) -> str:
    return f"rl_bids:{session_id}"


async def submit_bid(
    session_id: str,
    resource: str,
    department: str,
    patient_token: str,
    *,
    candidate_id: str | None = None,
    arrived_at: str | None = None,
) -> None:
    """A flow participant bids for `resource` (e.g. 'icu_bed') on behalf of `patient_token`.
    One bid per department per session (last write wins, matching the engine's one-per-agent).
    Fire-and-forget safe: never raise into the agent body."""
    try:
        pools: dict[str, list] = (await cache.get(_key(session_id))) or {}
        bid = {
            "department": department,
            "patient_token": patient_token,
            "candidate_id": candidate_id or patient_token,
            "arrived_at": arrived_at or "-1h",
        }
        pool = [b for b in pools.get(resource, []) if b["department"] != department]
        pool.append(bid)
        pools[resource] = pool
        await cache.set(_key(session_id), pools, ttl=_POOL_TTL)
        log.info("flow bid: session=%s resource=%s dept=%s patient=%s",
                 session_id, resource, department, patient_token)
    except Exception as exc:  # noqa: BLE001
        log.warning("submit_bid failed (session=%s): %s", session_id, exc)


async def resolve_flow_auctions(session_id: str, org_id: str | None = None) -> list[dict]:
    """Run one auction per contested resource from the session's pooled bids. Called at
    synthesis, after the parallel participants have all bid. Returns the engine responses."""
    try:
        pools = await cache.get(_key(session_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("resolve: could not read bid pool (session=%s): %s", session_id, exc)
        return []
    if not pools:
        return []

    slug = await slug_for_org(org_id)
    client = AllocationClient()
    responses: list[dict] = []
    for resource, bids in pools.items():
        if len(bids) < 2:
            # No contention — one bidder isn't an auction. Skip (nothing to arbitrate).
            log.info("flow resolve: %s has %d bidder(s), no auction", resource, len(bids))
            continue
        unit = resource[:-4] if resource.endswith("_bed") else resource
        try:
            resp = await advise(
                hasura,
                query=f"a {unit} bed is opening",
                unit=unit,
                resource=resource,
                candidate_specs=bids,
                client=client,
            )
            async with tenant_transaction(slug) as execute:
                await persist(resp, execute, trigger_source=f"flow:{session_id}")
                await enqueue_observation(execute, resp)
            responses.append(resp)
            log.info("flow auction %s: winner=%s (resource=%s, %d bidders)",
                     resp.get("auction_id"), resp.get("winner"), resource, len(bids))
        except Exception as exc:  # noqa: BLE001
            log.warning("flow auction failed for %s (session=%s): %s", resource, session_id, exc)

    try:
        await cache.delete(_key(session_id))
    except Exception:  # noqa: BLE001
        pass
    return responses
