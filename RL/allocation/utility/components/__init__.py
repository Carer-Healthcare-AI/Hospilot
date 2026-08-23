"""The eight utility components, one module each.

Each implements :class:`~allocation.contracts.Component`: it returns a normalised score in
``[0, 1]`` plus the factors behind it, and never applies its own cap. The engine does that,
from the resource's caps table, so re-fitting the caps (B.13) never touches this package.

Three of the eight are products rather than weighted sums — Waiting (D.3), Alternative (D.7)
and Operational (D.5, which is a single per-agent rule) — so an absent term makes the whole
component absent instead of dragging it toward zero.
"""

from __future__ import annotations

from typing import Mapping

from allocation.config import Config
from allocation.contracts import Component, ComponentName
from allocation.profiles.registry import ResourceProfile
from allocation.utility.components.alternative import Alternative
from allocation.utility.components.clinical_benefit import ClinicalBenefit
from allocation.utility.components.financial import Financial
from allocation.utility.components.operational import Operational
from allocation.utility.components.resource_stress import ResourceStress
from allocation.utility.components.throughput import Throughput
from allocation.utility.components.urgency import Urgency
from allocation.utility.components.waiting import Waiting

__all__ = [
    "Alternative",
    "ClinicalBenefit",
    "Financial",
    "Operational",
    "ResourceStress",
    "Throughput",
    "Urgency",
    "Waiting",
    "build_components",
]


def build_components(
    config: Config, profile: ResourceProfile
) -> Mapping[ComponentName, Component]:
    """Instantiate every component a profile declares.

    Constructed per profile rather than imported as singletons, so a second resource type can
    carry a different component set — and so tests can substitute one component without
    touching the other seven.
    """
    available: dict[ComponentName, Component] = {
        # Target-unit benefit is per resource: the question inverts across the bed family.
        ComponentName.CLINICAL_BENEFIT: ClinicalBenefit(config, profile.resource_type.value),
        ComponentName.URGENCY: Urgency(config),
        ComponentName.WAITING: Waiting(config),
        ComponentName.THROUGHPUT: Throughput(config),
        ComponentName.OPERATIONAL: Operational(config),
        ComponentName.FINANCIAL: Financial(config),
        # Alternative is scored relative to the unit being auctioned, so it is the one
        # component that needs to know which resource this is (care ladder, Step 8).
        ComponentName.ALTERNATIVE: Alternative(
            config, profile.allocation_horizon_hours, profile.resource_type.unit
        ),
        ComponentName.RESOURCE_STRESS: ResourceStress(config),
    }
    return {name: available[name] for name in profile.components}
