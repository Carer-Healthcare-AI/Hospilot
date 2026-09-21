"""Initial, explicitly unfitted diagnostic-machine utility contract.

The caps preserve the bed use case's mathematical scale while assigning diagnostic meanings.
They are assumptions for simulation and must not be treated as clinically signed off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class DiagnosticComponent(str, Enum):
    DIAGNOSTIC_VALUE = "diagnostic_value"
    URGENCY = "urgency"
    DELAY_PRESSURE = "delay_pressure"
    OPERATIONAL_IMPACT = "operational_impact"
    MACHINE_SCARCITY = "machine_scarcity"
    ALTERNATIVE_QUALITY = "alternative_quality"
    TRANSPORT_BURDEN = "transport_burden"
    RESOURCE_STRESS = "resource_stress"


# Same point scale as the bed framework; meanings are diagnostic-specific. Negative caps
# already carry their sign, so utility remains a plain sum.
INITIAL_ASSUMED_CAPS: Mapping[DiagnosticComponent, float] = {
    DiagnosticComponent.DIAGNOSTIC_VALUE: 60.0,
    DiagnosticComponent.URGENCY: 40.0,
    DiagnosticComponent.DELAY_PRESSURE: 25.0,
    DiagnosticComponent.OPERATIONAL_IMPACT: 25.0,
    DiagnosticComponent.MACHINE_SCARCITY: 20.0,
    DiagnosticComponent.ALTERNATIVE_QUALITY: -20.0,
    DiagnosticComponent.TRANSPORT_BURDEN: -10.0,
    DiagnosticComponent.RESOURCE_STRESS: -10.0,
}


@dataclass(frozen=True, slots=True)
class DiagnosticUtility:
    scores: Mapping[DiagnosticComponent, float]
    caps: Mapping[DiagnosticComponent, float] = field(
        default_factory=lambda: dict(INITIAL_ASSUMED_CAPS)
    )

    def __post_init__(self) -> None:
        missing = set(self.caps) - set(self.scores)
        extra = set(self.scores) - set(self.caps)
        if missing or extra:
            raise ValueError(
                f"diagnostic scores must match caps; missing={sorted(x.value for x in missing)}, "
                f"extra={sorted(x.value for x in extra)}"
            )
        invalid = {name: score for name, score in self.scores.items() if not 0 <= score <= 1}
        if invalid:
            raise ValueError(f"diagnostic component scores must be in [0, 1]: {invalid}")

    @property
    def points(self) -> Mapping[DiagnosticComponent, float]:
        return {name: self.caps[name] * score for name, score in self.scores.items()}

    @property
    def total(self) -> float:
        return sum(self.points.values())
