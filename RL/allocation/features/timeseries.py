"""Slopes over a vitals window.

``slope(f) = (f_now - f_earliest_in_window) / hours_elapsed`` where the window is 4 hours
(D.0). Readings outside it are excluded — Appendix C.4 notes explicitly that Ward's 08:00
reading "falls outside the 4-hour window".

A slope needs two readings. One reading is not a trend, and returning 0.0 for it would say
"this patient is stable" about a patient nobody has measured twice. Every function here
returns ``None`` in that case and the caller converts it to an absent :class:`Signal`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Sequence

from allocation.contracts import VitalsReading

Extractor = Callable[[VitalsReading], float | None]


def in_window(
    readings: Sequence[VitalsReading], now: datetime, window_hours: float
) -> tuple[VitalsReading, ...]:
    """Readings at or before ``now`` and no older than ``window_hours``, oldest first."""
    cutoff = now - timedelta(hours=window_hours)
    kept = [r for r in readings if cutoff <= r.recorded_at <= now]
    return tuple(sorted(kept, key=lambda r: r.recorded_at))


def latest(
    readings: Sequence[VitalsReading], now: datetime, window_hours: float
) -> VitalsReading | None:
    window = in_window(readings, now, window_hours)
    return window[-1] if window else None


def slope_per_hour(
    readings: Sequence[VitalsReading],
    extract: Extractor,
    now: datetime,
    window_hours: float,
) -> float | None:
    """Change per hour between the earliest and latest readings that both have a value.

    ``None`` when fewer than two readings carry the value, or when they share a timestamp.
    """
    window = [r for r in in_window(readings, now, window_hours) if extract(r) is not None]
    if len(window) < 2:
        return None
    first, last = window[0], window[-1]
    hours = (last.recorded_at - first.recorded_at).total_seconds() / 3600.0
    if hours <= 0:
        return None
    start, end = extract(first), extract(last)
    assert start is not None and end is not None  # filtered above
    return (end - start) / hours


def series_slope(
    values: Sequence[tuple[datetime, float]], now: datetime, window_hours: float
) -> float | None:
    """Slope over an arbitrary ``(timestamp, value)`` series, e.g. computed NEWS2 scores."""
    cutoff = now - timedelta(hours=window_hours)
    window = sorted((t, v) for t, v in values if cutoff <= t <= now)
    if len(window) < 2:
        return None
    hours = (window[-1][0] - window[0][0]).total_seconds() / 3600.0
    if hours <= 0:
        return None
    return (window[-1][1] - window[0][1]) / hours
