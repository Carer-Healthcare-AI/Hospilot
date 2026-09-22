"""Persist a ``/diagnostic/auction`` response into the per-tenant ``allocation.*`` schema.

Write-mechanism-agnostic, mirroring ``rl_gateway/persist.py``: this module turns a response
into parameterised ``(sql, params)`` statements and hands them to an injected async
``execute``. Fill that seam with the tenant write path — ``rl_gateway.db.tenant_transaction``
already provides one, and is reused rather than reimplemented.

Prerequisite: ``095_diagnostic_allocation_tables.sql`` applied on the tenant.

**One row per agent per round, losers included.** Same rule as the bed tables and for the same
reason: store only the winner and fairness, denial-cohort and cap-fitting questions become
permanently unanswerable, and none of it can be backfilled.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Mapping

Executor = Callable[[str, tuple], Awaitable[None]]


_AUCTION_SQL = """
INSERT INTO allocation.diagnostic_auction (
    id, auction_key, modality, machine_id, mode, trigger_source,
    opened_at, closed_at, max_rounds, rounds_run, reserve_price, contention,
    winning_agent, winning_request_id, winning_bid, outcome,
    caps_version, config_version, unsigned_rules, participants
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (id) DO NOTHING
"""

_BID_SQL = """
INSERT INTO allocation.diagnostic_bid (
    auction_id, round_index, agent, request_id, action, pathway,
    amount, utility, ceiling, alpha, remaining_budget, clamped_by
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (auction_id, round_index, agent) DO NOTHING
"""

_AWARD_SQL = """
INSERT INTO allocation.diagnostic_award (
    auction_id, machine_id, request_id, starts_at, ends_at
) VALUES (%s,%s,%s,%s,%s)
ON CONFLICT (auction_id) DO NOTHING
"""


def _participants(resp: Mapping[str, Any]) -> dict[str, str | None]:
    """{agent: request_id} for every agent that appeared in the opening round.

    Taken from round 0 rather than from the winner, because an agent that bid and lost is the
    denial cohort — the thing the bed schema's comment calls out as impossible to backfill.
    """
    rounds = resp.get("rounds") or []
    if not rounds:
        return {}
    return {bid["agent"]: bid.get("request_id") for bid in rounds[0].get("bids", [])}


def auction_row(resp: Mapping[str, Any], *, trigger_source: str) -> tuple:
    rounds = resp.get("rounds") or []
    return (
        resp.get("auction_id"),
        resp.get("auction_key"),
        resp.get("modality"),
        resp.get("machine_id"),
        resp.get("mode"),
        trigger_source,
        resp.get("opened_at"),
        resp.get("closed_at"),
        resp.get("max_rounds"),
        len(rounds),
        resp.get("reserve_price"),
        resp.get("contention"),
        resp.get("winner"),
        resp.get("winning_request_id"),
        resp.get("winning_bid"),
        resp.get("outcome"),
        resp.get("caps_version"),
        resp.get("config_version"),
        json.dumps(resp.get("unsigned_rules") or {}),
        json.dumps(_participants(resp)),
    )


def bid_rows(resp: Mapping[str, Any]) -> list[tuple]:
    auction_id = resp.get("auction_id")
    rows = []
    for rnd in resp.get("rounds") or []:
        for bid in rnd.get("bids") or []:
            rows.append(
                (
                    auction_id,
                    rnd.get("round_index"),
                    bid.get("agent"),
                    bid.get("request_id"),
                    bid.get("action"),
                    bid.get("pathway"),
                    bid.get("amount"),
                    bid.get("utility"),
                    bid.get("ceiling"),
                    bid.get("alpha"),
                    bid.get("remaining_budget"),
                    bid.get("clamped_by"),
                )
            )
    return rows


def award_row(resp: Mapping[str, Any]) -> tuple | None:
    interval = resp.get("awarded_interval")
    if not interval:
        return None
    return (
        resp.get("auction_id"),
        interval.get("machine_id"),
        interval.get("request_id"),
        interval.get("starts_at"),
        interval.get("ends_at"),
    )


async def persist_auction(
    execute: Executor, resp: Mapping[str, Any], *, trigger_source: str = "imaging_order"
) -> None:
    """Write one response. Caller owns the transaction, so a partial auction never lands."""
    await execute(_AUCTION_SQL, auction_row(resp, trigger_source=trigger_source))
    for row in bid_rows(resp):
        await execute(_BID_SQL, row)
    award = award_row(resp)
    if award is not None:
        await execute(_AWARD_SQL, award)
