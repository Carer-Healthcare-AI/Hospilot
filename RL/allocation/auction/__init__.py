"""The auction: round state machine, reserve, guards, settlement.

First price, hidden maxima, three rounds, agents see the leading bid but never each other's
ceilings. The layer exists to keep three quantities distinct::

    utility   what the bed is worth to this patient now      ER 171
    ceiling   maximum willingness to compete                 ER 171
    bid       what was actually exposed                      ER 118  <- wins

ER wins at 118 while valuing the bed at 171 — *"It does not need to spend all its willingness
to pay."* Collapse any two of those and the mechanism stops meaning anything.
"""

from allocation.auction.guards import GuardedBid, apply_guards, safety_violations
from allocation.auction.reserve import reserve_price
from allocation.auction.rounds import decision_order, initial_positions, run_round
from allocation.auction.runner import AuctionOutcome, round_budget, run_auction
from allocation.auction.settle import determine_winner, settle_auction
from allocation.auction.state import AuctionResult, ExitReason, Position, UtilitySource

__all__ = [
    "AuctionOutcome",
    "AuctionResult",
    "ExitReason",
    "GuardedBid",
    "Position",
    "UtilitySource",
    "apply_guards",
    "decision_order",
    "determine_winner",
    "initial_positions",
    "reserve_price",
    "round_budget",
    "run_auction",
    "run_round",
    "safety_violations",
    "settle_auction",
]
