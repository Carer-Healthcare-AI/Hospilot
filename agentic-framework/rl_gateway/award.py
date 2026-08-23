"""Durable auction award — the advisory handoff from a decided auction to the reservation path.

An advisory auction never holds a bed (schema invariant: "only `live` holds a bed"), so the
winner is recorded as an AWARD: the winning department + its patient + the resource. The award
is (1) written to Redis under a per-(session, resource) key so the reservation path can honor
it, (2) injected into the winning agent's commit state as `state['_bed_award']`, and (3)
broadcast to the live flow view. Nothing here reserves a bed — the physical reservation still
flows through bed_agent + HITL, now SEEDED with the awarded patient (read_award()).
"""

from __future__ import annotations

import logging
from typing import Any

from cache import redis as cache

log = logging.getLogger("rl_gateway.award")

_TTL = 1800  # 30 min — a flow lives well under this


def _key(session_id: str) -> str:
    return f"rl_award:{session_id}"


def build_award(resp: dict[str, Any], *, resource: str, unit: str,
                winner_dept: str, winner_node: str, patient_token: str | None) -> dict[str, Any]:
    """Shape the engine response + local winner context into a persistable award."""
    return {
        "auction_id": resp.get("auction_id"),
        "resource": resource,
        "resource_id": (resp.get("resource") or {}).get("id"),
        "unit": unit,
        "winner": resp.get("winner"),          # engine AgentKind (er | ot | ward)
        "winner_dept": winner_dept,            # our department (er | ot | icu)
        "winner_node": winner_node,            # flow graph node id
        "patient_token": patient_token,
        "winning_candidate_id": resp.get("winning_candidate_id"),
        "winning_bid": resp.get("winning_bid"),
        "outcome": resp.get("outcome"),
    }


async def write_award(session_id: str | None, award: dict[str, Any]) -> None:
    """Persist the award for the reservation path (one entry per resource, keyed by session).
    Fire-and-forget: never raise into the flow."""
    if not session_id:
        return
    try:
        awards: dict[str, Any] = (await cache.get(_key(session_id))) or {}
        awards[award["resource"]] = award
        await cache.set(_key(session_id), awards, ttl=_TTL)
        log.info("bed award: session=%s resource=%s winner=%s patient=%s",
                 session_id, award.get("resource"), award.get("winner_dept"),
                 award.get("patient_token"))
    except Exception as exc:  # noqa: BLE001
        log.warning("write_award failed (session=%s): %s", session_id, exc)


async def read_awards(session_id: str | None) -> dict[str, Any]:
    """All awards in this session as {resource: award}. For the reservation path (bed_agent /
    UI) to SEED which patient a freeing bed is held for — approval stays HITL."""
    if not session_id:
        return {}
    try:
        return (await cache.get(_key(session_id))) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("read_awards failed (session=%s): %s", session_id, exc)
        return {}


async def read_award(session_id: str | None, resource: str) -> dict[str, Any] | None:
    """The award for one resource in this session, or None."""
    return (await read_awards(session_id)).get(resource)
