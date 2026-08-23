"""Opening, running and closing one auction. RL-Steps section 7.

**One entry point for every trigger.** A real bed-release event, a CDC row, and a hand-typed
query all become a :class:`ReleaseEvent` and arrive here. The query does not get its own
implementation — if it ever does, the thing being tested stops being the thing that runs.

``mode`` is what separates them. Only ``LIVE`` holds a bed and moves a budget; every mode is
scored and logged identically so a shadow run stays comparable to a real one.

**How many rounds run is decided per auction, not by the profile alone.** RL-Steps' three
rounds assume its own worked scenario — a bed thirty minutes out, utilities good for ten.
:func:`round_budget` re-derives the cap from the actual release event, and the loop closes
early on quiescence. Three constraints, whichever is tightest:

* ``profile.max_rounds`` — the framework's cadence, an upper bound
* the bed landing — a round closing after ``predicted_free_at`` allocates an occupied bed
* the shortest eligible utility TTL — past it the auction is pricing stale vitals

The floor is one round. A bed ninety seconds out still needs an owner, and one sealed round
beats no auction at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Protocol, Sequence

from allocation.auction.reserve import meets_reserve, reserve_price
from allocation.auction.rounds import initial_positions, run_round
from allocation.auction.settle import close, determine_winner, settle_auction
from allocation.auction.state import AuctionResult, Position, UtilitySource
from allocation.budget.spend import SpendResult, contention as compute_contention
from allocation.config import Config
from allocation.contracts import (
    AgentKind,
    BiddingPolicy,
    BudgetState,
    Candidate,
    FeatureSnapshot,
    PathwayOptions,
    ReleaseEvent,
)
from allocation.profiles.registry import ResourceProfile


class PathwaySource(Protocol):
    """Supplies pathway options per round, mirroring :class:`UtilitySource`.

    A seam for the same reason: the auction must not know whether an alternative unit's
    occupancy came from Hasura, a fixture or a simulator. Per round, not per auction, because
    availability moves inside an auction — that is the whole point of re-reading.
    """

    def options(self, round_index: int) -> Mapping[AgentKind, PathwayOptions]: ...


@dataclass(frozen=True, slots=True)
class AuctionOutcome:
    """Everything one auction produced. This is what the audit layer persists."""

    result: AuctionResult
    budgets: Mapping[AgentKind, BudgetState]
    spends: Mapping[AgentKind, SpendResult]


def round_budget(
    profile: ResourceProfile, event: ReleaseEvent, eligible: Sequence[Candidate]
) -> int:
    """How many rounds fit before the world invalidates the auction.

    ``profile.max_rounds`` is a ceiling, not a schedule — RL-Steps' three rounds assume a bed
    thirty minutes out. Two real constraints can only shrink it:

    * **The bed lands.** A round that closes after ``predicted_free_at`` allocates a bed that
      is already physically occupied.
    * **The data goes stale.** Utilities carry per-agent TTLs (ER's is 10 minutes), and the
      feature snapshot behind them ages from open. Re-scoring each round refreshes the
      arithmetic, not the vitals — an auction longer than the tightest eligible TTL is
      scoring clinical data nobody would trust.

    Never below one: a bed landing in ninety seconds still needs an owner, and a single
    sealed round beats no auction at all.
    """
    lead_seconds = (event.predicted_free_at - event.detected_at).total_seconds()
    ttl_seconds = min(profile.ttl_for(c.agent) for c in eligible) * 60.0
    by_deadline = int(lead_seconds // profile.round_seconds)
    by_ttl = int(ttl_seconds // profile.round_seconds)
    return max(1, min(profile.max_rounds, by_deadline, by_ttl))


def _quiescent(
    positions: Mapping[AgentKind, Position],
    before: Mapping[AgentKind, tuple[bool, float]],
) -> bool:
    """Did a whole round pass without a single bid moving or anyone exiting?

    Compared on ``(active, current_bid)`` only. Utility and ceiling are refreshed every round
    by construction, and a round in which they moved but no bid did is still a round that
    found no new price.
    """
    return {a: (p.active, p.current_bid) for a, p in positions.items()} == before


def _reserve_met_now(
    config: Config,
    positions: Mapping[AgentKind, Position],
    ceilings: Mapping[str, float],
    snapshot: FeatureSnapshot,
) -> bool:
    """Would the standing leader clear the reserve if the auction closed here?

    The guard that makes quiescence safe. Utilities are rescored every round, so an agent
    parked at its ceiling this round may be able to raise next round when that ceiling moves
    — section 15 has ER's rising 148 -> 171 inside two minutes. Closing a stalemate that sits
    *below* the reserve would record ``not_awarded`` and leave a scarce bed unallocated, when
    one more round could still have cleared it. Above the reserve there is nothing left to
    discover, and the bed goes to the leader either way.
    """
    standing = max((p.current_bid for p in positions.values() if p.active), default=0.0)
    reserve = reserve_price(
        config,
        highest_ceiling=max(ceilings.values(), default=0.0),
        occupancy=snapshot.hospital.occupancy,
    )
    return meets_reserve(standing, reserve)


def run_auction(
    config: Config,
    profile: ResourceProfile,
    event: ReleaseEvent,
    candidates: Sequence[Candidate],
    utility_source: UtilitySource,
    policy: BiddingPolicy,
    budgets: Mapping[AgentKind, BudgetState],
    snapshot: FeatureSnapshot,
    policy_name: str = "heuristic",
    charge_budgets: bool | None = None,
    pathways: "PathwaySource | None" = None,
) -> AuctionOutcome:
    """Run a complete auction to its close.

    ``charge_budgets`` overrides the mode gate on settlement — see :func:`settle_auction`.

    ``pathways`` supplies the strategic exits with what they need, re-read each round for the
    same reason utilities are: HDU can fill between round 1 and round 3, and an exit taken
    against round 1's availability would move a patient into a bed that is no longer there.
    """
    eligible = [c for c in candidates if profile.is_eligible(c.agent)]
    if not eligible:
        raise ValueError(
            f"no eligible bidders for {profile.resource_type.value}; "
            f"profile allows {[a.value for a in profile.eligible_agents]}"
        )

    positions = initial_positions(eligible)
    by_agent = {c.agent: c for c in eligible}

    # Contention is fixed at open, from the opening bidder count — see settle.py.
    contention = compute_contention(
        config,
        n_bidders=len(eligible),
        occupancy=snapshot.hospital.occupancy,
        expected_discharges_4h=snapshot.hospital.expected_discharges_4h,
    )

    auction_id = str(uuid.uuid4())
    rounds = []
    opened_at = event.detected_at
    budgeted_rounds = round_budget(profile, event, eligible)

    breakdowns: list[Mapping[str, object]] = []

    for round_index in range(budgeted_rounds):
        round_opened = opened_at + timedelta(seconds=profile.round_seconds * round_index)
        round_utilities = utility_source.utilities(round_index)
        breakdowns.append(round_utilities)
        before = {a: (p.active, p.current_bid) for a, p in positions.items()}
        state, positions = run_round(
            config,
            auction_id=auction_id,
            round_index=round_index,
            opened_at=round_opened,
            positions=positions,
            candidates=by_agent,
            utilities=round_utilities,
            ceilings=utility_source.ceilings(round_index),
            budgets=budgets,
            snapshot=snapshot,
            policy=policy,
            contention=contention,
            policy_name=policy_name,
            pathways=pathways.options(round_index) if pathways else None,
        )
        rounds.append(state)

        # Rounds do NOT stop early merely because one bidder is left — section 17 has ER
        # alone and still raising to meet the reserve. They stop when nobody is left, or on
        # quiescence: an ascending auction's going-going-gone.
        if not state.active_agents:
            break
        if _quiescent(positions, before) and _reserve_met_now(
            config, positions, utility_source.ceilings(round_index), snapshot
        ):
            break

    # The reserve is a CLOSING construct, evaluated against the ceilings current at close.
    # Section 17 introduces it only once ER is left competing — "the resource manager can
    # require a minimum final commitment" — and by then ER's ceiling has risen 135 -> 171.
    # Computing it from the opening ceilings would price the bed against a world two rounds
    # out of date, which is the same mistake as pricing contention at close.
    closing_ceilings = utility_source.ceilings(len(rounds) - 1)
    reserve = reserve_price(
        config,
        highest_ceiling=max(closing_ceilings.values(), default=0.0),
        occupancy=snapshot.hospital.occupancy,
    )

    closed_at = opened_at + timedelta(seconds=profile.round_seconds * len(rounds))
    winner, outcome = determine_winner(positions, reserve)

    result = AuctionResult(
        auction_id=auction_id,
        auction_key=event.auction_key(profile.auction_key_bucket_minutes),
        resource_type=event.resource_type,
        resource_id=event.resource_id,
        mode=event.mode,
        opened_at=opened_at,
        closed_at=closed_at,
        # What THIS auction was allowed, not the profile's ceiling — see round_budget. The
        # audit row carries detected_at and predicted_free_at, so the derivation stays
        # re-checkable, and `rounds_run < max_rounds` now means the auction closed early
        # rather than merely that the profile allows more than the clock did.
        max_rounds=budgeted_rounds,
        reserve_price=reserve,
        contention=contention,
        rounds=tuple(rounds),
        breakdowns=tuple(breakdowns),  # type: ignore[arg-type]
        positions=positions,
        winner=None,
        winning_candidate_id=None,
        winning_bid=None,
        outcome=outcome,
        caps_version=config.caps_version,
        config_version=config.config_version,
        unsigned_rules=dict(config.unsigned),
    )
    result = close(result, winner, outcome, closed_at)

    updated_budgets, spends = settle_auction(config, result, budgets, charge=charge_budgets)
    return AuctionOutcome(result=result, budgets=updated_budgets, spends=spends)


def opens_at(event: ReleaseEvent, lead_minutes: float) -> datetime:
    """When an auction for this release should open.

    The auction must finish before the bed lands: three rounds at two minutes plus the
    45-minute cleaning constant that every timing claim in the workflow inherits
    (``agents/bed/prediction_activities.py:16``, "not tracked in DB").
    """
    return event.predicted_free_at - timedelta(minutes=lead_minutes)
