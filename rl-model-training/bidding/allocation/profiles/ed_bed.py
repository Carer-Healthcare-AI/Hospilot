"""An emergency department cubicle.

The bed with the sharpest naming hazard in the family. ``ed`` names a *unit*; ``er`` names an
:class:`~allocation.contracts.AgentKind`. A query like *"should ER or OT get the ICU bed?"*
mentions ER as a bidder, not as a destination — so ``"er"`` is deliberately **not** a
qualifier here. Including it would make that sentence name two resources, and the matcher
raises on that rather than picking one, turning a perfectly clear query into an error.

``"emergency"`` alone is excluded for the same reason: it qualifies the situation as often as
the unit.

ED cubicles are also what ER's *boarding count* measures. Auctioning one allocates the
resource whose scarcity already feeds Resource Stress and ER's Operational score, so an ED-bed
auction reads its own pressure as an input. That is coherent — occupancy genuinely should
raise the value of the next cubicle — but it is worth knowing before reading the numbers.
"""

from __future__ import annotations

from allocation.contracts import AgentKind, ResourceType
from allocation.profiles.bed import ICU_NOT_A_BIDDER_NOTE, bed_profile
from allocation.profiles.registry import REGISTRY

ED_BED = REGISTRY.register(
    bed_profile(
        resource_type=ResourceType.ED_BED,
        description=(
            "One emergency department cubicle: continuous monitoring only, held for minutes "
            "to an hour. What the ER boarding count counts."
        ),
        # No "er" and no bare "emergency" — see the module docstring.
        qualifiers=(
            "ed", "e d", "a and e", "a e", "casualty",
            "emergency department", "emergency room", "emergency dept",
        ),
        nouns=("bed", "beds", "cubicle", "cubicles", "space", "spaces", "trolley", "trolleys"),
        eligible_agents=(AgentKind.ER,),
        # ⚠ UNFITTED — carried from ICU.
        ttl_minutes={AgentKind.ER: 10},
        # units.yaml holds ED for 0.5 h against a 4 h horizon. See resus_bed for why the
        # horizon is left aligned with the family rather than corrected here.
        allocation_horizon_hours=4.0,
        notes=(
            "Caps in caps_ed_bed.yaml are UNFITTED — copied from the ICU table.",
            ICU_NOT_A_BIDDER_NOTE,
            "Single eligible bidder — see resus_bed for what that costs the result.",
            "boarding_count and lwbs_risk describe ED pressure and already feed Resource "
            "Stress and ER's Operational score. In an ED-bed auction the resource being "
            "allocated is the one those figures measure.",
        ),
    )
)
