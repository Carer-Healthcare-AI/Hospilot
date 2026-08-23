"""Normalisation primitives. No clinical knowledge, no config reads — pure arithmetic.

Every scoring rule in the framework reduces to one of three shapes: clamp a ratio into
``[0, 1]``, look a value up in an ordered band table, or take a slope over a time window.
Keeping them here means the component modules read like the formulas in Appendix D.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return min(max(value, lo), hi)


def ratio(numerator: float, denominator: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """``clamp(numerator / denominator)``, guarding a zero denominator."""
    if denominator == 0:
        return hi if numerator > 0 else lo
    return clamp(numerator / denominator, lo, hi)


def deviation(value: float, base: float, span: float) -> float:
    """``clamp((value - base) / span)`` — the shape used by oxygen severity and age."""
    if span == 0:
        raise ValueError("span must be non-zero")
    return clamp((value - base) / span)


def band_by_max(value: float, bands: Sequence[Mapping[str, Any]]) -> float:
    """First band whose ``max`` is >= ``value``. A null ``max`` is unbounded.

    Bands are evaluated in file order, so the table reads top-to-bottom like the printed
    NEWS2 chart.
    """
    for band in bands:
        upper = band.get("max")
        if upper is None or value <= float(upper):
            return float(band["score"])
    raise ValueError(f"no band matched value {value!r}; the table needs an unbounded entry")


def band_by_min(value: float, bands: Sequence[Mapping[str, Any]]) -> float:
    """First band whose ``min`` is <= ``value``. Used by the organ-risk thresholds."""
    for band in bands:
        lower = band.get("min")
        if lower is None or value >= float(lower):
            return float(band["score"])
    raise ValueError(f"no band matched value {value!r}; the table needs a zero-floor entry")
