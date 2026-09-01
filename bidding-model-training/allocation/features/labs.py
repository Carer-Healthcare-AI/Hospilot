"""Organ-failure risk — a mini-SOFA over ``hospilot.lab_results``. D.1, weight .15.

Four analytes: lactate, creatinine, bilirubin, platelets, each banded, then averaged over
**those present within 24 hours**.

Appendix A is explicit about the trap: *"Skip absent labs, don't zero them. Log coverage — a
score from one lab is not a score from four."* Zeroing an unmeasured lactate would make an
untested septic patient outrank a tested one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from allocation.contracts import LabResult
from allocation.features.scale import band_by_max, band_by_min

# Analytes that use an ascending scale (higher is worse) versus platelets, where lower is.
_ASCENDING = ("lactate", "creatinine", "bilirubin")
_DESCENDING = ("platelets",)
ANALYTES = _ASCENDING + _DESCENDING


@dataclass(frozen=True, slots=True)
class OrganRisk:
    score: float | None
    coverage: float
    present: tuple[str, ...]
    values: Mapping[str, float]


def _latest_by_analyte(
    labs: Sequence[LabResult], now: datetime, freshness_hours: float
) -> dict[str, LabResult]:
    cutoff = now - timedelta(hours=freshness_hours)
    newest: dict[str, LabResult] = {}
    for lab in labs:
        name = lab.test_name.strip().lower()
        if name not in ANALYTES or not (cutoff <= lab.reported_at <= now):
            continue
        if name not in newest or lab.reported_at > newest[name].reported_at:
            newest[name] = lab
    return newest


def organ_risk(
    labs: Sequence[LabResult],
    bands: Mapping[str, Any],
    now: datetime,
    freshness_hours: float,
) -> OrganRisk:
    """Mean of the sub-scores present. ``score`` is ``None`` when no analyte is available."""
    newest = _latest_by_analyte(labs, now, freshness_hours)
    sub: dict[str, float] = {}
    values: dict[str, float] = {}

    for analyte, lab in newest.items():
        table = bands[analyte]
        value = float(lab.result_value)
        values[analyte] = value
        sub[analyte] = (
            band_by_min(value, table) if analyte in _ASCENDING else band_by_max(value, table)
        )

    if not sub:
        return OrganRisk(score=None, coverage=0.0, present=(), values={})

    return OrganRisk(
        score=sum(sub.values()) / len(sub),
        coverage=len(sub) / len(ANALYTES),
        present=tuple(sorted(sub)),
        values=values,
    )
