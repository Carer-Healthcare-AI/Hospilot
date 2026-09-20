"""Committed capacity — the one place a machine's fullness is computed.

There were three implementations of this arithmetic: one in ``scoring`` for the Machine
Scarcity component, one in ``auction`` feeding contention and the reserve price, and one in
``budgets`` feeding the scarcity factor. All three walked the same intervals and all three
could have been edited independently, which is how a run ends up reporting a scanner as 40 %
full to the bidder and 60 % full to the budget in the same auction.

They are consolidated here. The callers differ in *window* and in *which machines count*, and
those are parameters, not reasons to fork the arithmetic.

**This number is what stands in for ward occupancy** wherever a shared formula asks for one.
The substitution is exact in the only sense those formulas care about — both are "how full is
the resource, in [0, 1]" — and nothing else about a ward is implied.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from allocation.use_cases.diagnostic_machine.contracts import (
    DiagnosticMachineState,
    DiagnosticRequest,
)


def committed_fraction(
    machines: Sequence[DiagnosticMachineState],
    start: datetime,
    end: datetime,
    request: DiagnosticRequest | None = None,
) -> float | None:
    """Share of operating time already booked, over ``start``–``end``.

    ``request`` restricts the count to machines that could actually take it — a scanner that
    is down, or lacks the capability, is not capacity for that patient, and including it
    reports slack that does not exist.

    Returns ``None`` when there is no operating time to measure at all, and the distinction
    from ``1.0`` matters: ``None`` means *no reading*, which callers pass through to a
    documented fallback, while ``1.0`` means *measured, and full*. Collapsing the two would
    let a missing machine list read as a crisis.
    """
    counted = [m for m in machines if request is None or m.is_eligible(request)]
    if not counted:
        return None

    available = 0.0
    committed = 0.0
    for machine in counted:
        window_start = max(start, machine.window_starts_at)
        window_end = min(end, machine.window_ends_at)
        if window_end <= window_start:
            continue
        available += (window_end - window_start).total_seconds()
        for booking in machine.allocations:
            overlap_start = max(window_start, booking.starts_at)
            overlap_end = min(window_end, booking.ends_at)
            if overlap_end > overlap_start:
                committed += (overlap_end - overlap_start).total_seconds()

    if available <= 0:
        return None
    return min(1.0, committed / available)


def scarcity(
    machines: Sequence[DiagnosticMachineState],
    request: DiagnosticRequest,
    now: datetime,
    horizon: timedelta,
) -> float:
    """Committed fraction for the Machine Scarcity component, with no eligible machine as 1.0.

    The component needs a number in every state, and "nothing can take this patient" is
    maximally scarce — which is also the state in which bidding should be most aggressive. So
    the no-reading case resolves *here*, at the component, rather than inside the shared
    arithmetic where the other two callers would inherit it.
    """
    value = committed_fraction(machines, now, now + horizon, request=request)
    return 1.0 if value is None else value


def free_slots(
    machines: Sequence[DiagnosticMachineState],
    request: DiagnosticRequest,
    now: datetime,
    horizon: timedelta,
) -> float:
    """How many more requests of this size the modality could still take over the horizon.

    **This stands in for ``expected_discharges_4h``** in the contention formula, and it is the
    better-behaved of the two substitutions: a bed's figure is a forecast, this is arithmetic
    on a timetable. Both answer "how much relief is coming", which is all the formula does
    with the number.
    """
    window_end = now + horizon
    procedure = request.estimated_duration.total_seconds()
    if procedure <= 0:
        return 0.0

    free = 0.0
    for machine in machines:
        held = procedure + (machine.setup_minutes + machine.cleanup_minutes) * 60.0
        window_start = max(now, machine.window_starts_at)
        stop = min(window_end, machine.window_ends_at)
        if stop <= window_start:
            continue
        span = (stop - window_start).total_seconds()
        booked = sum(
            (min(stop, b.ends_at) - max(window_start, b.starts_at)).total_seconds()
            for b in machine.allocations
            if min(stop, b.ends_at) > max(window_start, b.starts_at)
        )
        free += max(0.0, span - booked) / held
    return free


__all__ = ["committed_fraction", "free_slots", "scarcity"]
