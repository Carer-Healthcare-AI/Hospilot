"""Shared configuration for the bed-family allocation profiles.

Every bed resource follows the same family structure: the same auction rounds, same utility
components, and the same care-ladder mapping. What differs between bed types is which agents
are eligible, the resource-specific caps and budgets, and the TTLs for each profile.
"""

from __future__ import annotations

from typing import Mapping

from allocation.contracts import AgentKind, ComponentName, ResourceType
from allocation.profiles.registry import ResourceProfile, UseCaseMatcher

#: All eight apply to every bed.
BED_COMPONENTS: tuple[ComponentName, ...] = (
    ComponentName.CLINICAL_BENEFIT,
    ComponentName.URGENCY,
    ComponentName.WAITING,
    ComponentName.THROUGHPUT,
    ComponentName.OPERATIONAL,
    ComponentName.FINANCIAL,
    ComponentName.ALTERNATIVE,
    ComponentName.RESOURCE_STRESS,
)

#: 3 x 120 s against a bed ~30 minutes out, with 10-20 minute utility TTLs. RL-Steps 1.
BED_MAX_ROUNDS = 3
BED_ROUND_SECONDS = 120
BED_AUCTION_KEY_BUCKET_MINUTES = 15


def caps_filename(resource_type: ResourceType) -> str:
    """``icu_bed`` -> ``caps_icu_bed.yaml``. One caps file per resource, never shared."""
    return f"caps_{resource_type.value}.yaml"


def budget_filename(resource_type: ResourceType) -> str:
    """``icu_bed`` -> ``budget_icu_bed.yaml``. One pool per resource (D-3).

    Budgets are denominated in utility points, so a pool is only meaningful against the caps
    table those points were scored under. Sharing one pool across bed types lets ICU auctions,
    at ~107 points each, drain what ward-bed auctions bid ~50 into.
    """
    return f"budget_{resource_type.value}.yaml"


def bed_profile(
    resource_type: ResourceType,
    description: str,
    qualifiers: tuple[str, ...],
    eligible_agents: tuple[AgentKind, ...],
    ttl_minutes: Mapping[AgentKind, int],
    allocation_horizon_hours: float,
    nouns: tuple[str, ...] = ("bed", "beds", "cot", "cots"),
    notes: tuple[str, ...] = (),
) -> ResourceProfile:
    """A profile for one bed type, with the family's shared structure filled in.

    ``qualifiers`` must not overlap another bed's: the token matcher raises on a query that
    names two resources, so an overlap turns a clear sentence into an ambiguity error.
    """
    return ResourceProfile(
        resource_type=resource_type,
        description=description,
        matcher=UseCaseMatcher(qualifiers=qualifiers, nouns=nouns),
        components=BED_COMPONENTS,
        eligible_agents=eligible_agents,
        ttl_minutes=ttl_minutes,
        max_rounds=BED_MAX_ROUNDS,
        round_seconds=BED_ROUND_SECONDS,
        allocation_horizon_hours=allocation_horizon_hours,
        auction_key_bucket_minutes=BED_AUCTION_KEY_BUCKET_MINUTES,
        caps_config=caps_filename(resource_type),
        budget_config=budget_filename(resource_type),
        notes=notes,
    )


#: Carried by every profile ICU can bid on. Its operational formula was defined in Step 10 —
#: the nursing-saturation rule, the same shape as Ward's — which is what unblocked eligibility.
#: The formula is a decision, not a measurement, and it measures nursing load rather than the
#: bed pressure ICU is really bidding on; ``Operational._icu`` records both caveats.
ICU_BIDDER_NOTE = (
    "AgentKind.ICU bids here: it wants a step-down bed to free its own capacity. Its "
    "Operational formula reuses Ward's nursing-saturation rule with the ward's unfitted "
    "saturation constant, and measures nursing load rather than ICU bed pressure — reading "
    "ICU's own occupancy needs the Step 12 adapter. The budget pool also has no `targets` row "
    "for icu, which is harmless under base.mode `common` (the shipped mode) but would give "
    "ICU a base of 0 under `derived`."
)

#: Carried by profiles ICU does **not** bid on, for the reason it does not.
ICU_NOT_A_BIDDER_NOTE = (
    "AgentKind.ICU is not eligible here. Patients do not step down from ICU into this unit, "
    "so ICU has no claim on the bed. Whether ICU bids for an *ICU* bed as internal demand is "
    "a different and still-open question — BUILD_SPEC F-12 / AGENT_BUDGET open decision 3."
)
