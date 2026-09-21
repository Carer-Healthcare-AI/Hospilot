"""Assembling shift-level episodes. RL-Steps section 21.

*"The agent does not maximize R(current auction). It tries to maximize the sum of discounted
rewards over 8 hours. So it optimizes the whole shift, not just this one bed."*

That single sentence is what makes the budget mean anything, and it is also what makes this
harder than a bandit problem. **The episode is the shift, not the auction.** An agent that bids
hard at 09:00 and has nothing left at 17:00 for a trauma arrival made a bad decision at 09:00,
and only a shift-length episode can express that.

Two consequences:

* Roughly six auctions per shift per department, each scored four hours after it closed. A
  shift's episode is not complete until well after the shift has ended.
* An episode containing **any** incomplete auction is itself incomplete. Silently dropping the
  unscored steps would tell the policy that a shift with an unobserved death went fine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from allocation.audit.records import OutcomeRow
from allocation.config import Config
from allocation.contracts import AgentKind
from allocation.reward.terms import discount_gamma

#: Minutes of clinical time that one unit of ``gamma`` is charged over. Ported verbatim from
#: ``critical_care_equipment``/``diagnostic_machine``'s own ``DISCOUNT_HORIZON_MINUTES`` (Block
#: 3.1): a discount must be raised over elapsed clock time, never over how many times the
#: policy was asked to decide — the decision count is something the policy itself controls.
DISCOUNT_HORIZON_MINUTES = 60.0


def minutes_between(earlier: datetime, later: datetime) -> float:
    """Clinical minutes from ``earlier`` to ``later``, floored at zero."""
    return max((later - earlier).total_seconds() / 60.0, 0.0)


def step_discount(
    gamma: float, elapsed_minutes: float, horizon: float = DISCOUNT_HORIZON_MINUTES
) -> float:
    """``gamma`` charged over ``elapsed_minutes`` of clinical time.

    ``step_discount(g, 0.0) == 1.0`` — two decisions at the same instant are the same moment.
    ``step_discount(g, horizon) == g``.
    """
    if horizon <= 0:
        raise ValueError("the discount horizon is a duration and must be positive")
    return float(gamma ** (max(elapsed_minutes, 0.0) / horizon))


@dataclass(frozen=True, slots=True)
class Step:
    """One auction, from one agent's point of view."""

    auction_id: str
    round_count: int
    won: bool
    bid: float
    utility: float
    cost: float
    reward: float
    complete: bool
    #: When this auction opened and closed — carried so :func:`link_steps` can compute
    #: :attr:`elapsed_minutes` without a second pass over the simulator's records. ``None`` for
    #: a hand-built ``Step`` (tests): :attr:`elapsed_minutes` then keeps its own default.
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    #: Clinical minutes from this auction to whatever comes next for this agent in this shift:
    #: the next auction's opening, or — on the shift's last auction — this auction's own close.
    #: Defaults to :data:`DISCOUNT_HORIZON_MINUTES` (one full discount unit), matching this
    #: field's absence before Block 3.1 fixed the decision-index discount bug, so an episode
    #: built by hand (every existing test) keeps its prior discounted value unchanged.
    elapsed_minutes: float = DISCOUNT_HORIZON_MINUTES


@dataclass(frozen=True, slots=True)
class Episode:
    """One agent, one shift."""

    agent: AgentKind
    shift_id: str
    steps: tuple[Step, ...]
    gamma: float

    @property
    def complete(self) -> bool:
        """A single unscored auction makes the whole episode unusable for training."""
        return bool(self.steps) and all(step.complete for step in self.steps)

    @property
    def total_reward(self) -> float:
        return sum(step.reward for step in self.steps)

    @property
    def discounted_return(self) -> float:
        """The quantity section 21 says the agent maximises, discounted over CLINICAL MINUTES
        between auctions rather than over how many auctions occurred (Block 3.1) — see
        :func:`step_discount`. Reduces to the original ``sum(gamma^t * R_t)`` whenever every
        step's ``elapsed_minutes`` is the default one discount horizon apart, which is exactly
        what a hand-built ``Step`` (every episode test predating this fix) still is."""
        running = 0.0
        for step in reversed(self.steps):
            running = step.reward + step_discount(self.gamma, step.elapsed_minutes) * running
        return running

    @property
    def wins(self) -> int:
        return sum(1 for step in self.steps if step.won)

    @property
    def spend(self) -> float:
        return sum(step.cost for step in self.steps)

    def burn_rate(self, budget_total: float) -> float:
        """Realised burn for this shift — the health metric of the whole mechanism."""
        return self.spend / budget_total if budget_total > 0 else 0.0


def link_steps(steps: Sequence[Step]) -> tuple[Step, ...]:
    """Fill in :attr:`Step.elapsed_minutes` from real auction timestamps, in place of the
    default. ``steps`` must already be in the chronological order this agent faced them —
    :func:`allocation.sim.dataset.generate` visits auctions in real time order, so the list it
    builds per (agent, shift) already is.

    A step with no ``opened_at`` (a hand-built ``Step``, i.e. every test that predates Block
    3.1) is left untouched: it keeps :data:`DISCOUNT_HORIZON_MINUTES`, exactly what an absent
    field meant before this fix existed.
    """
    from dataclasses import replace

    linked = list(steps)
    for index, step in enumerate(steps):
        if step.opened_at is None:
            continue
        if index + 1 < len(steps) and steps[index + 1].opened_at is not None:
            gap = minutes_between(step.opened_at, steps[index + 1].opened_at)
        elif step.closed_at is not None:
            gap = minutes_between(step.opened_at, step.closed_at)
        else:
            continue
        linked[index] = replace(step, elapsed_minutes=gap)
    return tuple(linked)


def build_episode(
    config: Config,
    agent: AgentKind,
    shift_id: str,
    steps: Sequence[Step],
) -> Episode:
    return Episode(
        agent=agent, shift_id=shift_id, steps=link_steps(steps), gamma=discount_gamma(config)
    )


def step_from(
    outcome_row: OutcomeRow,
    *,
    auction_id: str,
    round_count: int,
    won: bool,
    bid: float,
    utility: float,
    cost: float,
) -> Step:
    return Step(
        auction_id=auction_id,
        round_count=round_count,
        won=won,
        bid=bid,
        utility=utility,
        cost=cost,
        reward=outcome_row.reward_total,
        complete=outcome_row.complete,
    )


def trainable(episodes: Sequence[Episode]) -> tuple[Episode, ...]:
    """Only complete episodes.

    Today this returns nothing, because ``no_mortality`` has no source (F-01) and so every
    episode is incomplete. **That is the correct output**, and it is the clearest possible
    statement of what F-01 costs: not a degraded training set, an empty one.
    """
    return tuple(e for e in episodes if e.complete)
