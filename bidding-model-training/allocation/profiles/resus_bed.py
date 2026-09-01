"""A resuscitation bay.

Auctionable because the family covers every bed the hospital might free, but it is the least
like the others, and the difference is worth stating rather than discovering:

**A resus bay is usually where a patient already is, not where they are going.** ER's bid for
an ICU bed is a bid to *empty* a resus bay. Auctioning the bay itself is the inverse
transaction, and only ER can plausibly bid for it — which makes a single-bidder auction the
normal case, not a degenerate one. A single-bidder auction still runs, still charges budget
and still clears against the reserve price; what it cannot do is discriminate, so its result
carries much less information than a contested one.
"""

from __future__ import annotations

from allocation.contracts import AgentKind, ResourceType
from allocation.profiles.bed import ICU_NOT_A_BIDDER_NOTE, bed_profile
from allocation.profiles.registry import REGISTRY

RESUS_BED = REGISTRY.register(
    bed_profile(
        resource_type=ResourceType.RESUS_BED,
        description=(
            "One resuscitation bay: vasopressors and continuous monitoring, but neither "
            "sustained ventilation nor 1:1 ICU nursing. Held for well under an hour."
        ),
        qualifiers=("resus", "resuscitation", "resuscitation room", "trauma bay", "crash bay"),
        nouns=("bed", "beds", "bay", "bays", "space", "spaces", "slot", "slots"),
        # ER only. See the module docstring: a resus bay is ER's own real estate.
        eligible_agents=(AgentKind.ER,),
        # ⚠ UNFITTED — carried from ICU.
        ttl_minutes={AgentKind.ER: 10},
        # units.yaml holds resus for 1.2 h declared / 0.75 h narrative — the shortest in the
        # ladder, and far short of a 4 h horizon. Of every bed in the family this is the one
        # whose horizon most clearly wants revisiting; left aligned for now so Step 8 changes
        # horizon once, deliberately, rather than six times by inheritance.
        allocation_horizon_hours=4.0,
        notes=(
            "Caps in caps_resus_bed.yaml are UNFITTED — copied from the ICU table.",
            ICU_NOT_A_BIDDER_NOTE,
            "Single eligible bidder. The auction runs and clears, but with one bid there is "
            "nothing to discriminate between, so contention, burn rate and the reserve-price "
            "margin all read differently than in a contested auction. Do not compare its "
            "utilities against a three-bidder ICU auction without accounting for that.",
        ),
    )
)
