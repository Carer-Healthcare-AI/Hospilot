"""Financial Impact — cap 10. D.6.

``0.40 ExpectedRevenue + 0.30 AvoidedCost + 0.30 CapacityValue``

The smallest positive cap in the framework, deliberately: *"I would explicitly keep this
weight low so that an expensive elective case cannot overpower a much more clinically urgent
patient"* (RL-Steps section 8).

Worth watching anyway. Appendix C.6 observes that Financial is OT's **largest positive term**
at 10.0, against a Clinical Benefit of 7.5 — a stable elective patient's revenue outweighing
its clinical case. That is a consequence of the caps, which have never been fitted (B.13).

Avoided Cost (.30) has no source: there is no cost table anywhere (B.6). Finance provides.
"""

from __future__ import annotations

from typing import Any

from allocation.config import Config
from allocation.contracts import (
    AgentKind,
    Candidate,
    ComponentName,
    ComponentScore,
    FactorScore,
    FeatureSnapshot,
    Signal,
)
from allocation.features.scale import clamp
from allocation.utility.engine import weighted

# Which avoided-cost line each department's delay incurs.
_AVOIDED_COST_KEY = {
    AgentKind.OT: "ot_cancellation",
    AgentKind.ER: "ed_boarding",
    AgentKind.WARD: "ed_boarding",
}


class Financial:
    name = ComponentName.FINANCIAL

    def __init__(self, config: Config) -> None:
        self._config = config

    def score(
        self, candidate: Candidate, snapshot: FeatureSnapshot, agent: AgentKind
    ) -> ComponentScore:
        w = self._config.weights("financial")
        data = snapshot.for_candidate(candidate.candidate_id)

        return weighted(
            [
                FactorScore("expected_revenue", w["expected_revenue"], self._revenue(data)),
                FactorScore("avoided_cost", w["avoided_cost"], self._avoided(agent)),
                FactorScore("capacity_value", w["capacity_value"], self._capacity(snapshot)),
            ],
            source="financial",
        )

    def _revenue(self, data: Any) -> Signal:
        """``clamp(day_rate * expected_LOS / p95_revenue)``.

        Expected LOS comes from ``ipd_admissions.expected_discharge_at - admitted_at`` when
        present — no model needed — falling back to the configured per-agent estimate.
        """
        if data.icu_day_rate is None or data.expected_los_days is None:
            return Signal.absent(
                "contract_service_rates", "no ICU day rate or expected LOS available"
            )
        p95 = float(self._config.threshold("financial", "p95_revenue"))
        return Signal(
            clamp(data.icu_day_rate * data.expected_los_days / p95),
            "contract_service_rates+claims",
        )

    def _avoided(self, agent: AgentKind) -> Signal:
        """``clamp(avoided_cost / p95_avoided)`` — B.6, no cost table exists."""
        costs = self._config.rule("costs").get("avoided_cost", {})
        key = _AVOIDED_COST_KEY.get(agent)
        value = costs.get(key) if key else None
        if value is None:
            return Signal.absent("rules.costs", f"no avoided-cost line for {agent.value} — B.6")
        p95 = float(self._config.threshold("financial", "p95_avoided_cost"))
        return Signal(clamp(float(value) / p95), "rules.costs", "assumed — finance to provide")

    def _capacity(self, snapshot: FeatureSnapshot) -> Signal:
        """Current ICU occupancy fraction."""
        return Signal(clamp(snapshot.hospital.occupancy), "/icu/occupancy")
