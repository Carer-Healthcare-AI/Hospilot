"""Accrual, settlement and renewal — the budget's lifecycle. AGENT_BUDGET sections 6, 8, 9.

::

    open_shift    B = Base x Demand x Fairness x Scarcity        seeded at Base on shift 0
    settle        remaining -= Bid x Contention x Outcome x Rate  once, at settle
    recover       remaining = min(B, remaining + B/8 x rho)       hourly
    burn_rate     spent / B                                       checked every shift

**Decrement happens once, at settle, in the same transaction that writes ``auction_bid``.**
A bid that is raised and then withdrawn costs nothing; a standing bid costs when the auction
closes. Charging per round would make a three-round auction three times as expensive as a
one-round auction for the same allocation.

**The ``min`` cap in renewal is what prevents hoarding.** Without it a quiet hour banks
capacity above the shift allowance, and an agent learns to lose deliberately to accumulate it.
The alternatives are worse in the other direction: a hard shift reset creates end-of-shift
dumping, because unspent budget is worthless at 07:59.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from allocation.budget.base import BaseBudget
from allocation.budget.factors import BudgetFactors
from allocation.budget.shifts import Shift
from allocation.budget.spend import SpendResult, compute_cost
from allocation.config import Config
from allocation.contracts import BudgetSource, BudgetState


def open_shift(
    config: Config,
    base: BaseBudget,
    factors: BudgetFactors,
    shift: Shift,
    previous: BudgetState | None = None,
) -> BudgetState:
    """Open a department's budget for a shift.

    ``previous is None`` marks the first shift, which is **seeded at Base** rather than at
    RL-Steps' declared 1000/800/700. Seeding with the declared figures and then letting this
    formula recompute would drop ER about 12x at the first boundary, with every learned
    bidding behaviour calibrated to the wrong scale.
    """
    total = base.base * factors.product
    return BudgetState(
        agent=base.agent,
        shift_id=shift.shift_id,
        shift_start=shift.start,
        shift_end=shift.end,
        base=base.base,
        demand=factors.demand,
        criticality=factors.criticality,
        fairness=factors.fairness,
        scarcity=factors.scarcity,
        budget_total=total,
        budget_remaining=total,
        source=BudgetSource.SEED if previous is None else BudgetSource.COMPUTED,
        caps_version=config.caps_version,
        config_version=config.config_version,
        factor_sources=factors.sources,
    )


def settle(
    config: Config, state: BudgetState, bid: float, contention_factor: float, won: bool
) -> tuple[BudgetState, SpendResult]:
    """Charge one settled auction against a budget.

    The remainder floors at zero. A department cannot go negative — the affordability
    constraint in ``spend.max_affordable_bid`` is what should have prevented an unaffordable
    bid, and a floor here means a mispriced settle degrades gracefully instead of producing a
    negative budget that the next shift's ``min`` cap would silently launder.
    """
    result = compute_cost(config, bid, contention_factor, won)
    return (
        replace(
            state,
            budget_remaining=max(0.0, state.budget_remaining - result.cost),
            spent=state.spent + result.cost,
        ),
        result,
    )


def recover(config: Config, state: BudgetState, hours: float) -> BudgetState:
    """Apply ``hours`` of hourly recovery, capped at the shift total.

    ``hourly_recovery = B / hours_per_shift x rho``, rho = 0.5, so a department that spends
    steadily drifts down while one that stops idle recovers. **rho is a guess and needs
    burn-rate data to tune** (BA5).
    """
    cfg = config.budget["renewal"]
    if hours < 0:
        raise ValueError("recovery hours cannot be negative")

    rate = state.budget_total / float(cfg["hours_per_shift"]) * float(cfg["rho"])
    credited = rate * hours
    ceiling = state.budget_total if bool(cfg.get("cap_at_total", True)) else float("inf")
    new_remaining = min(ceiling, state.budget_remaining + credited)

    return replace(
        state,
        budget_remaining=new_remaining,
        recovered=state.recovered + (new_remaining - state.budget_remaining),
    )


def advance_shift(
    config: Config,
    previous: BudgetState,
    base: BaseBudget,
    factors: BudgetFactors,
    shift: Shift,
) -> BudgetState:
    """Roll into the next shift, recomputing the factors.

    Base changes only when caps, ``N_win`` or ``N_req`` change; the factors are recomputed
    every shift. Note that until the forecast retention table has 30 days and the auction log
    has ~10 shifts, Demand and Fairness are both pinned at 1.0 — so **only the global Scarcity
    factor moves, and relative budgets between departments do not change at all** (F-19). That
    is the design working, not a bug.
    """
    return open_shift(config, base, factors, shift, previous=previous)


def burn_band(config: Config, burn_rate: float) -> str:
    """Classify a burn rate against AGENT_BUDGET section 8's health bands.

    Burn rate is the health metric for the whole mechanism. Below the inert threshold, bidding
    maximum is free and the RL will learn to do exactly that.

    **Evaluate this at the end of a shift, not mid-shift.** A department three auctions into an
    eight-hour shift is *supposed* to read low; classifying that as "inert" is a misreading of
    a partial number, not a finding.
    """
    bands = config.budget["burn_rate_bands"]
    if burn_rate < float(bands["inert_below"]):
        return "inert"
    if burn_rate > float(bands["starved_above"]):
        return "starved"
    lo, hi = (float(x) for x in bands["working"])
    return "working" if lo <= burn_rate <= hi else "marginal"
