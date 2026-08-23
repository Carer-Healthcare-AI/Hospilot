"""A post-anaesthesia care unit bed (recovery bay).

Named ``pacu_bed`` rather than the plan's ``pacu_slot``, so that every member of the family
reads ``<unit>_bed`` and ``ResourceType.unit`` can strip the suffix to reach the care ladder
without a mapping table.

**The data behind this one is worse than for the other beds.** ``P(no PACU capacity)`` is what
substitutes for physiological deterioration when OT bids — an anaesthetised patient is stable,
so the deterioration slope reads zero while the delay harm is real — and ``ot_room_status`` has
no recovery-area concept at all (BUILD_SPEC F-05). So PACU is both a resource whose scarcity
drives another agent's Waiting score and a resource nothing in the schema can currently count.
"""

from __future__ import annotations

from allocation.contracts import AgentKind, ResourceType
from allocation.profiles.bed import ICU_NOT_A_BIDDER_NOTE, bed_profile
from allocation.profiles.registry import REGISTRY

PACU_BED = REGISTRY.register(
    bed_profile(
        resource_type=ResourceType.PACU_BED,
        description=(
            "One post-anaesthesia recovery bay: ventilation, vasopressors and 1:1 nursing, "
            "but held for hours rather than days."
        ),
        # "recovery" alone is deliberately absent — it appears in clinical prose about a
        # patient recovering, and would resolve sentences that name no unit at all.
        qualifiers=(
            "pacu", "p a c u", "recovery room", "recovery bay", "recovery unit",
            "post anaesthesia", "post anesthesia", "post op recovery", "postoperative recovery",
        ),
        nouns=("bed", "beds", "bay", "bays", "slot", "slots", "space", "spaces"),
        # OT is the overwhelming source; ER reaches PACU only via theatre.
        eligible_agents=(AgentKind.OT, AgentKind.ER),
        # ⚠ UNFITTED — carried from ICU.
        ttl_minutes={
            AgentKind.OT: 20,
            AgentKind.ER: 10,
        },
        # units.yaml holds PACU for 2.6 h declared / 1.25 h narrative. A 4 h horizon reaches
        # well past both. Left at 4 h with the rest of the family so Step 8 can settle horizon
        # and ladder together rather than one bed at a time.
        allocation_horizon_hours=4.0,
        notes=(
            "Caps in caps_pacu_bed.yaml are UNFITTED — copied from the ICU table.",
            ICU_NOT_A_BIDDER_NOTE,
            "P(no PACU capacity) drives OT's Waiting score, and ot_room_status has no "
            "recovery-area concept (F-05). Auctioning a PACU bed does not fix that: the "
            "count feeding another agent's utility still has no source.",
            "units.yaml gives PACU the same four capabilities as ICU, so on the Step 8 "
            "ladder PACU scores as a near-perfect alternative to an ICU bed, separated only "
            "by safe-hold duration. Worth confirming in the workshop rather than inheriting.",
        ),
    )
)
