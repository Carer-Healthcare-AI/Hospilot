"""§0 agent mapping and the silent-drop guard rail.

Pure logic (no I/O) so it is trivially testable; the /use-cases fetch lives in client.py and
its `resource.bidders` list is passed in here.

The hazard this defends against: the engine filters an ineligible candidate out with NO error
and only raises if EVERY candidate is ineligible (runner.py:152). A mis-mapped agent therefore
produces a completely normal-looking response with one department missing from the ladder —
contention drops, the reserve drops, a different department wins, and nothing says why. The
handover is explicit that this pre-check "is not optional".
"""

from __future__ import annotations

from rl_gateway.config import AGENT_MAP


class IneligibleAgentError(ValueError):
    """A mapped agent is not in the resource's eligible bidders — refuse to POST it."""


def map_agent(department: str) -> str:
    """Our department name -> the engine's AgentKind. Unknown departments are an error, not a
    pass-through, so a typo cannot reach the wire as a novel agent."""
    try:
        return AGENT_MAP[department.lower()]
    except KeyError:
        raise IneligibleAgentError(
            f"no agent mapping for department {department!r}; known: {sorted(AGENT_MAP)}"
        )


def assert_biddable(department: str, resource_bidders: list[str]) -> str:
    """Map `department` and confirm the result is an allowed bidder for the resource.

    `resource_bidders` is `resource.bidders` from GET /use-cases for the profile being
    auctioned. Raises rather than letting the engine silently drop the candidate.
    """
    mapped = map_agent(department)
    if mapped not in resource_bidders:
        raise IneligibleAgentError(
            f"department {department!r} maps to agent {mapped!r}, which is not an eligible "
            f"bidder for this resource ({sorted(resource_bidders)}). Posting it would let the "
            f"engine drop the candidate silently — refusing. If ICU must bid on this bed, "
            f"resolve decision F-12 (§0 option B), do not work around this guard."
        )
    return mapped
