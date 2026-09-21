"""Running a diagnostic-machine auction on the shared auction core.

**Almost nothing here is new machinery.** The ladder, the guards, the position bookkeeping,
the standing-bid view, the contention formula, the reserve price, the winner rule and the
budget settlement are all imported. What this module owns is the sequencing and the
vocabulary: a diagnostic auction allocates *an interval on a machine* rather than a bed, and
its decisions are :class:`DiagnosticPathwayAction`, not ``QAction``.

**Why not ``auction/runner.py`` itself?** That runner reads ``snapshot.hospital.occupancy``
and ``expected_discharges_4h`` — a bed-shaped ``HospitalState`` — in three places, to feed
contention and the reserve. A scanner has neither. The functions it *calls* are all neutral,
so this composes the same ones with a machine's committed capacity in place of a ward's
occupancy. That substitution is stated at each call site rather than hidden in a helper,
because it is the one place a bed idea is being reinterpreted rather than reused.

**Settlement is genuinely the bed function, not a copy.** ``settle_auction`` reads exactly
four attributes — ``positions``, ``winner``, ``contention`` and ``mode`` — so
:class:`DiagnosticAuctionResult` carries them under the same names and settles through the
same ledger. Charging diagnostics through a second implementation would let the two families'
budgets diverge on rounding alone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Mapping, Protocol, Sequence

from allocation.auction.guards import apply_guards
from allocation.auction.reserve import reserve_price
from allocation.auction.rounds import decision_order
from allocation.auction.settle import (
    OUTCOME_AWARDED,
    OUTCOME_NO_AWARD,
    determine_winner,
    settle_auction,
)
from allocation.auction.state import Position, standing_bids
from allocation.budget.spend import (
    SpendResult,
    contention as compute_contention,
    max_affordable_bid,
)
from allocation.config import Config
from allocation.contracts import Action, AgentKind, AuctionMode, BudgetState, RoundState
from allocation.use_cases.diagnostic_machine import capacity
from allocation.use_cases.diagnostic_machine.contracts import (
    AllocationInterval,
    DiagnosticMachineState,
    DiagnosticModality,
    DiagnosticPathwayAction,
    DiagnosticRequest,
)
from allocation.use_cases.diagnostic_machine.policy import (
    DiagnosticDecision,
    DiagnosticHeuristicPolicy,
    DiagnosticOptions,
    DiagnosticPlan,
    build_options,
)
from allocation.use_cases.diagnostic_machine.profiles import ModalityProfile
from allocation.use_cases.diagnostic_machine.scoring import (
    DiagnosticContext,
    caps_from_config,
    score_request,
)
from allocation.use_cases.diagnostic_machine.utility import DiagnosticUtility


class DiagnosticExitReason:
    """Why an agent stopped bidding for machine capacity.

    Parallel to ``auction.state.ExitReason`` and separate from it: the mechanical reasons are
    identical because they are facts about any auction, and the strategic ones are not,
    because "withdrew to an alternative unit" is not a thing that happens to a scan request.
    """

    BID_ABOVE_CEILING = "standing bid exceeds ceiling"
    CANNOT_WIN = "ceiling at or below the leading bid"
    UNAFFORDABLE = "budget cannot cover a competitive bid"
    POLICY = "policy withdrew"

    ALTERNATIVE = "withdrew to an alternative modality"
    AWAIT_CAPACITY = "withdrew to wait for scheduled capacity"
    RE_ENTER = "withdrew under a re-entry monitor"
    UNPLANNED = "withdrew with nothing arranged"

    @classmethod
    def for_action(cls, pathway: DiagnosticPathwayAction) -> str:
        return _STRATEGIC_EXITS.get(pathway, cls.POLICY)


_STRATEGIC_EXITS = {
    DiagnosticPathwayAction.USE_ALTERNATIVE: DiagnosticExitReason.ALTERNATIVE,
    DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY: DiagnosticExitReason.AWAIT_CAPACITY,
    DiagnosticPathwayAction.RE_ENTER_LATER: DiagnosticExitReason.RE_ENTER,
    DiagnosticPathwayAction.WITHDRAW_UNPLANNED: DiagnosticExitReason.UNPLANNED,
}


@dataclass(frozen=True, slots=True)
class DiagnosticBid:
    """One agent's position in one round. Losers and withdrawals are recorded too.

    ``action`` and ``pathway`` are both stored because they answer different questions: the
    first is what the auction did, the second is what the request did. A log that kept only
    the first cannot tell an abandonment from a handover to ultrasound.
    """

    auction_id: str
    round_index: int
    agent: AgentKind
    request_id: str
    action: Action
    pathway: DiagnosticPathwayAction
    amount: float
    utility: float
    ceiling: float
    alpha: float | None = None
    contention: float | None = None
    plan: DiagnosticPlan | None = None
    feasible: frozenset[DiagnosticPathwayAction] = field(default_factory=frozenset)
    clamped_by: str = ""
    component_scores: Mapping[str, float] = field(default_factory=dict)
    component_caps: Mapping[str, float] = field(default_factory=dict)
    component_points: Mapping[str, float] = field(default_factory=dict)
    previous_bid: float = 0.0
    leader_bid: float = 0.0
    proposed_bid: float | None = None
    affordable_limit: float | None = None
    remaining_budget: float = 0.0


@dataclass(frozen=True, slots=True)
class DiagnosticRound:
    auction_id: str
    round_index: int
    opened_at: datetime
    bids: tuple[DiagnosticBid, ...]
    active_agents: frozenset[AgentKind]

    @property
    def highest_bid(self) -> float:
        live = [b.amount for b in self.bids if b.action is not Action.WITHDRAW]
        return max(live, default=0.0)


@dataclass(frozen=True, slots=True)
class DiagnosticAuctionResult:
    """A closed diagnostic auction, with every round retained.

    The four attribute names ``settle_auction`` reads — ``positions``, ``winner``,
    ``contention``, ``mode`` — are deliberate, and are what let this settle through the bed
    ledger rather than through a copy of it.
    """

    auction_id: str
    auction_key: str
    modality: DiagnosticModality
    machine_id: str
    mode: AuctionMode
    opened_at: datetime
    closed_at: datetime
    max_rounds: int
    reserve_price: float
    contention: float
    rounds: tuple[DiagnosticRound, ...]
    positions: Mapping[AgentKind, Position]
    winner: AgentKind | None
    winning_request_id: str | None
    winning_bid: float | None
    outcome: str
    #: The interval the winner actually holds the machine for — setup and cleanup included.
    #: ``None`` on a no-award, and on an award whose start no longer fits the schedule, which
    #: is not the same thing and is why the outcome string is stored separately.
    awarded_interval: AllocationInterval | None
    caps_version: str
    config_version: str
    unsigned_rules: Mapping[str, str] = field(default_factory=dict)
    opening_budgets: Mapping[AgentKind, BudgetState] = field(default_factory=dict)
    closing_budgets: Mapping[AgentKind, BudgetState] = field(default_factory=dict)
    spends: Mapping[AgentKind, SpendResult] = field(default_factory=dict)

    @property
    def rounds_run(self) -> int:
        return len(self.rounds)

    @property
    def bids(self) -> tuple[DiagnosticBid, ...]:
        return tuple(bid for round_ in self.rounds for bid in round_.bids)


@dataclass(frozen=True, slots=True)
class DiagnosticAuctionOutcome:
    result: DiagnosticAuctionResult
    budgets: Mapping[AgentKind, BudgetState]
    spends: Mapping[AgentKind, SpendResult]


class DecisionHook(Protocol):
    """What a learner needs at a decision point, and nothing more.

    Called **before** the decision is acted on and against the same view the policy receives,
    so the state a transition records is the state the action was chosen from. The bed system
    reconstructed this afterwards from the closed result, which paired every action with a
    state that already contained its consequence — ``rounds_left`` read 0.010 where the
    decision saw 0.562 — and the learner fit values on states it would never see at serving
    time.

    A callable rather than an object so the auction keeps no dependency on the RL layer.
    """

    def __call__(
        self,
        *,
        agent: AgentKind,
        request: DiagnosticRequest,
        round_index: int,
        utility: DiagnosticUtility,
        ceiling: float,
        view: RoundState,
        budget: BudgetState,
        options: DiagnosticOptions,
        contention: float,
        max_rounds: int,
        decision: DiagnosticDecision,
    ) -> None: ...


class DiagnosticUtilitySource(Protocol):
    """Utilities and ceilings, re-read every round.

    Expiry means *recalculate*, not re-award. Delay pressure rises every round by
    construction, so a utility generated at round 0 is already stale at round 2 — and it is
    that rise which lets a long-waiting request eventually outbid a fresh one.
    """

    def utilities(self, round_index: int) -> Mapping[str, DiagnosticUtility]: ...

    def ceilings(self, round_index: int) -> Mapping[str, float]: ...


@dataclass(frozen=True, slots=True)
class RescoringUtilities:
    """The default source: rescore every request at each round's clock.

    ``Ceiling = U`` via :func:`allocation.utility.ceiling.ceiling_for`'s D.9 fallback, which
    is shared with the bed family. Bid != utility != ceiling stays load-bearing here for the
    same reason it does there.
    """

    requests: Mapping[str, DiagnosticRequest]
    profile: ModalityProfile
    machines: Sequence[DiagnosticMachineState]
    opened_at: datetime
    caps: Mapping[object, float] | None = None
    contexts: Mapping[str, DiagnosticContext] = field(default_factory=dict)

    def _at(self, round_index: int) -> datetime:
        return self.opened_at + timedelta(seconds=self.profile.round_seconds * round_index)

    def utilities(self, round_index: int) -> Mapping[str, DiagnosticUtility]:
        now = self._at(round_index)
        return {
            request_id: score_request(
                request,
                self.profile,
                self.machines,
                now,
                context=self.contexts.get(request_id),
                caps=self.caps,  # type: ignore[arg-type]
            )
            for request_id, request in self.requests.items()
        }

    def ceilings(self, round_index: int) -> Mapping[str, float]:
        from allocation.utility.ceiling import ceiling_for

        return {
            request_id: ceiling_for(utility).value
            for request_id, utility in self.utilities(round_index).items()
        }


# -- capacity pressure ---------------------------------------------------------------------


def committed_fraction(
    machines: Sequence[DiagnosticMachineState], now: datetime, horizon: timedelta
) -> float:
    """Occupancy for contention and the reserve price. See :mod:`.capacity`.

    Resolves a no-reading to 1.0 here rather than in the shared arithmetic: an auction with no
    measurable capacity is maximally contested, and the other callers of
    :func:`capacity.committed_fraction` want their own fallback.
    """
    value = capacity.committed_fraction(machines, now, now + horizon)
    return 1.0 if value is None else value


def free_slots(
    machines: Sequence[DiagnosticMachineState],
    request: DiagnosticRequest,
    now: datetime,
    horizon: timedelta,
) -> float:
    """Relief coming over the horizon, standing in for ``expected_discharges_4h``."""
    return capacity.free_slots(machines, request, now, horizon)


# -- the auction ---------------------------------------------------------------------------


def run_diagnostic_auction(
    config: Config,
    profile: ModalityProfile,
    machines: Sequence[DiagnosticMachineState],
    requests: Sequence[DiagnosticRequest],
    budgets: Mapping[AgentKind, BudgetState],
    policy: DiagnosticHeuristicPolicy | None = None,
    opened_at: datetime | None = None,
    mode: AuctionMode = AuctionMode.SIMULATION,
    utility_source: DiagnosticUtilitySource | None = None,
    contexts: Mapping[str, DiagnosticContext] | None = None,
    charge_budgets: bool | None = None,
    alternative_machines: Sequence[DiagnosticMachineState] = (),
    decision_hook: "DecisionHook | None" = None,
) -> DiagnosticAuctionOutcome:
    """Run one auction for capacity on ``machines`` to its close.

    One request per agent — the auction allocates one interval, and an agent bidding for two
    of its own patients at once is a queueing question this does not answer. The eligibility
    filter is a **hard constraint**: a request whose modality, capability or deadline the
    machines cannot meet is removed before bidding, never penalised inside it, because a
    penalty is something a learned policy can outspend.
    """
    if not machines:
        raise ValueError("a diagnostic auction needs at least one machine")
    if len({m.modality for m in machines}) != 1:
        raise ValueError("one auction allocates one modality's capacity")
    if machines[0].modality is not profile.modality:
        raise ValueError(
            f"machines are {machines[0].modality.value} but profile is {profile.modality.value}"
        )

    opened_at = opened_at or min(m.window_starts_at for m in machines)
    horizon = timedelta(hours=profile.allocation_horizon_hours)
    policy = policy or DiagnosticHeuristicPolicy(config)

    eligible = [
        request
        for request in requests
        if profile.is_eligible(request.agent)
        and any(m.earliest_start(request, opened_at) is not None for m in machines)
    ]
    if not eligible:
        raise ValueError(
            f"no eligible bidders for {profile.modality.value}; profile allows "
            f"{[a.value for a in profile.eligible_agents]} and every request must have a "
            "start that fits both the machine window and its own deadline"
        )
    seen = {request.agent for request in eligible}
    if len(seen) != len(eligible):
        raise ValueError("one request per agent per auction")

    by_agent = {request.agent: request for request in eligible}
    by_id = {request.request_id: request for request in eligible}
    positions = {
        request.agent: Position(agent=request.agent, candidate_id=request.request_id)
        for request in eligible
    }

    utility_source = utility_source or RescoringUtilities(
        requests=by_id,
        profile=profile,
        machines=machines,
        opened_at=opened_at,
        caps=caps_from_config(config.caps) if "components" in config.caps else None,
        contexts=dict(contexts or {}),
    )

    # Contention is fixed at open, from the opening bidder count — see auction/settle.py. The
    # two hospital-state inputs are the machine substitutions documented above.
    occupancy = committed_fraction(machines, opened_at, horizon)
    contention = compute_contention(
        config,
        n_bidders=len(eligible),
        occupancy=occupancy,
        expected_discharges_4h=free_slots(machines, eligible[0], opened_at, horizon),
    )

    auction_id = str(uuid.uuid4())
    rounds: list[DiagnosticRound] = []

    for round_index in range(profile.max_rounds):
        round_opened = opened_at + timedelta(seconds=profile.round_seconds * round_index)
        before = {a: (p.active, p.current_bid) for a, p in positions.items()}
        state, positions = _run_round(
            config,
            auction_id=auction_id,
            round_index=round_index,
            opened_at=round_opened,
            positions=positions,
            requests=by_agent,
            utilities=utility_source.utilities(round_index),
            ceilings=utility_source.ceilings(round_index),
            budgets=budgets,
            profile=profile,
            machines=machines,
            alternative_machines=alternative_machines,
            policy=policy,
            contention=contention,
            decision_hook=decision_hook,
        )
        rounds.append(state)

        if not state.active_agents:
            break
        # Rounds do NOT stop early merely because one bidder is left: an unopposed bidder must
        # still raise to meet the reserve, or scarce capacity goes at the opening bid.
        quiescent = {a: (p.active, p.current_bid) for a, p in positions.items()} == before
        if quiescent and _reserve_met_now(
            config, positions, utility_source.ceilings(round_index), occupancy
        ):
            break

    closing_ceilings = utility_source.ceilings(len(rounds) - 1)
    reserve = reserve_price(
        config, highest_ceiling=max(closing_ceilings.values(), default=0.0), occupancy=occupancy
    )
    closed_at = opened_at + timedelta(seconds=profile.round_seconds * len(rounds))
    winner, outcome = determine_winner(positions, reserve)

    awarded_interval = None
    if winner is not None:
        awarded_interval = _book(machines, by_agent[winner], closed_at)
        if awarded_interval is None:
            # The auction found a price and the schedule then refused it: every start that
            # still fits the request's deadline was gone by close. Recorded as no_award rather
            # than as an award nobody can perform, and the budgets settle as a no-award too.
            winner, outcome = None, OUTCOME_NO_AWARD

    winning_position = positions[winner] if winner is not None else None
    result = DiagnosticAuctionResult(
        auction_id=auction_id,
        auction_key=_auction_key(profile, machines, opened_at),
        modality=profile.modality,
        machine_id=awarded_interval.machine_id if awarded_interval else machines[0].machine_id,
        mode=mode,
        opened_at=opened_at,
        closed_at=closed_at,
        max_rounds=profile.max_rounds,
        reserve_price=reserve,
        contention=contention,
        rounds=tuple(rounds),
        positions=positions,
        winner=winner,
        winning_request_id=winning_position.candidate_id if winning_position else None,
        winning_bid=winning_position.current_bid if winning_position else None,
        outcome=outcome,
        awarded_interval=awarded_interval,
        caps_version=config.caps_version,
        config_version=config.config_version,
        unsigned_rules=dict(config.unsigned),
    )

    updated, spends = settle_auction(config, result, budgets, charge=charge_budgets)
    result = replace(
        result,
        opening_budgets=dict(budgets),
        closing_budgets=dict(updated),
        spends=dict(spends),
    )
    return DiagnosticAuctionOutcome(result=result, budgets=updated, spends=spends)


def _run_round(
    config: Config,
    *,
    auction_id: str,
    round_index: int,
    opened_at: datetime,
    positions: Mapping[AgentKind, Position],
    requests: Mapping[AgentKind, DiagnosticRequest],
    utilities: Mapping[str, DiagnosticUtility],
    ceilings: Mapping[str, float],
    budgets: Mapping[AgentKind, BudgetState],
    profile: ModalityProfile,
    machines: Sequence[DiagnosticMachineState],
    alternative_machines: Sequence[DiagnosticMachineState],
    policy: DiagnosticHeuristicPolicy,
    contention: float,
    decision_hook: "DecisionHook | None" = None,
) -> tuple[DiagnosticRound, dict[AgentKind, Position]]:
    """One bidding round.

    Agents act in descending order of standing bid, and a later actor sees an earlier actor's
    raise — the same ordering the bed engine uses, via the same ``decision_order``. Round 1 is
    sealed: everyone decides against the state at round start.
    """
    working = dict(positions)
    recorded: list[DiagnosticBid] = []

    for agent, position in working.items():
        utility = utilities.get(position.candidate_id)
        if utility is None:
            continue
        working[agent] = position.with_bid(
            position.current_bid, utility.total, ceilings[position.candidate_id]
        )

    sealed = bool(config.auction["rounds"].get("sealed_first_round", True)) and round_index == 0
    at_round_start = dict(working)

    for agent in decision_order(working):
        position = working[agent]
        request = requests[agent]
        utility = utilities[position.candidate_id]
        ceiling = ceilings[position.candidate_id]
        view: RoundState = standing_bids(
            at_round_start if sealed else working, auction_id, round_index, opened_at
        )
        options = build_options(
            request,
            profile,
            machines,
            opened_at,
            alternative_machines=alternative_machines,
        )
        decision = policy.decide(
            agent,
            ceiling,
            view,
            budgets[agent],
            options,
            # A rule-based policy ignores these three; a learned one cannot encode its
            # state without them. Passed always rather than conditionally, so the two
            # kinds of policy sit behind one seam.
            utility=utility,
            contention=contention,
            max_rounds=profile.max_rounds,
        )
        if decision_hook is not None:
            decision_hook(
                agent=agent,
                request=request,
                round_index=round_index,
                utility=utility,
                ceiling=ceiling,
                view=view,
                budget=budgets[agent],
                options=options,
                contention=contention,
                max_rounds=profile.max_rounds,
                decision=decision,
            )

        if decision.action is Action.WITHDRAW:
            working[agent] = position.withdrawn(
                round_index, _exit_reason(position, ceiling, view, decision)
            )
            recorded.append(
                _row(auction_id, round_index, working[agent], decision, position.current_bid,
                     utility, ceiling, contention, previous_bid=position.current_bid,
                     leader_bid=view.highest_bid, remaining_budget=budgets[agent].budget_remaining)
            )
            continue

        if decision.action is Action.HOLD or decision.alpha is None:
            recorded.append(
                _row(auction_id, round_index, position, decision, position.current_bid,
                     utility, ceiling, contention, previous_bid=position.current_bid,
                     leader_bid=view.highest_bid, remaining_budget=budgets[agent].budget_remaining)
            )
            continue

        # A ceiling at or below zero admits no legal bid, not even a zero one — the same guard
        # the bed rounds carry, and reachable here for the same reason: exploration.
        if ceiling <= 0.0:
            working[agent] = position.withdrawn(
                round_index, _exit_reason(position, ceiling, view, decision)
            )
            recorded.append(
                _row(auction_id, round_index, working[agent],
                     replace(decision, pathway=DiagnosticPathwayAction.WITHDRAW_UNPLANNED,
                             plan=None, alpha=None),
                     position.current_bid, utility, ceiling, contention,
                     previous_bid=position.current_bid, leader_bid=view.highest_bid,
                     remaining_budget=budgets[agent].budget_remaining)
            )
            continue

        increment = decision.alpha * (ceiling - position.current_bid)
        proposed = position.current_bid + increment
        affordable = max_affordable_bid(
            config, budgets[agent].budget_remaining, contention, won=True
        )
        guarded = apply_guards(
            config,
            proposed=proposed,
            ceiling=ceiling,
            remaining_budget=budgets[agent].budget_remaining,
            contention=contention,
        )
        working[agent] = position.with_bid(guarded.amount, utility.total, ceiling)
        recorded.append(
            _row(auction_id, round_index, working[agent], decision, guarded.amount, utility,
                 ceiling, contention, clamped_by=guarded.clamped_by,
                 previous_bid=position.current_bid, leader_bid=view.highest_bid,
                 proposed_bid=proposed, affordable_limit=affordable,
                 remaining_budget=budgets[agent].budget_remaining)
        )

    return (
        DiagnosticRound(
            auction_id=auction_id,
            round_index=round_index,
            opened_at=opened_at,
            bids=tuple(recorded),
            active_agents=frozenset(a for a, p in working.items() if p.active),
        ),
        working,
    )


def _row(
    auction_id: str,
    round_index: int,
    position: Position,
    decision: DiagnosticDecision,
    amount: float,
    utility: DiagnosticUtility,
    ceiling: float,
    contention: float,
    clamped_by: str = "",
    previous_bid: float = 0.0,
    leader_bid: float = 0.0,
    proposed_bid: float | None = None,
    affordable_limit: float | None = None,
    remaining_budget: float = 0.0,
) -> DiagnosticBid:
    return DiagnosticBid(
        auction_id=auction_id,
        round_index=round_index,
        agent=position.agent,
        request_id=position.candidate_id,
        action=decision.action,
        pathway=decision.pathway,
        amount=amount,
        utility=utility.total,
        ceiling=ceiling,
        alpha=decision.alpha,
        contention=contention,
        plan=decision.plan,
        feasible=decision.feasible,
        clamped_by=clamped_by,
        component_scores={key.value: value for key, value in utility.scores.items()},
        component_caps={key.value: value for key, value in utility.caps.items()},
        component_points={key.value: value for key, value in utility.points.items()},
        previous_bid=previous_bid,
        leader_bid=leader_bid,
        proposed_bid=proposed_bid,
        affordable_limit=affordable_limit,
        remaining_budget=remaining_budget,
    )


def _exit_reason(
    position: Position, ceiling: float, view: RoundState, decision: DiagnosticDecision
) -> str:
    """Why this agent left, preferring the mechanical fact over what the policy called it."""
    if position.current_bid > ceiling:
        return DiagnosticExitReason.BID_ABOVE_CEILING
    rivals = [
        bid.amount
        for bid in view.bids
        if bid.agent is not position.agent and bid.action is not Action.WITHDRAW
    ]
    if rivals and ceiling <= max(rivals):
        return DiagnosticExitReason.CANNOT_WIN
    return DiagnosticExitReason.for_action(decision.pathway)


def _reserve_met_now(
    config: Config,
    positions: Mapping[AgentKind, Position],
    ceilings: Mapping[str, float],
    occupancy: float,
) -> bool:
    """Would the standing leader clear the reserve if the auction closed here?"""
    from allocation.auction.reserve import meets_reserve

    standing = max((p.current_bid for p in positions.values() if p.active), default=0.0)
    reserve = reserve_price(
        config, highest_ceiling=max(ceilings.values(), default=0.0), occupancy=occupancy
    )
    return meets_reserve(standing, reserve)


def _book(
    machines: Sequence[DiagnosticMachineState],
    request: DiagnosticRequest,
    not_before: datetime,
) -> AllocationInterval | None:
    """The interval the winner gets: earliest start across the machines that can take it."""
    offers = [
        (machine, machine.earliest_start(request, not_before))
        for machine in machines
    ]
    usable = [(m, start) for m, start in offers if start is not None]
    if not usable:
        return None
    machine, start = min(usable, key=lambda item: (item[1], item[0].machine_id))
    return machine.interval_for(request, start)


def _auction_key(
    profile: ModalityProfile, machines: Sequence[DiagnosticMachineState], opened_at: datetime
) -> str:
    """Modality plus machine set plus time bucket, so a re-fired trigger cannot double-open."""
    bucket = int(opened_at.timestamp() // (profile.auction_key_bucket_minutes * 60))
    ids = "+".join(sorted(m.machine_id for m in machines))
    return f"{profile.modality.value}:{ids}:{bucket}"


__all__ = [
    "DiagnosticAuctionOutcome",
    "DiagnosticAuctionResult",
    "DiagnosticBid",
    "DecisionHook",
    "DiagnosticExitReason",
    "DiagnosticRound",
    "DiagnosticUtilitySource",
    "OUTCOME_AWARDED",
    "OUTCOME_NO_AWARD",
    "RescoringUtilities",
    "committed_fraction",
    "free_slots",
    "run_diagnostic_auction",
]
