"""When is the next bed of this type expected. *Q(Wait for Next Resource)*.

RL-Steps' example is precise: *"OT predicts an ICU discharge in 35 min with 88 % confidence."*
**No endpoint produces that.** ``/discharge/volume`` gives ``expected_discharges_4h`` — a rate
over a four-hour window — and nothing anywhere gives a timed release with a confidence. A
per-bed discharge-timing model is B.10-adjacent work that does not exist.

So this module does the one honest thing available: it derives a timing from the rate under a
**single stated assumption**, and marks every value it returns as derived rather than forecast.

    releases arrive as a Poisson process at rate  lambda = expected_discharges_4h / 4 h

    E[time to next release]  =  1 / lambda
    P(release within w)      =  1 - exp(-lambda * w)

That assumption is wrong in a known direction: discharges cluster on ward rounds and shift
changes, so real releases are burstier than Poisson. A burstier process has the same mean and a
*longer* median wait, which means this **over-states** ``release_probability`` for short windows.
An agent trusting it waits slightly too often. Recorded here rather than discovered later, and
carried on :attr:`NextRelease.basis` so it reaches the audit row.

The alternative — a hard-coded 35 minutes at 88 % because RL-Steps wrote those numbers down —
would be a constant with no derivation at all, and it would be indistinguishable in the log from
a real forecast. This is at least reproducible from an input that exists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from allocation.config import Config
from allocation.contracts import HospitalState

#: Below this many expected discharges the derivation is meaningless — a rate of zero puts the
#: next release at infinity, and a rate near zero puts it beyond any horizon anyone cares about.
_MIN_RATE = 1e-6


@dataclass(frozen=True, slots=True)
class NextRelease:
    """When the next bed of this type is expected, and how much that is worth trusting.

    ``probability`` is always scoped to a window — ``P(a bed frees within w)`` — because an
    unscoped "88 % confident" has no meaning. RL-Steps' phrasing conflates the two and this
    type deliberately does not.
    """

    #: ``None`` when the rate is unknown or zero. Not a far-future date: "we cannot say" and
    #: "not for a long time" are different claims and only one of them is true here.
    expected_at: datetime | None
    #: ``P(at least one release within the window)``, or ``None`` alongside ``expected_at``.
    probability: float | None
    window_minutes: float
    #: The rate this was derived from, per hour. Retained so the audit row can be re-derived.
    rate_per_hour: float | None
    #: What produced it. Reaches the bid row, so a wait decision can never be mistaken in the
    #: log for one taken against a real discharge-timing model.
    basis: str

    @property
    def known(self) -> bool:
        return self.expected_at is not None and self.probability is not None


def next_release(
    config: Config,
    hospital: HospitalState,
    at: datetime,
    window_minutes: float,
) -> NextRelease:
    """Derive the next-release estimate for ``hospital``'s unit at ``at``.

    ``window_minutes`` is how long *this patient* can safely wait, not a fixed horizon. The
    same hospital state yields a different probability for a patient with a 45-minute window
    than for one with four hours, and that difference is the entire content of the decision.
    """
    cfg = config.rule("pathway").get("next_release", {})
    if not bool(cfg.get("enabled", True)):
        return NextRelease(None, None, window_minutes, None, "disabled in rules/pathway.yaml")

    discharges = hospital.expected_discharges_4h
    if discharges is None:
        # Absent is absent. A missing forecast is not a hospital with no discharges.
        return NextRelease(
            None, None, window_minutes, None,
            "expected_discharges_4h absent — no release estimate",
        )

    hours = float(cfg.get("forecast_window_hours", 4.0))
    rate = float(discharges) / hours
    if rate < _MIN_RATE:
        return NextRelease(
            None, None, window_minutes, rate,
            f"expected_discharges_4h = {discharges:g} — no release expected in the forecast",
        )

    mean_wait_hours = 1.0 / rate
    window_hours = window_minutes / 60.0
    probability = 1.0 - math.exp(-rate * window_hours)

    return NextRelease(
        expected_at=at + timedelta(hours=mean_wait_hours),
        probability=min(1.0, max(0.0, probability)),
        window_minutes=window_minutes,
        rate_per_hour=rate,
        basis=(
            f"DERIVED, not forecast: Poisson at {rate:.3f}/h from expected_discharges_4h="
            f"{discharges:g}. Real releases cluster, so this over-states short-window "
            "probability. No discharge-timing model exists."
        ),
    )
