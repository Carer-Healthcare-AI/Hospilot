"""Ceiling uplift — the B.9 interim. RL-Steps section 8.2, formula D.9.

``Ceiling = U * (1 + uplift)``

The ceiling is a bidder's **maximum willingness to compete**, and RL-Steps puts it above
current utility: Ward values the bed at 86 but may bid to 105, *"because deterioration is
expected to raise its value over the next two hours"*. With ``uplift = 0`` that behaviour
cannot occur — an agent can never fight for a patient on account of who they are about to
become, only who they are.

**This module is off unless ``rules/uplift.yaml`` says otherwise, and that is deliberate.**
RL-Steps says the ceiling should exceed utility; it gives no rule for by how much. The bands
are ours. Switching them on is a deviation from the documents, not compliance with them.

**It is also the only unsigned table that reallocates a bed rather than distorting a score.**
A wrong cap moves everyone's points together and usually preserves the ranking. A wrong uplift
raises one bidder's ceiling and lets it outbid a rival it should have lost to. That asymmetry
is why the default is off and why ``Config.unsigned`` reports it.

The estimate is deliberately conservative in three ways: it floors at zero (an improving
patient never gets a ceiling below its utility, which D.9 does not define), it caps at
``max_uplift``, and it returns **absent** rather than "stable" when there is no trend to read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from allocation.config import Config
from allocation.contracts import PatientData
from allocation.features import news2 as news2_features
from allocation.features import timeseries


@dataclass(frozen=True, slots=True)
class UpliftEstimate:
    """An expected fractional rise in utility over the allocation horizon.

    ``value`` is 0.0 both when the model is disabled and when the patient is stable, so
    ``source`` and ``note`` are what distinguish "no uplift applies" from "no uplift could be
    computed". Both end up on the audit row.
    """

    value: float
    source: str
    note: str = ""
    band: str | None = None

    @property
    def applied(self) -> bool:
        return self.value > 0.0


DISABLED = UpliftEstimate(0.0, "disabled", "uplift.yaml enabled: false — D.9 fallback Ceiling = U")


def enabled(config: Config) -> bool:
    return bool(config.rule("uplift").get("enabled", False))


def estimate(
    config: Config,
    data: PatientData,
    now: datetime,
    window_hours: float,
) -> UpliftEstimate:
    """The expected uplift for one patient.

    Reads the same NEWS2 series that drives Clinical Benefit's deterioration factor — the one
    signal in the data that says *this patient is becoming sicker*, which is what the ceiling
    is meant to price.
    """
    cfg: Mapping[str, Any] = config.rule("uplift")
    if not cfg.get("enabled", False):
        return DISABLED

    bands = news2_features_bands(config)
    scores = news2_features.score_series(data.vitals, bands, now, window_hours)

    minimum = int(cfg.get("min_readings", 2))
    if len(scores) < minimum:
        return UpliftEstimate(
            0.0,
            "vitals",
            f"fewer than {minimum} NEWS2 readings in the window — no trend to read",
        )

    slope = timeseries.series_slope(
        [(s.recorded_at, s.points) for s in scores], now, window_hours
    )
    if slope is None:
        return UpliftEstimate(0.0, "vitals", "no NEWS2 slope available")

    if bool(cfg.get("floor_at_zero", True)):
        slope = max(0.0, slope)

    uplift, label = _band_for(cfg.get("bands", ()), slope)
    ceiling = float(cfg.get("max_uplift", 0.5))

    return UpliftEstimate(
        value=min(uplift, ceiling),
        source=str(cfg.get("source", "news2_slope_bands")),
        note=f"NEWS2 slope {slope:+.2f}/h",
        band=label,
    )


def _band_for(bands: Sequence[Mapping[str, Any]], slope: float) -> tuple[float, str | None]:
    """Highest band the slope reaches. Bands are read descending so order in YAML is free."""
    for band in sorted(bands, key=lambda b: -float(b["min_slope_per_hour"])):
        if slope >= float(band["min_slope_per_hour"]):
            return float(band["uplift"]), band.get("label")
    return 0.0, None


def news2_features_bands(config: Config) -> Mapping[str, Any]:
    return config.threshold("news2_bands")
