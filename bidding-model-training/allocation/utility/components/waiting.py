"""Waiting / Delay Impact — cap 25. D.3.

``WD = 25 * clamp(P_det * Severity * DelayFactor / 2.0)``

**This component is not interchangeable across agents, and that is the framework's design.**
For a medical bidder ``P_det`` is the physiological probability of deterioration given delay.
For a surgical bidder the patient is anaesthetised and stable, so the physiological reading is
zero while the delay harm is real — the surgery cannot conclude without a bed. D.3 substitutes
``P(no PACU capacity)``.

Two consequences, both flagged:

* ``P_det`` has no model (B.8, buildable today from vitals history). Until then D.3 substitutes
  the Deterioration Risk already computed for Clinical Benefit.
* ``P(no PACU capacity)`` has **no source**: ``ot_room_status`` has no recovery-area concept
  (F-05). It is a bare configured number.

``DelayFactor = 1 + clamp(elapsed / 240 min)`` in ``[1, 2]`` is the term that makes the
component rise while an agent waits — 15 -> 19 -> 22 -> 25 in RL-Steps section 8 — and it needs
no model at all. Dividing the product by 2.0 returns it to ``[0, 1]``.

This is a **product, not a weighted mean**: an absent term makes the whole component absent
rather than silently collapsing it toward zero.
"""

from __future__ import annotations

from datetime import datetime
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
from allocation.features import news2 as news2_features
from allocation.features import timeseries
from allocation.features.scale import band_by_min, clamp

SURGICAL_AGENTS = (AgentKind.OT,)


class Waiting:
    name = ComponentName.WAITING

    def __init__(self, config: Config) -> None:
        self._config = config

    def score(
        self, candidate: Candidate, snapshot: FeatureSnapshot, agent: AgentKind
    ) -> ComponentScore:
        data = snapshot.for_candidate(candidate.candidate_id)
        now = snapshot.taken_at
        window = float(self._config.threshold("vitals", "slope_window_hours"))

        p_det = self._probability(data, agent, now, window)
        severity = self._severity(data, agent, now, window)
        delay = self._delay(candidate, now)

        factors = (
            FactorScore("p_deterioration", 1.0, p_det),
            FactorScore("severity", 1.0, severity),
            FactorScore("delay_factor", 1.0, delay),
        )
        missing = [f.name for f in factors if not f.present]
        if missing:
            return ComponentScore(
                normalised=Signal.absent("waiting", f"missing: {', '.join(missing)}"),
                coverage=(3 - len(missing)) / 3,
                factors=factors,
            )

        # `delay` carries the normalised part; the factor itself is 1 + that, in [1, 2].
        delay_factor = 1.0 + float(delay.value or 0.0)
        divisor = float(self._config.threshold("waiting", "delay_factor_max"))
        product = float(p_det.value or 0.0) * float(severity.value or 0.0) * delay_factor

        return ComponentScore(
            normalised=Signal(clamp(product / divisor), "waiting"),
            coverage=1.0,
            factors=factors,
        )

    # -- terms ---------------------------------------------------------------------

    def _probability(
        self, data: Any, agent: AgentKind, now: datetime, window: float
    ) -> Signal:
        if agent in SURGICAL_AGENTS:
            if data.pacu_capacity_probability is None:
                return Signal.absent("ot_room_status", "P(no PACU capacity) has no source — F-05")
            return Signal(
                clamp(data.pacu_capacity_probability),
                "config.p_no_pacu",
                "substituted for P(deterioration): a stable anaesthetised patient reads zero",
            )

        slope = self._news2_slope(data, now, window)
        if slope is None:
            return Signal.absent("vitals", "no NEWS2 slope to substitute for P_det")
        cap = float(self._config.threshold("clinical_benefit", "news2_slope_cap_per_hour"))
        return Signal(
            clamp(slope / cap),
            "vitals.news2_slope",
            "DeteriorationRisk substituted for P(deterioration|delay) — B.8 not built",
        )

    def _severity(self, data: Any, agent: AgentKind, now: datetime, window: float) -> Signal:
        """Severity if deterioration occurs.

        For a surgical bidder the consequence is categorical — the case cannot conclude — so
        severity is 1.0 rather than a NEWS2 band, which would read a stable anaesthetised
        patient as trivial. Appendix C.3 does exactly this.
        """
        if agent in SURGICAL_AGENTS:
            return Signal(
                1.0, "profile.surgical", "delay consequence is categorical, not physiological"
            )

        latest = timeseries.latest(data.vitals, now, window)
        if latest is None:
            return Signal.absent("vitals", "no reading in the window")

        points = news2_features.score_reading(
            latest, self._config.threshold("news2_bands")
        ).points
        bands = [
            {"min": b["min_news2"], "score": b["score"]}
            for b in self._config.threshold("waiting", "severity_bands")
        ]
        return Signal(band_by_min(points, bands), "vitals.news2_band")

    def _delay(self, candidate: Candidate, now: datetime) -> Signal:
        """The normalised part of DelayFactor. The caller reconstitutes ``1 + x``."""
        if candidate.arrived_at is None:
            return Signal.absent("visits.arrived_at", "no arrival or escalation time recorded")
        elapsed = (now - candidate.arrived_at).total_seconds() / 60.0
        window = float(self._config.threshold("waiting", "delay_window_minutes"))
        return Signal(clamp(elapsed / window), "visits.arrived_at", f"{elapsed:.0f} min elapsed")

    def _news2_slope(self, data: Any, now: datetime, window: float) -> float | None:
        scores = news2_features.score_series(
            data.vitals, self._config.threshold("news2_bands"), now, window
        )
        if len(scores) < 2:
            return None
        return timeseries.series_slope([(s.recorded_at, s.points) for s in scores], now, window)
