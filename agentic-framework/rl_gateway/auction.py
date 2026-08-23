"""Assemble one advisory auction and run it — the top-level adapter entry point.

The engine selects the profile from a natural-language `query` (resolved via /use-cases), so a
call needs three things: the `query` string to send, the `resource` key to look eligibility up
under, and the `unit` whose beds to read. The §0 guard and the one-per-agent rule are enforced
HERE, before the POST — the engine drops an ineligible or duplicate agent, and we refuse to let
it happen silently.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from rl_gateway.assemble import build_candidate, build_hospital_state
from rl_gateway.client import AllocationClient
from rl_gateway.forecast import Forecaster
from rl_gateway.mapping import IneligibleAgentError, assert_biddable


async def build_request(
    hasura: Any,
    *,
    query: str,
    unit: str,
    resource: str,
    candidate_specs: Sequence[Mapping[str, Any]],
    client: AllocationClient,
    forecaster: Forecaster | None = None,
) -> dict[str, Any]:
    """Read the world and assemble a POST /auction body, guarding eligibility first."""
    bidders = await client.bidders_for(resource)

    seen: dict[str, str] = {}
    candidates: list[dict[str, Any]] = []
    for spec in candidate_specs:
        # Raises before the POST rather than being silently dropped by the engine.
        mapped = assert_biddable(spec["department"], bidders)
        if mapped in seen:
            raise IneligibleAgentError(
                f"two candidates map to agent {mapped!r} ({seen[mapped]} and "
                f"{spec['candidate_id']}); the engine keeps only one. Nominate a single "
                f"strongest claim per department."
            )
        seen[mapped] = spec["candidate_id"]
        candidates.append(await build_candidate(hasura, spec))

    hospital = await build_hospital_state(hasura, unit, forecaster)
    return {"query": query, "hospital": hospital, "candidates": candidates}


async def advise(
    hasura: Any,
    *,
    query: str,
    unit: str,
    resource: str,
    candidate_specs: Sequence[Mapping[str, Any]],
    client: AllocationClient | None = None,
    forecaster: Forecaster | None = None,
) -> dict[str, Any]:
    """Build + run one advisory auction. Returns the engine's response (persist via persist.py).
    `mode` is forced to advisory inside the client — never live over HTTP."""
    client = client or AllocationClient()
    body = await build_request(
        hasura,
        query=query,
        unit=unit,
        resource=resource,
        candidate_specs=candidate_specs,
        client=client,
        forecaster=forecaster,
    )
    return await client.run_auction(body)
