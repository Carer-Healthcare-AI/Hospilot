"""The second bed — written first, deliberately, as the seam check.

``registry.py`` claims that adding a resource type touches only ``profiles/<resource>.py``
plus config rows, and invites the check: *"worth writing a throwaway second profile during
design purely to check."* This profile is that check, and it **found two leaks**:

* ``HospitalState`` was ICU-shaped — an HDU auction had to report its beds in a field named
  ``icu_total_beds``. Fixed by unit-scoping the dataclass and its parse boundary.
* ``load_config`` hardcoded ``caps.yaml`` while ``ResourceProfile.caps_config`` had zero
  readers, so every resource silently shared ICU's unfitted maxima. Fixed by wiring the field.

Neither lived in ``profiles/``. The docstring's claim is now true; it was not before.

HDU is no longer *only* the alternative pathway. It remains the fallback that half the
withdrawals in RL-Steps turn on — and Alternative Availability still scores it as one — but it
is now also a bed that can itself be auctioned, which is the point of the family.
"""

from __future__ import annotations

from allocation.contracts import AgentKind, ResourceType
from allocation.profiles.bed import ICU_BIDDER_NOTE, bed_profile
from allocation.profiles.registry import REGISTRY

HDU_BED = REGISTRY.register(
    bed_profile(
        resource_type=ResourceType.HDU_BED,
        description=(
            "One high-dependency / step-down bed: continuous monitoring and vasopressors, "
            "but neither sustained ventilation nor 1:1 nursing."
        ),
        # No overlap with any other bed's qualifiers — the matcher raises on a query naming
        # two resources. "step down"/"stepdown" are how the unit is asked for in practice.
        qualifiers=(
            "hdu", "h d u", "high dependency", "high dependency unit",
            "step down", "stepdown", "step down unit",
        ),
        # ER and Ward escalate into HDU; OT steps down into it post-operatively; ICU steps
        # down into it too, which is the other half of what Step 10 unblocked.
        eligible_agents=(AgentKind.ER, AgentKind.OT, AgentKind.WARD, AgentKind.ICU),
        # ⚠ UNFITTED. Carried across from the ICU profile because no HDU-specific TTL study
        # exists. A TTL is how long a utility stays valid, so these are wrong in the same way
        # the caps are wrong — visibly, and pending the same workshop.
        ttl_minutes={
            AgentKind.ER: 10,
            AgentKind.OT: 20,
            AgentKind.WARD: 15,
            AgentKind.ICU: 15,
        },
        # units.yaml gives HDU a 2.8 h safe hold, so a 4 h horizon reaches past what the unit
        # can actually cover. Kept at 4 h for now: the horizon is also the denominator of
        # Alternative's duration term, and changing it here would move that scoring for HDU
        # alone, ahead of the Step 8 ladder work that should settle it deliberately.
        allocation_horizon_hours=4.0,
        notes=(
            "Caps in caps_hdu_bed.yaml are UNFITTED — copied from the ICU table, which was "
            "itself chosen rather than fitted (B.13/BA8). Config.unsigned reports this on "
            "every run. They must be fitted before an HDU auction means anything.",
            ICU_BIDDER_NOTE,
            "HDU is simultaneously an auctionable resource and a rung on the care ladder "
            "that Alternative Availability scores against. Step 8 must exclude the unit "
            "being auctioned from its own alternatives, or an HDU auction will score HDU as "
            "a fallback to itself.",
        ),
    )
)
