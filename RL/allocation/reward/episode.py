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
from typing import Sequence

from allocation.audit.records import OutcomeRow
from allocation.config import Config
from allocation.contracts import AgentKind
from allocation.reward.terms import discount_gamma


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
        """``sum(gamma^t * R_t)`` — the quantity section 21 says the agent maximises."""
        return sum(self.gamma**t * step.reward for t, step in enumerate(self.steps))

    @property
    def wins(self) -> int:
        return sum(1 for step in self.steps if step.won)

    @property
    def spend(self) -> float:
        return sum(step.cost for step in self.steps)

    def burn_rate(self, budget_total: float) -> float:
        """Realised burn for this shift — the health metric of the whole mechanism."""
        return self.spend / budget_total if budget_total > 0 else 0.0


def build_episode(
    config: Config,
    agent: AgentKind,
    shift_id: str,
    steps: Sequence[Step],
) -> Episode:
    return Episode(
        agent=agent, shift_id=shift_id, steps=tuple(steps), gamma=discount_gamma(config)
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
