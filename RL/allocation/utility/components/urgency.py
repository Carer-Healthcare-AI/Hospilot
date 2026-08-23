"""Urgency — cap 40. D.2.

``0.35 NEWS2 + 0.30 OxygenTrend + 0.20 VitalTrend + 0.15 TimeToCritical``

Time-to-critical (.15) has no model. It is B.5, and B.5 is in the group Appendix B calls *"not
blocked and nobody has noticed"* — a survival model over ``vitals`` trajectories, trainable
from data already held, needing no auction history. Until it exists this component runs at
85 % coverage.
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
from allocation.features import news2 as news2_features
from allocation.features import timeseries
from allocation.features.scale import clamp
from allocation.utility.engine import weighted


class Urgency:
    name = ComponentName.URGENCY

    def __init__(self, config: Config) -> None:
        self._config = config

    def score(
        self, candidate: Candidate, snapshot: FeatureSnapshot, agent: AgentKind
    ) -> ComponentScore:
        cfg = self._config
        w = cfg.weights("urgency")
        data = snapshot.for_candidate(candidate.candidate_id)
        now = snapshot.taken_at
        window = float(cfg.threshold("vitals", "slope_window_hours"))

        return weighted(
            [
                FactorScore("news2", w["news2"], self._news2(data.vitals, now, window)),
                FactorScore("oxygen_trend", w["oxygen_trend"],
                            self._oxygen_trend(data.vitals, now, window)),
                FactorScore("vital_trend", w["vital_trend"],
                            self._vital_trend(data.vitals, now, window)),
                FactorScore("time_to_critical", w["time_to_critical"], self._time_to_critical()),
            ],
            source="urgency",
        )

    def _news2(self, vitals: Any, now: Any, window: float) -> Signal:
        """``NEWS2_now / 20``."""
        bands = self._config.threshold("news2_bands")
        latest = timeseries.latest(vitals, now, window)
        if latest is None:
            return Signal.absent("vitals", "no reading in the window")
        score = news2_features.score_reading(latest, bands)
        note = f"coverage {score.coverage:.0%}"
        if score.missing:
            note += f", missing {', '.join(score.missing)}"
        return Signal(score.normalised, "vitals.news2", note)

    def _oxygen_trend(self, vitals: Any, now: Any, window: float) -> Signal:
        """``clamp(-slope(spo2) / 3.0)`` — only a FALLING SpO2 scores. A rise is not urgency."""
        slope = timeseries.slope_per_hour(vitals, lambda r: r.spo2, now, window)
        if slope is None:
            return Signal.absent("vitals.spo2", "fewer than two SpO2 readings in the window")
        cap = float(self._config.threshold("urgency", "spo2_fall_cap_per_hour"))
        return Signal(clamp(-slope / cap), "vitals.spo2_slope")

    def _vital_trend(self, vitals: Any, now: Any, window: float) -> Signal:
        """Mean of the absolute pulse and systolic-BP slopes, each capped.

        Absolute value in both directions: a collapsing blood pressure and a spiking one are
        each instability. Sub-slopes that are unavailable are dropped, not zeroed.
        """
        cfg = self._config
        parts: list[float] = []

        pulse = timeseries.slope_per_hour(vitals, lambda r: r.pulse, now, window)
        if pulse is not None:
            parts.append(clamp(abs(pulse) / float(cfg.threshold("urgency", "pulse_slope_cap_per_hour"))))

        bp = timeseries.slope_per_hour(vitals, lambda r: r.bp_systolic, now, window)
        if bp is not None:
            parts.append(clamp(abs(bp) / float(cfg.threshold("urgency", "bp_slope_cap_per_hour"))))

        if not parts:
            return Signal.absent("vitals", "no pulse or BP slope available")
        return Signal(sum(parts) / len(parts), "vitals.pulse+bp_systolic_slope")

    def _time_to_critical(self) -> Signal:
        """B.5 — a survival model over vitals trajectories. Trainable today; not built."""
        return Signal.absent("model.time_to_critical", "B.5 not built; buildable from vitals history")
