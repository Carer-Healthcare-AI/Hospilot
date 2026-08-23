"""Row shapes, one per table in migration 091.

These are the only types that touch the database, and they are deliberately flat: a change to
a column is a change here and nowhere else.

**Everything that can only be captured now is captured now.** Four things in the framework
wait on this log and none of them backfills:

    B.10  Expected ICU benefit   needs patients who were DENIED a bed
    B.11  Criticality            needs request timestamps
    B.12  Fairness v2/v3         needs win/loss history weighted by utility forgone
    B.13  Cap fitting            needs contested cases with PER-COMPONENT values

Store only the winner, or only the utility total, and all four stay blocked permanently. That
is why :class:`BidRow` carries the component breakdown and the coverage fractions, and why a
row is written for every agent in every round including withdrawals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class AuctionRow:
    """``allocation.auction`` — one contested resource release."""

    id: str
    auction_key: str
    resource_type: str
    resource_id: str
    mode: str
    trigger_source: str
    predicted_free_at: datetime
    opened_at: datetime
    closed_at: datetime | None
    max_rounds: int
    rounds_run: int
    reserve_price: float
    winning_agent: str | None
    winning_candidate_id: str | None
    winning_bid: float | None
    outcome: str | None
    caps_version: str
    config_version: str
    unsigned_rules: Mapping[str, str] = field(default_factory=dict)
    # Every eligible bidder and the patient it was bidding for, agent -> candidate_id.
    #
    # Not derivable from the bid rows, and B.10 is the reason: it needs patients who were
    # DENIED a bed. A candidate that was eligible but never bid is a denial, and without this
    # column it leaves no trace at all. It is also what lets the validator prove that no
    # participant was dropped from the log rather than merely that the rows are self-
    # consistent.
    participants: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BidRow:
    """``allocation.auction_bid`` — one agent, one round, winners and losers alike.

    *"Both episodes are needed, which is why the log must record the losers' bids and
    utilities, not only the winner's."*
    """

    auction_id: str
    round_index: int
    agent: str
    candidate_id: str
    patient_token: str | None
    action: str
    amount: float
    utility: float
    ceiling: float
    alpha: float | None
    contention: float | None
    outcome_factor: float | None
    cost: float | None
    component_points: Mapping[str, float]
    component_coverage: Mapping[str, float]
    policy_name: str
    decided_at: datetime
    #: Which of the six decisions produced this row (``QAction``), not merely which bid
    #: mechanic. ``action`` alone cannot separate a patient moved safely to HDU, a patient
    #: waiting on a bed due in twenty minutes, and a patient abandoned — all three are
    #: ``withdraw``. Without this column ``safely_held`` (+10) and ``second_bed_opened``
    #: (+15) attach to whichever agent happened to bid, so the reward is unattributable and
    #: a policy trained on it learns to credit a bid for a pathway decision.
    q_action: str | None = None
    #: What the exit committed to, flattened: target unit, safe-hold minutes, expected
    #: release and its probability, and the re-entry condition. Flat because every other row
    #: here is, and because the reward observer reads it back column-wise.
    plan: Mapping[str, Any] = field(default_factory=dict)
    #: Estimated value per action considered, ``{q_action: value}``. Section 12 publishes
    #: both sides — ``Q(Continue) = 41`` against ``Q(Withdraw) = 58`` — and the losing
    #: estimate is the label a value function trains on. Storing only the argmax discards it.
    #: Empty for a rule-based policy, which ranks nothing and must not appear to.
    q_values: Mapping[str, float] = field(default_factory=dict)
    #: Actions that were available at all. An evaluation that cannot separate *declined* from
    #: *unavailable* reads a policy that never had an alternative as one that never wanted one.
    feasible_actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BudgetRow:
    """``allocation.agent_budget`` — one agent, one shift, all four factors retained.

    Storing the product alone makes a budget unauditable and impossible to re-derive after a
    cap change. The Base inputs are here for the same reason.
    """

    agent: str
    shift_id: str
    shift_start: datetime
    shift_end: datetime
    base: float
    demand_factor: float
    criticality_factor: float
    fairness_factor: float
    scarcity_factor: float
    #: Where each factor came from. A factor of 1.00 that was *computed* and one that *fell
    #: back* are the same number and completely different facts — today Demand and Fairness
    #: are both fallbacks and only Scarcity is a live measurement.
    factor_sources: dict[str, str]
    budget_total: float
    budget_remaining: float
    spent_this_shift: float
    recovered_this_shift: float
    source: str
    n_win: int | None
    n_req: int | None
    cost_per_win: float | None
    cost_per_loss: float | None
    caps_version: str
    config_version: str


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    """``allocation.utility_snapshot`` — the inputs behind one round's utilities.

    Without this a stored score cannot be re-derived, and a score that cannot be re-derived is
    useless to cap fitting.
    """

    auction_id: str
    round_index: int
    taken_at: datetime
    hospital_state: Mapping[str, Any]
    patient_data: Mapping[str, Any]
    factor_signals: Mapping[str, Any]
    caps_version: str
    config_version: str


@dataclass(frozen=True, slots=True)
class OutcomeRow:
    """``allocation.auction_outcome`` — the reward, observed hours later.

    ``mortality_observed`` is **tri-state**: ``True``, ``False``, or ``None`` meaning *not
    known*. There is no mortality field anywhere in the hospilot schema (F-01), so today it is
    always ``None`` — and ``None`` must never be read as "no death occurred". It is the
    largest single reward term (+30 / -60) and it sets the sign of the episode.
    """

    auction_id: str
    observed_at: datetime
    horizon_hours: float
    terms: Mapping[str, float]
    reward_total: float
    mortality_observed: bool | None
    mortality_source: str | None
    complete: bool
    missing_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuditBundle:
    """Every row produced by one auction. Written atomically or not at all.

    Partial writes are worse than no write: a bid row whose budget decrement never landed
    describes an auction that did not happen.
    """

    auction: AuctionRow
    bids: tuple[BidRow, ...]
    budgets: tuple[BudgetRow, ...]
    snapshots: tuple[SnapshotRow, ...]

    @property
    def auction_id(self) -> str:
        return self.auction.id

    @property
    def row_count(self) -> int:
        return 1 + len(self.bids) + len(self.budgets) + len(self.snapshots)
