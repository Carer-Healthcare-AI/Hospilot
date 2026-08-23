"""Throughput Impact — cap 25. D.4.

``0.40 BedRelease + 0.30 NursingCapacity + 0.20 QueueImpact + 0.10 DownstreamImpact``

All four factors are hospital-flow measures, and three of them read hospital state rather than
the patient, so they discriminate less between bidders than their 25-point cap suggests.

**Two discrepancies between Appendix C and D.4, both surfaced rather than silently followed
(BUILD_SPEC F-20):**

* Appendix C computes Downstream as ``1 - 3/5 = 0.400`` for all three bidders, but C.1 declares
  expected discharges over 4 h as **1**, which gives 0.800. C.1's value is used here — it is
  the declared shared state, and Resource Stress in the same appendix reads it as 1.
* Appendix C gives Ward a Queue Impact of 0.300 while ER and OT get 0.700 from the same
  ``/er/boarding`` count of 7. D.4 defines it hospital-wide with no per-agent variant, so 0.700
  is used for every bidder.
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


class Throughput:
    name = ComponentName.THROUGHPUT

    def __init__(self, config: Config) -> None:
        self._config = config

    def score(
        self, candidate: Candidate, snapshot: FeatureSnapshot, agent: AgentKind
    ) -> ComponentScore:
        w = self._config.weights("throughput")
        data = snapshot.for_candidate(candidate.candidate_id)
        state = snapshot.hospital

        return weighted(
            [
                FactorScore("bed_release", w["bed_release"], self._bed_release(candidate)),
                FactorScore("nursing_capacity", w["nursing_capacity"], self._nursing(data)),
                FactorScore("queue_impact", w["queue_impact"], self._queue(state)),
                FactorScore("downstream_impact", w["downstream_impact"], self._downstream(state)),
            ],
            source="throughput",
        )

    def _bed_release(self, candidate: Candidate) -> Signal:
        """``UnitWeight[current_unit]`` — what moving this patient frees up."""
        table = self._config.threshold("throughput", "unit_weight")
        if candidate.current_unit is None:
            return Signal.absent("ipd_admissions", "no current unit recorded")
        unit = candidate.current_unit.strip().lower()
        if unit not in table:
            return Signal.absent(
                "config.unit_weight", f"no weight configured for unit {unit!r}"
            )
        return Signal(float(table[unit]), "config.unit_weight", unit)

    def _nursing(self, data: Any) -> Signal:
        """``clamp((pending_tasks / max(nurses, 1)) / 8)``.

        ``nursing_tasks`` has no status column and no ward: pending is ``completed = false``
        and the ward comes from ``admission_id -> ipd_admissions.bed_id -> beds.ward`` (F-08).
        """
        if data.pending_nursing_tasks is None or data.ward_nurses is None:
            return Signal.absent("nursing_tasks+staff_roster", "task or nurse count unavailable")
        saturation = float(self._config.threshold("throughput", "nursing_saturation"))
        ratio = data.pending_nursing_tasks / max(data.ward_nurses, 1)
        return Signal(clamp(ratio / saturation), "nursing_tasks+staff_roster")

    def _queue(self, state: Any) -> Signal:
        """``clamp(boarding_count / 10)``. Hospital-wide, per D.4 — see the module note."""
        if state.boarding_count is None:
            return Signal.absent("/er/boarding", "no boarding count available")
        cap = float(self._config.threshold("throughput", "boarding_cap"))
        return Signal(clamp(state.boarding_count / cap), "/er/boarding")

    def _downstream(self, state: Any) -> Signal:
        """``clamp(1 - predicted_discharges_4h / 5)``. Fewer discharges -> this move matters more."""
        if state.expected_discharges_4h is None:
            return Signal.absent("/discharge/volume", "no discharge forecast available")
        cap = float(self._config.threshold("throughput", "discharge_cap"))
        return Signal(clamp(1.0 - state.expected_discharges_4h / cap), "/discharge/volume")
