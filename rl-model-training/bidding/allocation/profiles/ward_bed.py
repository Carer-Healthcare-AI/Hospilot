"""A general inpatient ward bed.

The bed type that inverts the model, and the reason the family is worth building:

* **ICU bids here, and that is new.** ICU is the natural bidder for a ward bed — it wants to
  step patients down to free its own capacity — and until Step 10 it was eligible nowhere,
  because Operational had no ICU formula (F-D). It does now, so this is the first profile
  where the department with the strongest claim on the resource can actually bid for it.
* **"Does ICU help?" becomes "is a ward bed sufficient?"** Clinical Benefit's 0.25 weight slot
  currently reads ``rules/icu_benefit.yaml``, whose question is inverted here. Step 9.
* **Ward sits at the bottom of the care ladder**, so almost every alternative is an escalation
  rather than a fallback. Scoring ICU as a comfortable alternative to a ward bed — quality
  1.0, "you need this less" — is exactly the misreading Step 8 exists to fix.

Registering the profile does not fix any of those. It makes them reachable, and therefore
testable, instead of hypothetical.
"""

from __future__ import annotations

from allocation.contracts import AgentKind, ResourceType
from allocation.profiles.bed import ICU_BIDDER_NOTE, bed_profile
from allocation.profiles.registry import REGISTRY

WARD_BED = REGISTRY.register(
    bed_profile(
        resource_type=ResourceType.WARD_BED,
        description=(
            "One general inpatient ward bed: no continuous monitoring, no vasopressors. "
            "The destination for admissions from ER and step-downs from ICU, HDU and PACU."
        ),
        qualifiers=(
            "ward", "general ward", "inpatient ward", "medical ward", "surgical ward",
            "inpatient", "floor",
        ),
        # ER admits into it, OT sends post-operative patients to it, Ward bids for internal
        # moves, and ICU steps patients down into it — the strongest claim of the four, since
        # a ward bed is how ICU frees an ICU bed. Step 10 defined ICU's operational formula,
        # which is what made it eligible.
        eligible_agents=(AgentKind.ER, AgentKind.OT, AgentKind.WARD, AgentKind.ICU),
        # ⚠ UNFITTED — carried from ICU. See the caps note below. ICU's 15 mirrors Ward's:
        # a step-down decision goes stale at about the rate an internal ward move does.
        ttl_minutes={
            AgentKind.ER: 10,
            AgentKind.OT: 20,
            AgentKind.WARD: 15,
            AgentKind.ICU: 15,
        },
        # units.yaml gives ward a safe_hold_hours of 0.0 — a ward cannot hold a deteriorating
        # patient at all. That makes it a poor *alternative*, which is a statement about the
        # ladder, not about how far ahead a ward-bed allocation reaches. Horizon stays 4 h.
        allocation_horizon_hours=4.0,
        notes=(
            "Caps in caps_ward_bed.yaml are UNFITTED — copied from the ICU table. A ward bed "
            "and an ICU bed do not plausibly share maxima: ICU-bed utilities run ~107 in the "
            "worked example and a ward bed cannot be worth the same 60-point clinical "
            "benefit. This is the single most misleading inheritance in the family.",
            ICU_BIDDER_NOTE,
            "Clinical Benefit still reads rules/icu_benefit.yaml, which asks whether ICU "
            "helps. For a ward bed the question is whether a ward bed is sufficient. Step 9.",
        ),
    )
)
