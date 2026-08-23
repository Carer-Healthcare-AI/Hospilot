"""Weights, caps, coverage renormalisation, assembly.

**Coverage renormalisation is implemented once, here.** D.0::

                     sum(w_k * s_k)     over factors PRESENT
    Component = Cap * --------------
                     sum(w_k)           over factors PRESENT

    coverage  = sum(w_k present) / sum(w_k all)

A component returns a normalised score and the factors behind it; the engine applies the cap
from the resource's caps table. Components never see their own cap, so re-fitting the caps (B.13) is a
config change and never a code change — which matters, because every agent budget is
denominated in utility points and moves when the caps move.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Sequence

from allocation.config import Config
from allocation.contracts import (
    AgentKind,
    Candidate,
    Component,
    ComponentName,
    ComponentResult,
    ComponentScore,
    FactorScore,
    FeatureSnapshot,
    Signal,
    UtilityBreakdown,
)
from allocation.profiles.registry import ResourceProfile


def weighted(factors: Sequence[FactorScore], source: str) -> ComponentScore:
    """Coverage-renormalised weighted mean. The single implementation of the D.0 rule.

    Absent factors are dropped from both numerator and denominator. When every factor is
    absent the component itself is absent — it does not become 0.
    """
    total_weight = sum(f.weight for f in factors)
    if total_weight <= 0:
        raise ValueError(f"{source}: factor weights must sum to a positive number")

    present = [f for f in factors if f.present]
    present_weight = sum(f.weight for f in present)

    if present_weight <= 0:
        return ComponentScore(
            normalised=Signal.absent(source, "no factor had an input"),
            coverage=0.0,
            factors=tuple(factors),
        )

    value = sum(f.weight * float(f.signal.value or 0.0) for f in present) / present_weight
    return ComponentScore(
        normalised=Signal(min(max(value, 0.0), 1.0), source),
        coverage=present_weight / total_weight,
        factors=tuple(factors),
    )


class UtilityEngine:
    """Scores one candidate into a :class:`UtilityBreakdown`.

    Components are injected rather than imported, so a resource profile can carry a different
    set and tests can substitute one component without touching the rest.
    """

    def __init__(
        self,
        config: Config,
        profile: ResourceProfile,
        components: Mapping[ComponentName, Component],
    ) -> None:
        missing = [c for c in profile.components if c not in components]
        if missing:
            raise ValueError(
                f"profile {profile.resource_type.value} needs components with no "
                f"implementation: {[c.value for c in missing]}"
            )
        self._config = config
        self._profile = profile
        self._components = components

    def score(
        self, candidate: Candidate, snapshot: FeatureSnapshot, now: datetime | None = None
    ) -> UtilityBreakdown:
        results: list[ComponentResult] = []

        for name in self._profile.components:
            component = self._components[name]
            score = component.score(candidate, snapshot, candidate.agent)
            cap = self._config.cap(name.value)
            # Caps carry their own sign: Alternative (-20) and Resource Stress (-10) produce
            # negative points, so the utility is a plain sum. D.0's prose writes the utility
            # with minus signs in front of both; Appendix C sums the already-negative values.
            points = cap * float(score.normalised.value or 0.0)
            results.append(
                ComponentResult(
                    component=name,
                    cap=cap,
                    points=points,
                    coverage=score.coverage,
                    factors=score.factors,
                )
            )

        return UtilityBreakdown(
            candidate_id=candidate.candidate_id,
            agent=candidate.agent,
            components=tuple(results),
            caps_version=self._config.caps_version,
            config_version=self._config.config_version,
            computed_at=now or datetime.now(timezone.utc),
        )

    def score_all(
        self, candidates: Sequence[Candidate], snapshot: FeatureSnapshot
    ) -> dict[str, UtilityBreakdown]:
        return {c.candidate_id: self.score(c, snapshot, snapshot.taken_at) for c in candidates}


def agent_of(candidate: Candidate) -> AgentKind:
    """Small helper so components read as the formulas do."""
    return candidate.agent
