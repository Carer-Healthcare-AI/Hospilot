"""Persist an engine /auction response into the per-tenant allocation.* schema.

Write-mechanism-agnostic by design, mirroring the engine's own PostgresSink: this module
turns a response into parameterised (sql, params) statements and hands them to an injected
async `execute` callable. Fill that seam with whatever the tenant write path is — a psycopg
cursor (as db/provisioning.py uses), or a Hasura-mutation shim. Keeping it a seam means this
file never imports a driver and never needs to know how a tenant connection is resolved.

Prerequisites: migrations 123/124/126 applied on the tenant (Task 1). If the write path is
Hasura GraphQL rather than raw SQL, the allocation.* tables must also be tracked in Hasura
metadata — reload alone does not track them.

Fidelity note: the response carries lean bid rows (agent, candidate, action, amount, utility,
ceiling, alpha). The richer per-bid audit columns (component_points/coverage, cost,
policy_name) live in the audit bundle at GET /auction/{id}/audit; they are nullable/default
here, so a response-only insert is valid. Enrich from the bundle later for B.13 cap-fitting.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping

# An async statement executor: (sql, params) -> awaitable. Placeholders are psycopg-style %s;
# adapt in the shim if the driver differs.
Executor = Callable[[str, tuple], Awaitable[None]]


_AUCTION_SQL = """
INSERT INTO allocation.auction (
    id, auction_key, resource_type, resource_id, mode, trigger_source,
    predicted_free_at, opened_at, closed_at, max_rounds, rounds_run,
    reserve_price, winning_agent, winning_candidate_id, winning_bid, outcome,
    caps_version, config_version, unsigned_rules, participants
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (id) DO NOTHING
"""

_BID_SQL = """
INSERT INTO allocation.auction_bid (
    auction_id, round_index, agent, candidate_id, action, amount,
    utility, ceiling, alpha
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (auction_id, round_index, agent) DO NOTHING
"""


def _participants(resp: Mapping[str, Any]) -> dict[str, str]:
    """{agent: candidate_id} from positions — every eligible bidder, incl. those that never
    bid (a denial, needed by B.10)."""
    return {
        agent: pos.get("candidate_id")
        for agent, pos in (resp.get("positions") or {}).items()
    }


def auction_row(
    resp: Mapping[str, Any], *, trigger_source: str, predicted_free_at: str | None
) -> tuple:
    resource = resp.get("resource") or {}
    gov = resp.get("governance") or {}
    return (
        resp["auction_id"],
        resp.get("auction_key"),
        resource.get("type"),
        resource.get("id"),
        resp.get("mode"),
        trigger_source,
        predicted_free_at or resp.get("opened_at"),
        resp.get("opened_at"),
        resp.get("closed_at"),
        resp.get("max_rounds"),
        resp.get("rounds_run"),
        resp.get("reserve_price"),
        resp.get("winner"),
        resp.get("winning_candidate_id"),
        resp.get("winning_bid"),
        resp.get("outcome"),
        gov.get("caps_version"),
        gov.get("config_version"),
        json.dumps(gov.get("unsigned_rules") or {}),
        json.dumps(_participants(resp)),
    )


def bid_rows(resp: Mapping[str, Any]) -> list[tuple]:
    rows: list[tuple] = []
    for rnd in resp.get("rounds") or []:
        ridx = rnd.get("round_index")
        for bid in rnd.get("bids") or []:
            rows.append((
                resp["auction_id"], ridx,
                bid.get("agent"), bid.get("candidate_id"), bid.get("action"),
                bid.get("amount"), bid.get("utility"), bid.get("ceiling"), bid.get("alpha"),
            ))
    return rows


async def persist(
    resp: Mapping[str, Any],
    execute: Executor,
    *,
    trigger_source: str = "advisory",
    predicted_free_at: str | None = None,
) -> str:
    """Write the auction + its bid rows. The caller's `execute` should wrap these in one
    transaction (auction before bids — the bids FK it), matching the engine's one-transaction
    audit guarantee. Returns the auction_id."""
    await execute(_AUCTION_SQL, auction_row(
        resp, trigger_source=trigger_source, predicted_free_at=predicted_free_at
    ))
    for row in bid_rows(resp):
        await execute(_BID_SQL, row)
    return resp["auction_id"]
