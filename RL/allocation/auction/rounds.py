"""One bidding round. RL-Steps sections 9-17.

**Agents act in descending order of standing bid, and a later actor sees an earlier actor's
raise.** That is not a detail — it is what makes the worked example come out::

    round 2   ER 85 first  -> 101        section 13
              OT 75 next, sees 101       section 14: "Highest bid = 101"  -> 105
              Ward 55 last, ceiling 76   section 12: cannot win           -> withdraw

    round 3   OT 105 first, ceiling 94   section 16: no longer rational   -> withdraw
              ER 101 next, now unopposed section 17: closes               -> 118

With any other ordering the auction produces different numbers. Leader-first is also the
natural reading of an ascending multi-round auction: the standing leader commits, the others
respond.

**Rounds do not stop early when one bidder remains.** Section 17 has ER alone and still
raising from 101 to 118 against the reserve. Ending the auction the moment competition
disappears would hand a scarce bed over at the opening bid.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

from allocation.auction.guards import apply_guards
from allocation.auction.state import ExitReason, Position, standing_bids
from allocation.config import Config
from allocation.contracts import (
    Action,
    AgentKind,
    Bid,
    BiddingPolicy,
    BudgetState,
    Candidate,
    Decision,
    FeatureSnapshot,
    PathwayOptions,
    QAction,
    RoundState,
    UtilityBreakdown,
)


def decision_order(positions: Mapping[AgentKind, Position]) -> tuple[AgentKind, ...]:
    """Active agents, highest standing bid first, ties broken by agent name for determinism."""
    active = [(a, p) for a, p in positions.items() if p.active]
    active.sort(key=lambda item: (-item[1].current_bid, item[0].value))
    return tuple(agent for agent, _ in active)


def run_round(
    config: Config,
    *,
    auction_id: str,
    round_index: int,
    opened_at: datetime,
    positions: dict[AgentKind, Position],
    candidates: Mapping[AgentKind, Candidate],
    utilities: Mapping[str, UtilityBreakdown],
    ceilings: Mapping[str, float],
    budgets: Mapping[AgentKind, BudgetState],
    snapshot: FeatureSnapshot,
    policy: BiddingPolicy,
    contention: float,
    policy_name: str,
    pathways: Mapping[AgentKind, PathwayOptions] | None = None,
) -> tuple[RoundState, dict[AgentKind, Position]]:
    """Run one round, mutating a copy of ``positions``.

    Utilities are re-read every round. Expiry means *recalculate*, not re-award — a utility
    generated at 13:00 must not be trusted at 13:05, and section 15 has ER rising 148 -> 171
    and OT falling 112 -> 94 inside two minutes.

    ``pathways`` carries what the strategic exits need, one entry per agent, built once per
    round by :func:`~allocation.pathway.options.build_options`. Omitted, a policy can still
    run — every exit is then recorded as ``WITHDRAW_UNPLANNED``, which is the honest label
    for a withdrawal taken with nothing known about alternatives or releases.
    """
    working = dict(positions)
    recorded: list[Bid] = []

    # Refresh every position's utility and ceiling before anyone acts, so the ordering below
    # reflects this round's world rather than the previous one's.
    for agent, position in working.items():
        breakdown = utilities.get(position.candidate_id)
        if breakdown is None:
            continue
        working[agent] = position.with_bid(
            position.current_bid, breakdown.total, ceilings[position.candidate_id]
        )

    # Round 1 is sealed: every agent decides against the state at round start, seeing no
    # competition at all. From round 2 the view is live, so a later actor sees an earlier
    # actor's raise within the same round. Section 14 requires the second; section 10
    # requires the first.
    sealed = bool(config.auction["rounds"].get("sealed_first_round", True)) and round_index == 0
    at_round_start = dict(working)

    for agent in decision_order(working):
        position = working[agent]
        candidate = candidates[agent]
        breakdown = utilities[position.candidate_id]
        ceiling = ceilings[position.candidate_id]
        view = standing_bids(
            at_round_start if sealed else working, auction_id, round_index, opened_at
        )

        decision = _decide(
            policy, candidate, breakdown, ceiling, view, budgets[agent], snapshot,
            pathways.get(agent) if pathways else None,
        )
        action, alpha = decision.action, decision.alpha

        if action is Action.WITHDRAW:
            working[agent] = position.withdrawn(
                round_index, _exit_reason(position, ceiling, view, decision)
            )
            recorded.append(
                _bid_row(auction_id, round_index, position, Action.WITHDRAW,
                         position.current_bid, breakdown, ceiling, alpha, contention,
                         policy_name, decision)
            )
            continue

        if action is Action.HOLD or alpha is None:
            recorded.append(
                _bid_row(auction_id, round_index, position, Action.HOLD,
                         position.current_bid, breakdown, ceiling, alpha, contention,
                         policy_name, decision)
            )
            continue

        # Section 6: the policy never emits a point value.
        increment = alpha * (ceiling - position.current_bid)
        guarded = apply_guards(
            config,
            proposed=position.current_bid + increment,
            ceiling=ceiling,
            remaining_budget=budgets[agent].budget_remaining,
            contention=contention,
        )
        # Whole points, quantised inside apply_guards so the clamp is always the last word.
        # Rounding here instead would lift a bid sitting exactly on a fractional ceiling back
        # above it — the guard would pass and the invariant would still break.
        amount = guarded.amount

        working[agent] = position.with_bid(amount, breakdown.total, ceiling)
        recorded.append(
            _bid_row(auction_id, round_index, working[agent], Action.INCREASE_BID,
                     amount, breakdown, ceiling, alpha, contention, policy_name, decision)
        )

    return (
        RoundState(
            auction_id=auction_id,
            round_index=round_index,
            opened_at=opened_at,
            bids=tuple(recorded),
            active_agents=frozenset(a for a, p in working.items() if p.active),
        ),
        working,
    )


def _decide(
    policy: BiddingPolicy,
    candidate: Candidate,
    breakdown: UtilityBreakdown,
    ceiling: float,
    view: RoundState,
    budget: BudgetState,
    snapshot: FeatureSnapshot,
    options: PathwayOptions | None,
) -> Decision:
    """Ask the policy, through the widest seam it implements.

    A policy defining ``decide_q`` gets the pathway options and can choose any of the six
    actions. One defining only ``decide`` is adapted: its ``WITHDRAW`` becomes
    ``WITHDRAW_UNPLANNED``, because a policy that cannot express an onward plan did not make
    one, and ``INCREASE_BID``/``HOLD`` become ``CONTINUE``.

    **The adaptation never invents a strategic exit.** Promoting a bare withdrawal to
    ``WITHDRAW_ALTERNATIVE`` just because an alternative happened to be free would credit the
    policy with a decision it has no way to take — the exact defect the Q-space was added to
    remove.
    """
    decide_q = getattr(policy, "decide_q", None)
    if decide_q is not None:
        return decide_q(candidate, breakdown, ceiling, view, budget, snapshot, options)

    action, alpha = policy.decide(candidate, breakdown, ceiling, view, budget, snapshot)
    if action is Action.WITHDRAW:
        return Decision(q_action=QAction.WITHDRAW_UNPLANNED, action=Action.WITHDRAW)
    return Decision.compete(QAction.CONTINUE, action, alpha)


def _exit_reason(
    position: Position, ceiling: float, view: RoundState, decision: Decision | None = None
) -> str:
    """Why this agent left, preferring what the policy said over what can be inferred.

    The mechanical reasons remain first because they are facts about the auction: a bid above
    its ceiling is irrational whatever the policy calls it. Below them, a strategic exit now
    records *what was arranged* rather than the undifferentiated "policy withdrew" — which is
    what made a safe hand-off and an abandonment identical in the log.
    """
    if position.current_bid > ceiling:
        return ExitReason.BID_ABOVE_CEILING
    rivals = [
        bid.amount
        for bid in view.bids
        if bid.agent is not position.agent and bid.action is not Action.WITHDRAW
    ]
    if rivals and ceiling <= max(rivals):
        return ExitReason.CANNOT_WIN
    if decision is not None:
        return ExitReason.for_action(decision.q_action)
    return ExitReason.POLICY


def _bid_row(
    auction_id: str,
    round_index: int,
    position: Position,
    action: Action,
    amount: float,
    breakdown: UtilityBreakdown,
    ceiling: float,
    alpha: float | None,
    contention: float,
    policy_name: str,
    decision: Decision | None = None,
) -> Bid:
    del policy_name  # carried on the audit row, not on the in-memory bid
    return Bid(
        auction_id=auction_id,
        round_index=round_index,
        agent=position.agent,
        candidate_id=position.candidate_id,
        action=action,
        amount=amount,
        utility=breakdown.total,
        ceiling=ceiling,
        alpha=alpha,
        contention=contention,
        q_action=decision.q_action if decision else None,
        plan=decision.plan if decision else None,
        q_values=dict(decision.q_values) if decision else {},
        feasible=decision.feasible if decision else frozenset(),
    )


def leader(positions: Mapping[AgentKind, Position]) -> AgentKind | None:
    """The active agent holding the highest standing bid, or ``None`` if nobody is left."""
    active = [(a, p) for a, p in positions.items() if p.active and p.current_bid > 0]
    if not active:
        return None
    return max(active, key=lambda item: (item[1].current_bid, -ord(item[0].value[0])))[0]


def all_withdrawn(positions: Mapping[AgentKind, Position]) -> bool:
    return not any(p.active for p in positions.values())


def initial_positions(candidates: Sequence[Candidate]) -> dict[AgentKind, Position]:
    return {
        candidate.agent: Position(agent=candidate.agent, candidate_id=candidate.candidate_id)
        for candidate in candidates
    }
