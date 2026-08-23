"""Resource Stress — cap 0 to -10. D.8.

``RS = -10 * (0.50 Occupancy + 0.30 DemandPressure + 0.20 IsolationPressure)``

**The only component where every factor already has a live source** — ``beds``,
``/icu/demand``, ``/discharge/volume``, ``infection_cases``.

**And the only one that cannot change who wins.** None of its three factors reads the patient,
so it is identical for every bidder in a round and shifts all bids down together. It matters
for budget spend and for the reserve price, not for the allocation. Appendix C.6 makes the
point directly: *"an identical term applied to every bidder cannot affect who wins. RL-Steps
places it inside the utility, where it looks decisive and isn't."*
"""

from __future__ import annotations

from allocation.config import Config
from allocation.contracts import (
    AgentKind,
    Candidate,
    ComponentName,
    ComponentScore,
    FactorScore,
    FeatureSnapshot,
    HospitalState,
    Signal,
)
from allocation.features.scale import clamp
from allocation.utility.engine import weighted


class ResourceStress:
    name = ComponentName.RESOURCE_STRESS

    def __init__(self, config: Config) -> None:
        self._config = config

    def score(
        self, candidate: Candidate, snapshot: FeatureSnapshot, agent: AgentKind
    ) -> ComponentScore:
        w = self._config.weights("resource_stress")
        state = snapshot.hospital

        return weighted(
            [
                FactorScore("occupancy", w["occupancy"],
                            Signal(clamp(state.occupancy), "beds")),
                FactorScore("demand_pressure", w["demand_pressure"], self._demand(state)),
                FactorScore("isolation_pressure", w["isolation_pressure"], self._isolation(state)),
            ],
            source="resource_stress",
        )

    # Both read their fields directly rather than through ``getattr(state, name, None)``
    # (checklist C-6). The three-state rule says absent must be absent — but a field *rename*
    # is not an absent input, and getattr made the two indistinguishable: the signal would
    # have degraded to ``Signal.absent`` on every patient, silently, and the only trace would
    # be a coverage figure nobody had a baseline for. Direct access fails at the rename.

    def _demand(self, state: HospitalState) -> Signal:
        """``clamp(predicted_demand_4h / max(expected_discharges_4h, 1))``."""
        demand = state.predicted_demand_4h
        discharges = state.expected_discharges_4h
        if demand is None or discharges is None:
            return Signal.absent("/icu/demand+/discharge/volume", "forecast unavailable")
        return Signal(clamp(demand / max(discharges, 1.0)), "/icu/demand+/discharge/volume")

    def _isolation(self, state: HospitalState) -> Signal:
        """``active_isolation_cases / unit_total_beds``."""
        pressure = state.isolation_pressure
        if pressure is None:
            return Signal.absent("infection_cases", "no isolation case count available")
        return Signal(clamp(pressure), "infection_cases")
