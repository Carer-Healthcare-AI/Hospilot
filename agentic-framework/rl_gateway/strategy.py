"""RL arbitration for the flow's `bidding` execution strategy.

The app already lets agents in one execution level compete: each proposes a bid, the highest
scorer commits, the losers are skipped (workflows/strategies.py `bidding`). This makes the RL
engine the arbiter instead of a plain max-score: the contending units ARE the bidders, we run
one auction over them, and the RL winner is the unit that commits.

Maps each department agent to the engine's AgentKind (icu→ward per §0), nominates that
department's strongest waiting patient as its candidate (nominate_candidates — REAL patients,
not placeholders, so winning_candidate_id is a real token the reward loop can observe), runs
the auction, persists it, and returns the winning node_id. Returns None on any problem so the
caller falls back to the heuristic bidding.
"""

from __future__ import annotations

import logging
from typing import Any

from db.hasura import hasura

from rl_gateway.auction import advise
from rl_gateway.award import build_award, write_award
from rl_gateway.client import AllocationClient
from rl_gateway.db import slug_for_org, tenant_transaction
from rl_gateway.persist import persist
from rl_gateway.queries import AGENT_MAP, AGENT_NODE_TO_DEPT, participants_for, unit_for_resource
from rl_gateway.reward import enqueue_observation
from rl_gateway.select import select_bed_resource
from rl_gateway.trigger import nominate_candidates

log = logging.getLogger("rl_gateway.strategy")


def _bidding_context(units) -> str:
    """Short summary of who's in this step, so the selector can judge intent."""
    return "Agents running in this step: " + ", ".join(u.node_id for u in units)


async def rl_decide_winner(units, state: dict, resource: str | None = None, org_id: str | None = None) -> dict | None:
    """Run one auction over the contending units; return {"node": winning_node_id, "auction":
    <ladder summary>}, or None if it can't decide."""
    # WHICH bed (if any) is this flow contesting? If the caller already knows the resource
    # (e.g. a direct/CDC trigger), use it. Otherwise the flow LLM picks a resource from the
    # cases.json catalog — or decides no bed is being requested at all (inspection / monitoring
    # / forecasting), in which case there is no auction.
    if resource is None:
        resource = await select_bed_resource(state.get("goal", ""), _bidding_context(units))
        if resource is None:
            log.info("flow bidding: LLM found no bed request in this flow — no auction")
            return None
    unit = unit_for_resource(resource)
    query = f"a {unit} bed is opening"

    # Real candidates: one strongest waiting patient per department for this unit. The bidding
    # level decides the winner BEFORE its agents run, so we nominate from live clinical data
    # (critical vitals -> department bucket) rather than the agents' not-yet-produced results.
    # A department with no nominated patient simply cannot bid (no synthetic tokens).
    noms = {n["department"]: n for n in await nominate_candidates(hasura, unit)}

    # Only the departments the config lists as participants for this resource may bid.
    allowed = set(participants_for(resource))
    dept_by_node: dict[str, str] = {}
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for u in units:
        dept = AGENT_NODE_TO_DEPT.get(u.node_id)
        if not dept or dept not in allowed:
            continue
        mapped = AGENT_MAP.get(dept, dept)
        if mapped in seen:  # one bidder per engine agent (icu maps to the engine's ward slot)
            continue
        nom = noms.get(dept)
        if not nom:
            continue  # no real patient nominated for this department -> it cannot bid
        seen.add(mapped)
        dept_by_node[u.node_id] = dept
        specs.append(nom)

    if len(specs) < 2:
        return None  # not a contest (fewer than two departments with a real candidate)

    try:
        resp = await advise(hasura, query=query, unit=unit,
                            resource=resource, candidate_specs=specs, client=AllocationClient())
        async with tenant_transaction(await slug_for_org(org_id)) as execute:
            await persist(resp, execute, trigger_source="flow-bidding")
            await enqueue_observation(execute, resp)
    except Exception as exc:  # noqa: BLE001
        log.warning("rl_decide_winner failed, falling back: %s", exc)
        return None

    token_by_dept = {s["department"]: s.get("patient_token") for s in specs}
    winner_agent = resp.get("winner")  # er | ot | ward
    for node, dept in dept_by_node.items():
        if AGENT_MAP.get(dept, dept) == winner_agent:
            log.info("RL bidding: winner=%s dept=%s auction=%s bid=%s",
                     node, dept, resp.get("auction_id"), resp.get("winning_bid"))
            # Advisory handoff: record the award (winner dept + patient + resource) so the
            # reservation path can honor it. Only a real award is handed off; a no_award /
            # aborted auction still gates execution but hands off no patient.
            award = None
            if resp.get("outcome") == "awarded":
                award = build_award(
                    resp, resource=resource, unit=unit, winner_dept=dept,
                    winner_node=node, patient_token=token_by_dept.get(dept),
                )
                await write_award(state.get("session_id"), award)
            return {"node": node, "award": award, "auction": {
                "auction_id": resp.get("auction_id"),
                "resource": resource,
                "winner": winner_agent,
                "winning_bid": resp.get("winning_bid"),
                "reserve_price": resp.get("reserve_price"),
                "outcome": resp.get("outcome"),
                "utilities": resp.get("utilities"),
            }}
    return None
