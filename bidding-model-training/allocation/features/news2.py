"""NEWS2, from ``hospilot.vitals``.

Seven parameters: respiratory rate, SpO2, supplemental oxygen, systolic BP, pulse,
consciousness, temperature. **Six of the seven exist in the table.** ``on_oxygen`` is B.1 and
is the single highest-leverage missing item in the whole spec — it is worth 2 of 20 points
here, and separately feeds Oxygen Severity (.20 of Clinical Benefit) and Oxygen Trend (.30 of
Urgency).

A missing parameter is **excluded and reported through coverage**, never scored 0. Scoring an
unrecorded oxygen flag as 0 asserts "this patient is on room air", which is the same class of
error as scoring an untested patient as healthy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from allocation.contracts import VitalsReading
from allocation.features.scale import band_by_max
from allocation.features.timeseries import in_window

# Parameter -> (attribute on VitalsReading, points available). Points are the NEWS2 maxima
# and are used only to weight coverage, never to score.
_PARAMETERS: tuple[tuple[str, str, int], ...] = (
    ("respiratory_rate", "respiratory_rate", 3),
    ("spo2", "spo2", 3),
    ("on_oxygen", "on_oxygen", 2),
    ("bp_systolic", "bp_systolic", 3),
    ("pulse", "pulse", 3),
    ("gcs", "gcs", 3),
    ("temperature", "temperature", 3),
)

MAX_SCORE = 20.0


@dataclass(frozen=True, slots=True)
class News2Score:
    """A NEWS2 total plus what it was able to see."""

    points: float
    coverage: float
    missing: tuple[str, ...]
    recorded_at: datetime

    @property
    def normalised(self) -> float:
        """``NEWS2 / 20`` — the Urgency NEWS2 factor (D.2)."""
        return min(self.points / MAX_SCORE, 1.0)


def score_reading(reading: VitalsReading, bands: Mapping[str, Any]) -> News2Score:
    """NEWS2 for one row of vitals."""
    total = 0.0
    available = 0
    total_params = 0
    missing: list[str] = []

    for name, attribute, weight in _PARAMETERS:
        total_params += weight
        value = getattr(reading, attribute)
        if value is None:
            missing.append(name)
            continue
        available += weight
        if name == "on_oxygen":
            oxygen = bands["on_oxygen"]
            total += float(oxygen["true_score"] if value else oxygen["false_score"])
        else:
            total += band_by_max(float(value), bands[name])

    return News2Score(
        points=total,
        coverage=available / total_params if total_params else 0.0,
        missing=tuple(missing),
        recorded_at=reading.recorded_at,
    )


def score_series(
    readings: Sequence[VitalsReading],
    bands: Mapping[str, Any],
    now: datetime,
    window_hours: float,
) -> tuple[News2Score, ...]:
    """NEWS2 for every reading inside the window, oldest first."""
    return tuple(score_reading(r, bands) for r in in_window(readings, now, window_hours))
