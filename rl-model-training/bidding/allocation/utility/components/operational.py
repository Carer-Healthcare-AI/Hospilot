"""Operational Impact — cap 20. D.5.

**Per agent, with no shared formula.** RL-Steps gives none; D.5's three variants are the
framework's. The burden relieved by admitting a patient differs in kind, not degree, between
departments: OT is measured in cases at risk of cancellation, ER in boarding and
left-without-being-seen pressure, Ward in nursing load.

There is deliberately no fallback for an unknown agent — inventing one would put a number on
a department whose operational burden nobody has defined.

``ICU`` was that department until Step 10. It is the natural bidder for a step-down bed and
was eligible nowhere, because an ineligible-but-scored bidder would have run at reduced
coverage against agents scored in full. Its formula is the nursing-saturation rule, the same
shape as Ward's — a decision, not a measurement, and :meth:`Operational._icu` records what it
does and does not capture.
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


class Operational:
    name = ComponentName.OPERATIONAL

    def __init__(self, config: Config) -> None:
        self._config = config

    def score(
        self, candidate: Candidate, snapshot: FeatureSnapshot, agent: AgentKind
    ) -> ComponentScore:
        data = snapshot.for_candidate(candidate.candidate_id)
        state = snapshot.hospital

        if agent is AgentKind.OT:
            signal = self._ot(data)
        elif agent is AgentKind.ER:
            signal = self._er(state)
        elif agent is AgentKind.WARD:
            signal = self._ward(data)
        elif agent is AgentKind.ICU:
            signal = self._icu(data)
        else:
            signal = Signal.absent(
                "profile", f"no operational formula defined for agent {agent.value!r}"
            )

        return ComponentScore(
            normalised=signal,
            coverage=1.0 if signal.present else 0.0,
            factors=(FactorScore(f"operational_{agent.value}", 1.0, signal),),
        )

    def _ot(self, data: Any) -> Signal:
        """``clamp(cases_at_risk / 5)`` — following cases threatened by a cancellation."""
        if data.ot_cases_at_risk is None:
            return Signal.absent("ot_surgery_schedule", "no following-case count available")
        cap = float(self._config.threshold("operational", "cases_at_risk_cap"))
        return Signal(clamp(data.ot_cases_at_risk / cap), "ot_surgery_schedule")

    def _er(self, state: Any) -> Signal:
        """``0.5 clamp(boarding/10) + 0.5 clamp(lwbs_risk)``."""
        if state.boarding_count is None or state.lwbs_risk is None:
            return Signal.absent("/er/boarding+/er/lwbs", "boarding or LWBS unavailable")
        cap = float(self._config.threshold("throughput", "boarding_cap"))
        boarding = clamp(state.boarding_count / cap)
        return Signal(clamp(0.5 * boarding + 0.5 * clamp(state.lwbs_risk)), "/er/boarding+/er/lwbs")

    def _ward(self, data: Any) -> Signal:
        """The nursing-saturation rule, on the patient's own ward."""
        return self._nursing(data)

    def _icu(self, data: Any) -> Signal:
        """The nursing-saturation rule again, read for the ICU.

        ICU is the natural bidder for a step-down bed — it wants a ward or HDU bed to move a
        patient into so its own capacity frees up — and it had no formula at all until this
        was defined, so it would have bid at permanently reduced coverage (F-D).

        **Two things this measures less well than it looks.**

        *It is nursing load, not bed pressure.* ICU wants the step-down bed because its beds
        are full, and the closest thing to that is its occupancy. ``HospitalState`` is now
        unit-scoped to the bed being *auctioned*, so during a ward-bed auction it holds ward
        beds, not ICU's. Reading ICU's own occupancy needs the Step 12 adapter.

        *The saturation constant is the ward's.* ``nursing_saturation`` is 8 pending tasks per
        nurse, chosen for a ward. ICU nurses at 1:1 do not saturate at the same ratio, and
        nobody has fitted an ICU figure. The note says so on every score rather than a second
        unfitted constant being added to the config.
        """
        return self._nursing(
            data,
            note="ward saturation constant reused for ICU — unfitted; measures nursing load, "
                 "not the bed pressure ICU is actually bidding on (Step 12)",
        )

    def _nursing(self, data: Any, note: str = "") -> Signal:
        """``clamp((pending_tasks / nurses) / saturation)``.

        ``ward_nurses`` is named for its first consumer but holds the nurse count on the
        *candidate's current unit*, whichever that is. The Step 12 adapter must populate it
        for the bidding department's unit, not always a ward.
        """
        if data.pending_nursing_tasks is None or data.ward_nurses is None:
            return Signal.absent("nursing_tasks+staff_roster", "task or nurse count unavailable")
        saturation = float(self._config.threshold("throughput", "nursing_saturation"))
        ratio = data.pending_nursing_tasks / max(data.ward_nurses, 1)
        return Signal(clamp(ratio / saturation), "nursing_tasks+staff_roster", note)
