"""Agent priority budget — how much negotiating capacity a department may spend this shift.

::

    Patient Utility  =  how valuable this bed is for this patient
    Priority Budget  =  how much negotiating capacity this department may spend this shift

Budget derivation is based on bid pressure, scarcity and fairness constraints, and the
current shift state. :func:`~allocation.budget.ledger.burn_band` is the guardrail that
keeps the spend within a healthy working band.
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
