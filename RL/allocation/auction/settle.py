"""Closing an auction and charging the budgets. RL-Steps sections 18-19.

Three rules, each with a reason:

**The winner is the highest standing bid that clears the reserve.** First price — the winner
pays what it bid, not the runner-up's price.

**Losers pay a participation charge on their last standing bid.** Section 19 charges OT on its
105 even though section 16 says the commitment "disappears", and AGENT_BUDGET section 11
charges Ward 0.85 on a bid it never won with. The charge is small (outcome factor 0.1) and its
job is to stop endless meaningless auctions — an agent that bids into every contest at no cost
learns to do exactly that.

**Contention is fixed at open, not at close.** It prices how contested the resource was, and
by close the field has thinned by construction: section 18 ends with one bidder left, so
pricing at close would make the most contested auctions look the cheapest.

**Decrement happens once, here, in the transaction that writes the bid rows** — never per
round. Charging per round would make a three-round auction three times the price of a
one-round auction for the same allocation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from allocation.auction.reserve import meets_reserve
from allocation.auction.state import AuctionResult, Position
from allocation.budget.ledger import settle as settle_budget
from allocation.budget.spend import SpendResult
from allocation.config import Config
from allocation.contracts import AgentKind, AuctionMode, BudgetState

OUTCOME_AWARDED = "awarded"
OUTCOME_NO_AWARD = "no_award"
OUTCOME_ABORTED = "aborted"


def determine_winner(
    positions: Mapping[AgentKind, Position], reserve: float
) -> tuple[AgentKind | None, str]:
    """Highest standing bid among agents still active, if it clears the reserve."""
    contenders = [
        (agent, position)
        for agent, position in positions.items()
        if position.active and position.current_bid > 0
    ]
    if not contenders:
        return None, OUTCOME_NO_AWARD

    contenders.sort(key=lambda item: (-item[1].current_bid, item[0].value))
    agent, position = contenders[0]

    if not meets_reserve(position.current_bid, reserve):
        # Nobody was willing to meet the resource manager's minimum. The bed is not awarded
        # through the auction; the fallback pathway (safety layer, manual allocation) takes
        # over — which is precisely why the safety layer being undeclared matters (F-13).
        return None, OUTCOME_NO_AWARD

    return agent, OUTCOME_AWARDED


def settle_auction(
    config: Config,
    result: AuctionResult,
    budgets: Mapping[AgentKind, BudgetState],
    charge: bool | None = None,
) -> tuple[Mapping[AgentKind, BudgetState], Mapping[AgentKind, SpendResult]]:
    """Charge every participant and return the updated budgets.

    A non-binding auction (simulation, advisory, replay) is scored identically but does not
    move a budget — the numbers are still computed and logged so a shadow run is comparable to
    a live one.

    ``charge`` overrides that gate, and the question it answers is **"is this budget real?"**
    — not "is this auction live". A multi-shift session carries its own in-memory ledger, and
    a session whose budgets never move cannot show burn, recovery or exhaustion, which is the
    entire point of running one. Passing ``True`` against a budget loaded from
    ``allocation.agent_budget`` would corrupt it, so the default stays tied to the mode and
    the override has to be deliberate.
    """
    charge = result.mode.is_binding if charge is None else charge
    updated: dict[AgentKind, BudgetState] = {}
    spends: dict[AgentKind, SpendResult] = {}

    for agent, position in result.positions.items():
        won = agent is result.winner
        bid = position.current_bid

        if bid <= 0 and not won:
            updated[agent] = budgets[agent]
            continue

        new_state, spend = settle_budget(
            config, budgets[agent], bid=bid, contention_factor=result.contention, won=won
        )
        spends[agent] = spend
        updated[agent] = new_state if charge else budgets[agent]

    return updated, spends


def close(
    result: AuctionResult,
    winner: AgentKind | None,
    outcome: str,
    closed_at: datetime,
) -> AuctionResult:
    from dataclasses import replace

    winning_position = result.positions[winner] if winner is not None else None
    return replace(
        result,
        winner=winner,
        winning_candidate_id=winning_position.candidate_id if winning_position else None,
        winning_bid=winning_position.current_bid if winning_position else None,
        outcome=outcome,
        closed_at=closed_at,
    )


def holds_resource(mode: AuctionMode) -> bool:
    """Whether winning this auction actually takes the bed.

    Only a live auction calls ``hold_bed_temporarily``. A simulation that held real beds would
    be an outage, and an advisory run that did would be worse.
    """
    return mode.is_binding
