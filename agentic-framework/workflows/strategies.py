"""Execution-strategy registry + coordination handlers.

How agents in one execution *level* coordinate (common goal, bidding, competing,
…) is selected by the planner from ``strategies.json`` -- the JSON is the
allowlist/registry and each entry NAMES a handler. The executable coordination
logic lives here, keyed by that handler name, so adding a new strategy is

  1. add an entry to strategies.json (id + handler + description), and
  2. write one ``async def`` handler in STRATEGY_HANDLERS.

No changes to agents, planner, or builder are needed for a new strategy -- they
all talk to the generic AgentUnit interface (see graph.nodes) via the handler.

Handler contract (the B-ready seam)::

    async def handler(units: list[AgentUnit], state: dict) -> dict

``units`` is the set of agent nodes in one level; the handler owns the run loop
and returns the LangGraph state update (``{"results": {...}, ...}``) that the
level contributes. ``common_goal`` just commits every unit and merges -- exactly
today's behavior. ``bidding`` asks each unit to propose (a no-side-effect bid),
commits the highest bidder, and skips the losers.
"""

import asyncio
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

_STRATEGIES_PATH = Path(__file__).with_name("strategies.json")


# -- config loading ------------------------------------------------------------

@lru_cache(maxsize=1)
def load_strategies() -> list[dict]:
    """Return the strategy entries from strategies.json (cached)."""
    data = json.loads(_STRATEGIES_PATH.read_text(encoding="utf-8"))
    strategies = data.get("strategies", [])
    if not strategies:
        raise RuntimeError("strategies.json contains no strategies")
    return strategies


def _by_id() -> dict[str, dict]:
    return {s["id"]: s for s in load_strategies()}


def is_valid_strategy(strategy_id: str | None) -> bool:
    return bool(strategy_id) and strategy_id in _by_id()


def default_strategy_id() -> str:
    """The strategy marked ``default: true`` (or the first entry)."""
    for s in load_strategies():
        if s.get("default"):
            return s["id"]
    return load_strategies()[0]["id"]


def strategy_catalogue_text() -> str:
    """Formatted catalogue for injection into the planner prompt."""
    lines = []
    for s in load_strategies():
        wtu = f"  (use when: {s['when_to_use']})" if s.get("when_to_use") else ""
        lines.append(f"  {s['id']}: {s['name']} -- {s['description']}{wtu}")
    return "\n".join(lines)


# -- handler registry ----------------------------------------------------------
# Populated at the bottom of the module once the handlers are defined.
STRATEGY_HANDLERS: dict[str, Callable[..., Awaitable[dict]]] = {}


def get_handler(strategy_id: str | None) -> Callable[..., Awaitable[dict]]:
    """Resolve a pipeline's chosen strategy id to its coordination handler.

    Falls back to the default strategy when the id is missing/unknown. Fails
    LOUD if the JSON names a handler that has no implementation (config drift).
    """
    sid = strategy_id if is_valid_strategy(strategy_id) else default_strategy_id()
    handler_name = _by_id()[sid]["handler"]
    handler = STRATEGY_HANDLERS.get(handler_name)
    if handler is None:
        raise RuntimeError(
            f"strategy '{sid}' names handler '{handler_name}' which is not "
            f"registered in STRATEGY_HANDLERS (have: {sorted(STRATEGY_HANDLERS)})"
        )
    return handler


# -- handlers ------------------------------------------------------------------
# A handler receives the level's AgentUnits (graph.nodes.AgentUnit) + the current
# state and returns the merged LangGraph state update. Units expose:
#   await unit.commit(state)  -> dict   (run for real -- today's node body)
#   await unit.propose(state) -> dict   ({"score": float, ...} -- no side effects)
#   await unit.skip(state, reason) -> dict   (emit branch_skipped, return _skipped)
#   unit.node_id / unit.order


def _merge_updates(updates: list[dict]) -> dict:
    """Shallow-merge the per-unit state updates into one level update.

    Mirrors graph.state.merge_dict (last-writer-wins per top-level key), but
    deep-merges the dict-valued channels (results/_skipped/_bids) so concurrent
    units in a level accumulate rather than clobber.
    """
    merged: dict = {}
    for upd in updates:
        for key, value in (upd or {}).items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    return merged


async def common_goal(units, state: dict) -> dict:
    """Default: run every unit concurrently and merge results -- today's behavior."""
    updates = await asyncio.gather(*(u.commit(state) for u in units))
    return _merge_updates(list(updates))


async def rl_bidding(units, state: dict) -> dict:
    """Like `bidding`, but the RL bed-allocation engine arbitrates instead of max-score.
    The contending units are the bidders; the engine's winner commits, the rest are skipped.
    Falls back to the heuristic `bidding` when the engine is unavailable or can't decide, so
    it degrades safely and never starves the level.
    """
    if len(units) <= 1:
        return await common_goal(units, state)

    decision = None
    try:
        from rl_gateway.strategy import rl_decide_winner
        decision = await rl_decide_winner(units, state)
    except Exception:
        logger.exception("rl_bidding: RL arbitration failed -- degenerating to common_goal")

    winner = next((u for u in units if u.node_id == decision["node"]), None) if decision else None
    if winner is None:
        return await common_goal(units, state)

    # Surface the bid ladder + the award to the live flow view over the session WebSocket
    # (best-effort). The award (advisory) names the winning patient for the reservation path.
    award = decision.get("award")
    try:
        from api.routes.ws import broadcast
        sid = state.get("session_id")
        if sid:
            await broadcast(sid, {
                "type": "bed_auction",
                "winner_node": winner.node_id,
                "losers": [u.node_id for u in units if u is not winner],
                **decision["auction"],
            })
            if award:
                await broadcast(sid, {"type": "bed_auction_award", **award})
    except Exception:
        logger.debug("bed_auction broadcast skipped", exc_info=True)

    logger.info("RL-BID  winner=%s  losers=%s", winner.node_id,
                [u.node_id for u in units if u is not winner])
    # Hand the award to the winner's commit only: its body can reserve the awarded patient
    # through the normal (HITL-gated) reservation path. Losers never see it.
    winner_state = {**state, "_bed_award": award} if award else state
    results = await asyncio.gather(
        winner.commit(winner_state),
        *(u.skip(state, "lost_bid") for u in units if u is not winner),
    )
    return _merge_updates(list(results))


STRATEGY_HANDLERS["common_goal"] = common_goal
STRATEGY_HANDLERS["bidding"] = rl_bidding
STRATEGY_HANDLERS["rl_bidding"] = rl_bidding
# 'competing' is listed in strategies.json but points its handler at common_goal
# for now, so selecting it degrades gracefully (run-all-and-merge) until real
# candidate-scoring semantics land. When they do: register a `competing` handler
# here and flip strategies.json's "competing".handler back to "competing".
# get_handler() still fails LOUD for genuine config drift -- a JSON handler name
# that matches nothing registered here.
