"""Alternative Availability — cap 0 to -20. D.7.

``AL = -20 * Quality * Duration``

``Quality  = |needs & capability(best_alt_unit)| / |needs|``
``Duration = clamp(safe_hold_hours / allocation_horizon_hours)``

**The largest single discriminator in the utility, and the least evidenced.** In Appendix C.5
it produced -2.4 / -13.0 / -14.0, a wider spread than any other negative term, and it is what
makes Ward withdraw in section 12 and OT in section 16 — two of the three departures in the
whole auction. Both tables behind it are invented (B.4, B.7); neither exists in the schema.

**The score is relative to the unit being auctioned.** D.7 was written when the only
auctionable resource was an ICU bed, so every alternative was implicitly *below* the target
and "alternative" and "less capable unit" meant the same thing. With a bed family they come
apart, and two rules now apply (``care_ladder`` in ``rules/units.yaml``):

* **A unit is not an alternative to itself.** A ward bed is no fallback for a ward bed.
* **An escalation is not a fallback.** Auction an HDU bed and ICU sits above it. Scoring ICU
  as a comfortable alternative — quality 1.0, "you need this less" — reads a scarcer, better,
  harder-to-get bed as a convenience, and would penalise a patient precisely for being sick
  enough to need more than the bed on offer.

A patient with no viable alternative scores 0 here, which is correct and is not the same as
absent: "we looked and there is nothing" is a finding. A patient whose needs are unrecorded is
absent.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from allocation.config import Config
from allocation.contracts import (
    AgentKind,
    CareNeed,
    Candidate,
    ComponentName,
    ComponentScore,
    FactorScore,
    FeatureSnapshot,
    Signal,
)
from allocation.features.scale import clamp


class Alternative:
    name = ComponentName.ALTERNATIVE

    def __init__(
        self,
        config: Config,
        allocation_horizon_hours: float,
        target_unit: str | None = None,
    ) -> None:
        self._config = config
        # From the resource profile, not a threshold: "how long is safe" is measured against
        # the window this resource is being allocated over.
        self._horizon = allocation_horizon_hours
        # Which unit's bed is being auctioned, so alternatives can be ranked against it.
        # ``None`` keeps the pre-ladder behaviour: every alternative is scored as a fallback,
        # which is what D.7 assumed when ICU was the only auctionable resource.
        self._target_unit = target_unit

    def score(
        self, candidate: Candidate, snapshot: FeatureSnapshot, agent: AgentKind
    ) -> ComponentScore:
        data = snapshot.for_candidate(candidate.candidate_id)
        units = self._candidate_units(data)

        best = self._best_alternative(candidate, units)
        if best is None:
            quality = Signal(0.0, "beds", self._nothing_note(units))
            duration = Signal(0.0, "beds", self._nothing_note(units))
        else:
            quality, duration = best

        factors = (
            FactorScore("quality", 1.0, quality),
            FactorScore("duration", 1.0, duration),
        )

        missing = [f.name for f in factors if not f.present]
        if missing:
            return ComponentScore(
                normalised=Signal.absent("alternative", f"missing: {', '.join(missing)}"),
                coverage=(2 - len(missing)) / 2,
                factors=factors,
            )

        product = float(quality.value or 0.0) * float(duration.value or 0.0)
        return ComponentScore(
            normalised=Signal(clamp(product), "alternative"),
            coverage=1.0,
            factors=factors,
        )

    # -- alternatives ------------------------------------------------------------------

    def _candidate_units(self, data: Any) -> tuple[str, ...]:
        """Every unit offered as an alternative, classified and de-duplicated.

        ``alternative_units`` is the list form; ``best_alternative_unit`` is the single-value
        shorthand that predates it. Both are read so no existing scenario or adapter breaks.
        """
        raw: Sequence[str | None] = getattr(data, "alternative_units", ()) or ()
        if not raw:
            raw = (getattr(data, "best_alternative_unit", None),)

        seen: list[str] = []
        for ward in raw:
            unit = self.classify_unit(ward)
            if unit is not None and unit not in seen:
                seen.append(unit)
        return tuple(seen)

    def _best_alternative(
        self, candidate: Candidate, units: Sequence[str]
    ) -> tuple[Signal, Signal] | None:
        """The alternative that most reduces the need for this bed, after the ladder rules.

        "Best" is the largest ``quality * duration``: the strongest fallback is the one that
        should discount the bid furthest. Escalations and the target unit itself are scaled or
        removed first, so the winner is always a genuine fallback.

        Returns ``None`` when nothing survives — including when the only alternatives offered
        were escalations. Absence of *needs* is different and propagates as an absent Signal.
        """
        scored: list[tuple[float, Signal, Signal]] = []
        for unit in units:
            if self._target_unit is not None and unit == self._target_unit:
                continue  # a unit is not an alternative to itself

            quality = self._quality(candidate, unit)
            duration = self._duration(unit)
            if not quality.present or not duration.present:
                # Needs unrecorded, or no table row for this unit. Surface it rather than
                # silently preferring a unit that happens to have complete config.
                return quality, duration

            factor = self._escalation_factor(unit)
            if factor == 0.0:
                continue

            value = float(quality.value or 0.0) * float(duration.value or 0.0) * factor
            if factor != 1.0:
                note = f"{quality.note}; escalation to {unit} scaled by {factor:g}"
                quality = Signal(quality.value, quality.source, note)
            scored.append((value, quality, duration))

        if not scored:
            return None
        best = max(scored, key=lambda row: row[0])
        return best[1], best[2]

    def _nothing_note(self, units: Sequence[str]) -> str:
        """Why the score is a real zero rather than an absence.

        "We looked and there is nothing below this unit" is a finding, and the note has to say
        which units were considered — otherwise a zero here is indistinguishable from a
        patient whose alternatives were never recorded.
        """
        if not units or self._target_unit is None:
            return "no alternative unit available"
        offered = ", ".join(units)
        if tuple(units) == (self._target_unit,):
            verdict = "is the unit being auctioned"
        elif len(units) == 1:
            verdict = "is an escalation, not a fallback"
        else:
            verdict = "are escalations or the unit itself"
        return f"no fallback below {self._target_unit}: {offered} {verdict}"

    def _ladder(self) -> tuple[str, ...]:
        return tuple(str(u) for u in self._config.rule("units").get("care_ladder", ()))

    def _escalation_factor(self, unit: str) -> float:
        """``1.0`` for a de-escalation, the configured multiplier for an escalation.

        A unit missing from the ladder is treated as a de-escalation: the ladder is a config
        table and an unlisted unit is a config gap, not a reason to silently zero the largest
        discriminator in the utility.
        """
        if self._target_unit is None:
            return 1.0
        ladder = self._ladder()
        if unit not in ladder or self._target_unit not in ladder:
            return 1.0
        if ladder.index(unit) < ladder.index(self._target_unit):
            return float(
                self._config.rule("units").get("escalation_penalty_multiplier", 0.0)
            )
        return 1.0

    # -- the two terms -----------------------------------------------------------------

    def classify_unit(self, ward: str | None) -> str | None:
        """Map a free-text ``beds.ward`` onto a unit type.

        ``hospilot.beds`` has no unit-type enum, so this is ordered pattern matching against
        a config table (BUILD_SPEC 5.5). The final empty pattern is the catch-all.
        """
        if ward is None:
            return None
        text = ward.strip().lower()
        for row in self._config.rule("units")["ward_patterns"]:
            pattern = str(row["pattern"]).lower()
            if pattern == "" or re.search(re.escape(pattern), text):
                return str(row["unit"])
        return None

    def _quality(self, candidate: Candidate, unit: str) -> Signal:
        if not candidate.needs:
            return Signal.absent("candidate.needs", "care needs not recorded for this patient")

        table: Mapping[str, Any] = self._config.rule("units")["capability"]
        if unit not in table:
            return Signal.absent("rules.units", f"no capability vector for unit {unit!r} — B.7")

        capability = {CareNeed(c) for c in table[unit]}
        met = candidate.needs & capability
        return Signal(
            len(met) / len(candidate.needs),
            "rules.units.capability",
            f"{unit}: {len(met)}/{len(candidate.needs)} needs met",
        )

    def _duration(self, unit: str) -> Signal:
        table: Mapping[str, Any] = self._config.rule("units")["safe_hold_hours"]
        if unit not in table:
            return Signal.absent("rules.units", f"no safe-hold entry for unit {unit!r} — B.4")

        hours = float(table[unit]["default_hours"])
        return Signal(clamp(hours / self._horizon), "rules.units.safe_hold", f"{unit}: {hours} h")
