"""Utility ceiling — D.9, RL-Steps section 8.2.

``Ceiling = U * (1 + expected_uplift_over_horizon)``

The ceiling is a bidder's maximum willingness to compete, and the framework is explicit that
it sits **above** current utility: Ward values the bed at 86 now but may bid up to 105,
because deterioration is expected to raise its value over the next two hours.

That uplift needs a forward projection of deterioration — B.9, which falls out of B.5 once
B.5 exists. Neither is built, so ``Ceiling = U`` today.

**Bid != utility != ceiling** is load-bearing, not a technicality: ER wins at 118 while
valuing the bed at 171 (section 18). Collapsing ceiling into utility now is safe; collapsing
bid into either is not.
"""

from __future__ import annotations

from dataclasses import dataclass

from allocation.contracts import UtilityBreakdown


@dataclass(frozen=True, slots=True)
class Ceiling:
    value: float
    uplift: float
    source: str
    note: str = ""


def ceiling_for(utility: UtilityBreakdown, uplift: float | None = None) -> Ceiling:
    """Ceiling for a scored utility.

    ``uplift`` is the fractional increase expected over the allocation horizon. Passing
    ``None`` — the only option today — yields ``Ceiling = U``, which is the framework's own
    stated fallback and is *conservative*: it can only make an agent bid less than the
    framework intends, never more.
    """
    total = utility.total
    if uplift is None:
        return Ceiling(
            value=total,
            uplift=0.0,
            source="fallback",
            note="B.9 projection not built; D.9 fallback Ceiling = U",
        )
    if uplift < 0:
        raise ValueError("uplift must be non-negative; a ceiling below current utility is not defined")
    return Ceiling(value=total * (1.0 + uplift), uplift=uplift, source="model.b9")
