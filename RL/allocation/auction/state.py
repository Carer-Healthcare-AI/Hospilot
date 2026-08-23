"""Auction state. RL-Steps sections 7, 18.

First price, hidden maxima, three rounds, RL bidding from round 1. Agents **see the leading
bid** — section 14 has OT reasoning over a win-probability curve, which requires observing the
leader — but never each other's ceilings.

The distinction this whole layer exists to preserve::

    utility   what the bed is worth to this patient right now       ER 171
    ceiling   maximum willingness to compete                        ER 171 (= utility until B.9)
    bid       what was actually exposed                             ER 118  <- wins

ER wins at 118 while valuing the bed at 171. It never needs to spend its full willingness to
pay, and collapsing any two of those three would destroy the mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Mapping, Protocol

from allocation.contracts import (
    Action,
    AgentKind,
    AuctionMode,
    Bid,
    QAction,
    ResourceType,
    RoundState,
    UtilityBreakdown,
)


class ExitReason:
    """Why an agent stopped bidding. Recorded per position for the audit log.

    The first three are *mechanical* — facts about the auction, true whatever the policy
    intended. The rest are *strategic*, one per exit in :class:`~allocation.contracts.QAction`,
    and they are the reason this class grew: ``POLICY`` covered a patient moved safely to HDU,
    a patient waiting on a bed due in twenty minutes, and a patient abandoned with nothing
    arranged, all with the same four words.
    """

    BID_ABOVE_CEILING = "standing bid exceeds ceiling"
    CANNOT_WIN = "ceiling at or below the leading bid"
    UNAFFORDABLE = "budget cannot cover a competitive bid"
    POLICY = "policy withdrew"

    ALTERNATIVE = "withdrew to an alternative unit"
    AWAIT_NEXT = "withdrew to wait for a predicted release"
    RE_ENTER = "withdrew under a re-entry monitor"
    UNPLANNED = "withdrew with nothing arranged"

    @classmethod
    def for_action(cls, q_action: "QAction") -> str:
        return _STRATEGIC_EXITS.get(q_action, cls.POLICY)


_STRATEGIC_EXITS = {
    QAction.WITHDRAW_ALTERNATIVE: ExitReason.ALTERNATIVE,
    QAction.AWAIT_NEXT_RESOURCE: ExitReason.AWAIT_NEXT,
    QAction.RE_ENTER_LATER: ExitReason.RE_ENTER,
    QAction.WITHDRAW_UNPLANNED: ExitReason.UNPLANNED,
}


@dataclass(frozen=True, slots=True)
class Position:
    """One agent's standing position in an auction."""

    agent: AgentKind
    candidate_id: str
    current_bid: float = 0.0
    utility: float = 0.0
    ceiling: float = 0.0
    active: bool = True
    exit_round: int | None = None
    exit_reason: str = ""

    def with_bid(self, amount: float, utility: float, ceiling: float) -> Position:
        return replace(self, current_bid=amount, utility=utility, ceiling=ceiling)

    def withdrawn(self, round_index: int, reason: str) -> Position:
        return replace(self, active=False, exit_round=round_index, exit_reason=reason)


@dataclass(frozen=True, slots=True)
class AuctionResult:
    """A closed auction, with every round retained.

    ``rounds`` holds one :class:`RoundState` per round including losers and withdrawals —
    both a winning and a losing episode are needed for training, "which is why the log must
    record the losers' bids and utilities, not only the winner's" (sections 23-24).
    """

    auction_id: str
    auction_key: str
    resource_type: ResourceType
    resource_id: str
    mode: AuctionMode
    opened_at: datetime
    closed_at: datetime
    #: Rounds this auction was allowed, after the release event's lead time and the eligible
    #: utility TTLs were applied to the profile's cadence (``runner.round_budget``) — NOT the
    #: profile's ceiling. So ``rounds_run < max_rounds`` means it closed early: everyone
    #: withdrew, or a whole round moved nothing above the reserve.
    max_rounds: int
    reserve_price: float
    contention: float
    rounds: tuple[RoundState, ...]
    # Full component breakdown per round per candidate, keyed round -> candidate_id.
    # Carried on the result rather than re-derived at write time: B.13 cap fitting needs
    # per-component values on contested cases, and a breakdown recomputed later would be
    # against whatever the caps are *then*, which is exactly the number it cannot use.
    breakdowns: tuple[Mapping[str, UtilityBreakdown], ...]
    positions: Mapping[AgentKind, Position]
    winner: AgentKind | None
    winning_candidate_id: str | None
    winning_bid: float | None
    outcome: str
    caps_version: str
    config_version: str
    unsigned_rules: Mapping[str, str]

    @property
    def rounds_run(self) -> int:
        return len(self.rounds)

    def bids_for(self, agent: AgentKind) -> tuple[Bid, ...]:
        return tuple(
            bid for round_state in self.rounds for bid in round_state.bids if bid.agent is agent
        )

    def final_bid(self, agent: AgentKind) -> float:
        """The last standing bid, which is what settlement charges against.

        A withdrawal does not erase the exposure: section 19 charges OT on its 105 even
        though section 16 says the commitment "disappears". The participation charge is what
        stops endless meaningless auctions.
        """
        position = self.positions[agent]
        return position.current_bid


class UtilitySource(Protocol):
    """Supplies utilities and ceilings for a round.

    A seam, not a convenience: the auction does not care whether utilities came from vitals
    through the scoring engine or from a fixture. It also makes the worked example directly
    testable, since section 18 publishes utilities rather than the rows behind them.
    """

    def utilities(self, round_index: int) -> Mapping[str, UtilityBreakdown]: ...

    def ceilings(self, round_index: int) -> Mapping[str, float]: ...


def opening_round(agents: Mapping[AgentKind, Position]) -> RoundState:
    """A round-zero state with no bids — what the policy sees before anyone has acted."""
    return RoundState(
        auction_id="",
        round_index=0,
        opened_at=datetime.min,
        bids=(),
        active_agents=frozenset(a for a, p in agents.items() if p.active),
    )


def standing_bids(positions: Mapping[AgentKind, Position], auction_id: str, round_index: int,
                  opened_at: datetime) -> RoundState:
    """The view a policy is given: every agent's standing bid at this instant.

    Rebuilt before each agent decides, so a later bidder in the same round sees an earlier
    bidder's raise. Section 14 depends on this — OT reads ER's *new* 101, not its round-1 85.
    """
    bids = tuple(
        Bid(
            auction_id=auction_id,
            round_index=round_index,
            agent=position.agent,
            candidate_id=position.candidate_id,
            action=Action.HOLD if position.active else Action.WITHDRAW,
            amount=position.current_bid,
            utility=position.utility,
            ceiling=position.ceiling,
        )
        for position in positions.values()
    )
    return RoundState(
        auction_id=auction_id,
        round_index=round_index,
        opened_at=opened_at,
        bids=bids,
        active_agents=frozenset(a for a, p in positions.items() if p.active),
    )
