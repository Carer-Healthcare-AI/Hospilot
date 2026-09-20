"""The four bidding rules, as a diagnostic-local implementation.

**This is a deliberate re-implementation of arithmetic that also exists in
``allocation/policy/heuristic.py``, and the duplication is the point.**

An earlier version of this package extracted the rules into a shared ``policy/ladder.py`` and
had both families call it. That is the better factoring on its own terms, and it was reverted:
it edits ``HeuristicPolicy``, which is on the frozen bed execution path. The standing
instruction is that bed behaviour may not be touched, and that bed-coupled functionality gets
a diagnostic-specific equivalent inside this package instead. So the rules live here, and the
bed policy is untouched.

What that costs, stated plainly so nobody has to rediscover it: **the two families can now
drift apart on a bid.** If someone tunes the overtake margin in one place and not the other,
nothing fails — the two just start bidding differently for no stated reason.
:func:`allocation.use_cases.diagnostic_machine.ladder.climb` and ``HeuristicPolicy.decide_q``
are the pair to keep in step, and ``tests/test_diagnostic_ladder.py`` pins them against each
other so a divergence is a test failure rather than a surprise.

What is *not* duplicated: ``max_affordable_bid`` and ``clamp`` are imported from the shared
packages and called unmodified. Only the rule sequencing is local.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from allocation.budget.spend import max_affordable_bid
from allocation.config import Config
from allocation.contracts import Action, AgentKind, BudgetState, RoundState
from allocation.features.scale import clamp

EPS = 1e-9


class Verdict(str, Enum):
    """What the ladder concluded, before any resource vocabulary is applied."""

    EXIT = "exit"
    HOLD = "hold"
    COMPETE = "compete"


@dataclass(frozen=True, slots=True)
class LadderOutcome:
    verdict: Verdict
    alpha: float | None = None
    #: Why the ladder said leave. Carried into the exit's note so the log records the
    #: mechanical reason alongside whatever the caller arranged.
    reason: str = ""


def standing_bid(round_state: RoundState, agent: AgentKind) -> float:
    """This agent's own live bid. A withdrawal stands for nothing."""
    for bid in round_state.bids:
        if bid.agent is agent:
            return 0.0 if bid.action is Action.WITHDRAW else bid.amount
    return 0.0


def highest_rival(round_state: RoundState, agent: AgentKind) -> float:
    """The best standing bid held by someone else.

    Excluding the agent's own bid matters: section 13 has ER reading "highest opponent = 75"
    while itself holding 85. Reading its own bid as the target would make a leader chase
    itself.
    """
    rivals = [
        bid.amount
        for bid in round_state.bids
        if bid.agent is not agent and bid.action is not Action.WITHDRAW
    ]
    return max(rivals, default=0.0)


def round_contention(round_state: RoundState) -> float:
    """Contention as carried on this round's bid rows, or 1.0 before any row has one."""
    live = [b.contention for b in round_state.bids if b.contention is not None]
    return live[0] if live else 1.0


def can_afford_to_compete(
    config: Config,
    budget: BudgetState,
    round_state: RoundState,
    mine: float,
    rival: float,
    margin: float,
) -> bool:
    """Could this agent pay for a bid that would actually lead?

    The target is what it takes to *win*, not what it takes to bid. An agent that can afford
    40 against a leader at 90 cannot compete, and bidding 40 anyway buys nothing but a
    participation charge.

    Deliberately permissive in two places, because a false exit is worse than a false stay:
    with no rival yet only a one-point bid is required, and with no contention on the round
    view it falls back to 1.0 rather than refusing.
    """
    affordable = max_affordable_bid(
        config, budget.budget_remaining, round_contention(round_state), won=True
    )
    target = (rival + margin) if rival > EPS else 1.0
    # Already committed this much: the exposure is sunk, so only the increment needs covering.
    return affordable >= min(target, max(target - mine, 1.0))


def climb(
    config: Config,
    *,
    agent: AgentKind,
    ceiling: float,
    round_state: RoundState,
    budget: BudgetState,
    opening_alpha: float,
    lead_alpha: float,
    overtake_margin: float,
) -> LadderOutcome:
    """The four rules, in the order the worked example requires.

    1. standing bid above ceiling      -> EXIT     section 16
    2. ceiling at or below the leader  -> EXIT     section 12
    2b. cannot afford a winning bid    -> EXIT     AGENT_BUDGET 7.3
    3. leading                         -> alpha    section 13
    4. trailing                        -> alpha derived from what overtaking costs

    Rule 4 is the one worth noting: section 14's alpha of 0.82 is not a constant, it is what
    OT needed to clear a leader at 101 from 75 inside a ceiling of 112.
    """
    mine = standing_bid(round_state, agent)
    rival = highest_rival(round_state, agent)

    if mine > ceiling + EPS:
        return LadderOutcome(Verdict.EXIT, reason="standing bid exceeds ceiling")

    if rival > EPS and ceiling <= rival + EPS:
        return LadderOutcome(Verdict.EXIT, reason="ceiling below the leading bid")

    if not can_afford_to_compete(config, budget, round_state, mine, rival, overtake_margin):
        return LadderOutcome(Verdict.EXIT, reason="budget cannot cover a competitive bid")

    headroom = ceiling - mine
    if headroom <= EPS:
        # At the ceiling with a rival ahead is rule 2; at the ceiling while leading there is
        # nothing left to expose. Holding is a form of continuing, not of winning now.
        return LadderOutcome(Verdict.HOLD)

    if mine <= EPS and rival <= EPS:
        return LadderOutcome(Verdict.COMPETE, alpha=opening_alpha)

    if mine >= rival - EPS:
        return LadderOutcome(Verdict.COMPETE, alpha=lead_alpha)

    needed = rival - mine + overtake_margin
    return LadderOutcome(Verdict.COMPETE, alpha=clamp(needed / headroom))
