"""The diagnostic state encoder.

One immutable read of everything a policy may see at a decision point, as a fixed-length
vector of floats in ``[0, 1]``.

**Every feature here is scaled at the point of construction, and every one is checked.** The
bed encoder shipped with five constant columns and one feature arriving across 1/53 of its
declared range, and neither was visible until someone went looking — a dead column costs the
learner nothing but silently shrinks the state, and a mis-scaled one starves a linear model of
gradient in the direction that matters. ``tests/test_diagnostic_encoder.py`` sweeps the
scenarios and fails on any column that never moves or that leaves its band, so the same defect
cannot arrive here unnoticed.

**The version hash is a safety interlock, not bookkeeping.** Weights fitted under one feature
layout are meaningless under another, and the failure is silent — the vector is still the
right length and the numbers are still finite. :func:`encoder_version` hashes the feature
names, and the Q-policy refuses to load weights whose recorded version differs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from allocation.contracts import Action, AgentKind, BudgetState, RoundState
from allocation.features.scale import clamp
from allocation.use_cases.diagnostic_machine.policy import DiagnosticOptions
from allocation.use_cases.diagnostic_machine.utility import (
    DiagnosticComponent,
    DiagnosticUtility,
)

#: The feature layout. Order is load-bearing — it is what the version hash covers.
FEATURES: tuple[str, ...] = (
    # -- the request's own value ---------------------------------------------------------
    "diagnostic_value",
    "urgency",
    "delay_pressure",
    "operational_impact",
    "machine_scarcity",
    "alternative_quality",
    "transport_burden",
    "resource_stress",
    # -- where this bid stands -----------------------------------------------------------
    "utility_scaled",
    "ceiling_scaled",
    "own_bid_over_ceiling",
    "leader_bid_over_ceiling",
    "is_leading",
    "behind_by_scaled",
    "headroom_fraction",
    # -- the auction ---------------------------------------------------------------------
    "rounds_left",
    "n_bidders_scaled",
    "contention_scaled",
    # -- what the agent can afford -------------------------------------------------------
    "budget_remaining_fraction",
    "burn_rate",
    # -- what the exits are worth --------------------------------------------------------
    "safe_wait_fraction",
    "next_capacity_slack",
    "has_alternative",
    "alternative_yield_ratio",
    # -- agent-extension (2026-08-27): APPOINTMENTS' declared reschedulability -----------
    # Appended, not inserted, so every existing feature keeps its index.
    "is_scheduled_demand",
)

#: Utilities and ceilings are unbounded in principle. This is the divisor that brings them
#: into [0, 1]; it is the sum of the positive diagnostic caps, so a request scoring every
#: positive component at its maximum lands at 1.0. Chosen, not fitted — but it is a *scale*,
#: and the test sweep checks the range is actually used.
UTILITY_SCALE = 170.0

#: Contention is clamped to [0.8, 1.3] by the shared formula.
CONTENTION_LO, CONTENTION_SPAN = 0.8, 0.5


def encoder_version() -> str:
    """Short hash of the feature layout. Changes whenever a column is added or reordered."""
    return hashlib.sha256("|".join(FEATURES).encode()).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class EncodedState:
    """One encoded decision point, with the layout it was built under."""

    values: tuple[float, ...]
    version: str

    def __post_init__(self) -> None:
        if len(self.values) != len(FEATURES):
            raise ValueError(
                f"expected {len(FEATURES)} features, got {len(self.values)}"
            )

    def named(self) -> dict[str, float]:
        return dict(zip(FEATURES, self.values))


def encode(
    utility: DiagnosticUtility,
    ceiling: float,
    round_state: RoundState,
    budget: BudgetState,
    options: DiagnosticOptions,
    agent: AgentKind,
    max_rounds: int,
    contention: float,
    now: datetime,
) -> EncodedState:
    """Encode one decision point.

    Called at the moment the policy is asked, against the same view the policy receives — not
    reconstructed afterwards from a closed auction. That distinction is the bed system's most
    expensive encoder bug: re-encoding from the result paired each action with a state that
    already contained its consequence, and the learner fit values on states it would never see
    at serving time.
    """
    scores = utility.scores
    mine = _standing(round_state, agent)
    leader = _leader(round_state, agent)
    safe_wait = options.safe_wait_minutes
    window = max(options.request.safe_delay.total_seconds() / 60.0, 1e-9)

    alternative = options.best_alternative()
    requested_yield = options.request.yield_for(options.profile.modality)

    # No `has_next_capacity` column. It would be constant 1.0 by construction — the auction
    # only admits requests that have a start at all — and a column that never moves is a
    # column the learner pays for and gets nothing from. Absence is encoded as slack 0.0,
    # which is also the correct reading: no capacity means no slack.
    next_at = options.next_capacity_at
    if next_at is None:
        slack = 0.0
    else:
        remaining = (options.request.latest_useful_at - next_at).total_seconds() / 60.0
        slack = clamp(remaining / window)

    values = (
        scores[DiagnosticComponent.DIAGNOSTIC_VALUE],
        scores[DiagnosticComponent.URGENCY],
        scores[DiagnosticComponent.DELAY_PRESSURE],
        scores[DiagnosticComponent.OPERATIONAL_IMPACT],
        scores[DiagnosticComponent.MACHINE_SCARCITY],
        scores[DiagnosticComponent.ALTERNATIVE_QUALITY],
        scores[DiagnosticComponent.TRANSPORT_BURDEN],
        scores[DiagnosticComponent.RESOURCE_STRESS],
        clamp(utility.total / UTILITY_SCALE),
        clamp(ceiling / UTILITY_SCALE),
        clamp(mine / ceiling) if ceiling > 0 else 0.0,
        clamp(leader / ceiling) if ceiling > 0 else 0.0,
        1.0 if mine >= leader and mine > 0 else 0.0,
        clamp((leader - mine) / ceiling) if ceiling > 0 else 0.0,
        clamp((ceiling - mine) / ceiling) if ceiling > 0 else 0.0,
        clamp((max_rounds - round_state.round_index) / max(max_rounds, 1)),
        clamp(len(round_state.active_agents) / 4.0),
        clamp((contention - CONTENTION_LO) / CONTENTION_SPAN),
        clamp(
            budget.budget_remaining / budget.budget_total if budget.budget_total > 0 else 0.0
        ),
        clamp(budget.burn_rate),
        clamp(safe_wait / window),
        slack,
        1.0 if alternative is not None else 0.0,
        clamp(alternative[1] / requested_yield)
        if alternative is not None and requested_yield > 0
        else 0.0,
        1.0 if options.request.reschedulable else 0.0,
    )
    return EncodedState(values=values, version=encoder_version())


def _standing(round_state: RoundState, agent: AgentKind) -> float:
    for bid in round_state.bids:
        if bid.agent is agent:
            return 0.0 if bid.action is Action.WITHDRAW else bid.amount
    return 0.0


def _leader(round_state: RoundState, agent: AgentKind) -> float:
    rivals = [
        bid.amount
        for bid in round_state.bids
        if bid.agent is not agent and bid.action is not Action.WITHDRAW
    ]
    return max(rivals, default=0.0)


def describe(states: Sequence[EncodedState]) -> dict[str, tuple[float, float]]:
    """Per-feature ``(min, max)`` over a set of states — the dead-column check.

    Returned rather than asserted so a caller can report the spread as well as test it.
    """
    if not states:
        return {}
    columns = list(zip(*(state.values for state in states)))
    return {name: (min(col), max(col)) for name, col in zip(FEATURES, columns)}


__all__ = ["FEATURES", "EncodedState", "describe", "encode", "encoder_version"]
