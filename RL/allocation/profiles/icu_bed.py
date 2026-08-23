"""The founding use case, and now one bed among six.

    "ER, OT, and ICU/Ward demand compete for one limited ICU bed. RL learns who should
     receive the bed while balancing survival, throughput, waiting time, cancellations,
     and financial impact."

That sentence is the *use case*, and it selects this profile. It is the only bed in the family
whose caps, TTLs and horizon were chosen for it rather than inherited from it — which is why
it is the only one not carrying an UNFITTED warning. It is not the runtime trigger:
an auction opens on a bed-release event (``predict_discharge_probability`` or the
``change_queue`` CDC feed), because the auction is 3 rounds x ~2 minutes against a bed 30
minutes out, and utilities carry 10-20 minute TTLs. A hand-typed query fires the same
``open_auction`` path with ``AuctionMode.SIMULATION`` — one implementation, never two.
"""

from __future__ import annotations

from allocation.contracts import AgentKind, ResourceType
from allocation.profiles.bed import ICU_NOT_A_BIDDER_NOTE, bed_profile
from allocation.profiles.registry import REGISTRY

ICU_BED = REGISTRY.register(
    bed_profile(
        resource_type=ResourceType.ICU_BED,
        description="One general adult ICU bed, contested by ER, OT and Ward.",
        # A qualifier naming the unit plus a noun naming the thing, in any order. "itu" is
        # UK usage for the same unit. HDU and PACU used to be excluded here as the
        # *alternative* pathways; they are now beds in their own right with their own
        # profiles, so they are excluded for the ordinary reason instead — they name a
        # different resource, and qualifiers must not overlap across the family.
        qualifiers=(
            "icu", "i c u", "itu", "intensive care", "intensive care unit",
            "critical care", "critical care unit",
        ),
        # AgentKind.ICU is deliberately absent: RL-Steps section 3 gives ICU internal demand
        # a TTL but never models it as a bidder, and whether it bids and holds a budget is
        # AGENT_BUDGET open decision 3 (BUILD_SPEC F-12). It affects the bidder count, hence
        # contention, hence every burn rate.
        eligible_agents=(AgentKind.ER, AgentKind.OT, AgentKind.WARD),
        ttl_minutes={
            AgentKind.ER: 10,
            AgentKind.OT: 20,
            AgentKind.WARD: 15,
        },
        allocation_horizon_hours=4.0,
        notes=(
            "Caps in caps_icu_bed.yaml are ICU-specific and unfitted (B.13/BA8). Every other "
            "bed type supplies its own; reusing these would inherit a calibration never "
            "valid for it.",
            ICU_NOT_A_BIDDER_NOTE,
            "Waiting/Delay is not interchangeable across agents: for OT the patient is "
            "stable, so the physiological P(deterioration) reads zero while the delay harm "
            "is real. P(no PACU capacity) substitutes — and PACU has no representation in "
            "ot_room_status (BUILD_SPEC F-05).",
            "Operational Impact has a different formula per agent (D.5). There is no shared "
            "definition to fall back on.",
        ),
    )
)
