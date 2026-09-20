"""Agent priority budget — how much negotiating capacity a department may spend this shift.

::

    Patient Utility  =  how valuable this bed is for this patient
    Priority Budget  =  how much negotiating capacity this department may spend this shift

Governed by ``AGENT_BUDGET.md`` v0.3, which supersedes ``RL_STEPS_END_TO_END.md`` section 4.
Base is derived from target win counts rather than declared, Criticality is dropped, Scarcity
is global, and Fairness is 1.0 until the auction log exists.

The whole mechanism is judged by one test (AGENT_BUDGET section 2)::

    A department that bids its ceiling on every case
    must run out before the shift ends.

If it does not, bidding maximum is free, the RL will learn to do exactly that, and the budget
is decoration. RL-Steps' own 1000/800/700 fails that test at roughly 8 % burn — a factor of
twelve. :func:`~allocation.budget.ledger.burn_band` is how it stays checked.
"""

from allocation.budget.base import BaseBudget, derive_all, derive_base
from allocation.budget.factors import BudgetFactors, compute_factors
from allocation.budget.ledger import advance_shift, burn_band, open_shift, recover, settle
from allocation.budget.shifts import Shift, resolve_shift
from allocation.budget.spend import (
    SpendResult,
    can_afford,
    compute_cost,
    contention,
    max_affordable_bid,
)

__all__ = [
    "BaseBudget",
    "BudgetFactors",
    "Shift",
    "SpendResult",
    "advance_shift",
    "burn_band",
    "can_afford",
    "compute_cost",
    "compute_factors",
    "contention",
    "derive_all",
    "derive_base",
    "max_affordable_bid",
    "open_shift",
    "recover",
    "resolve_shift",
    "settle",
]
