"""A schedule-based diagnostic simulator.

**The bed simulator does not generalise and is not reused.** It models arrivals, stays,
deteriorations and discharges: a bed is *occupied for a length of stay*. A scanner is *booked
for an interval*, and the difference is not cosmetic — the whole question here is which
interval, on which machine, before which deadline, and none of that has a bed counterpart.
What is reused is everything above it: the auction, the ladder, the budgets, the settlement.

Five things this simulator is required to get right, and where each lives:

    durations            every booking is the procedure plus setup and cleanup
                         (``ModalityProfile.occupies`` / ``machine.interval_for``)
    setup/cleanup        held on the machine, so two machines of one modality can differ
    deadlines            ``latest_useful_at``; a booking that would end after it is refused
                         by ``can_allocate``, never booked and scored badly afterwards
    capabilities         a hard eligibility filter (``machine.is_eligible``), applied before
                         the auction, never as a penalty inside it
    non-overlap          enforced by ``DiagnosticMachineState.__post_init__`` on every state
                         it constructs, so an overlapping schedule cannot be represented

**Determinism is a property, not an aspiration.** Given the same arrivals the run is
reproducible with no seed at all: nothing here samples. Ties break on ``(time, request_id)``
and ``(start, machine_id)`` throughout, which is what stops two runs of the same scenario from
disagreeing about which of two equal machines took a booking.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Mapping, Sequence

from allocation.accounting import (
    ChargeRow,
    Disposition,
    LifecycleAudit,
    audit_lifecycle,
    settlement_problems,
)
from allocation.contracts import AgentKind, AuctionMode, BudgetState
from allocation.use_cases.diagnostic_machine.auction import (
    DiagnosticAuctionResult,
    OUTCOME_AWARDED,
    run_diagnostic_auction,
)
from allocation.use_cases.diagnostic_machine.contracts import (
    AllocationInterval,
    DiagnosticMachineState,
    DiagnosticModality,
    DiagnosticPathwayAction,
    DiagnosticRequest,
)
from allocation.use_cases.diagnostic_machine.policy import DiagnosticHeuristicPolicy
from allocation.use_cases.diagnostic_machine.profiles import ModalityProfile
from allocation.use_cases.diagnostic_machine.scoring import DiagnosticContext
from allocation.config import Config


class Fate:
    """What became of a diagnostic request. One of these, exactly once, for every request.

    The four unserved fates are kept apart because they mean different things operationally
    and the framework's own evaluation asks about them separately. Collapsing ``DIVERTED``
    into ``ABANDONED`` would report a question answered by ultrasound as a question not
    answered, which is the diagnostic form of the mistake the bed reward made with
    re-entry placements.
    """

    #: Booked and performed inside the clinically useful window.
    ANSWERED = "answered"
    #: Booked, performed, but the answer landed after ``latest_useful_at``. Only reachable
    #: through a schedule change after booking; ``can_allocate`` refuses it up front.
    ANSWERED_LATE = "answered_late"
    #: Handed to another modality that answers the same question.
    DIVERTED = "diverted"
    #: Still pending when the safe diagnostic delay ran out.
    EXPIRED = "expired"
    #: Left the auction with nothing arranged.
    ABANDONED = "abandoned"
    #: Still pending when the simulation ended. Not a failure of the mechanism — a boundary.
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class ProcedureRecord:
    """One performed procedure."""

    request_id: str
    agent: AgentKind
    machine_id: str
    modality: DiagnosticModality
    requested_at: datetime
    starts_at: datetime
    ends_at: datetime
    latest_useful_at: datetime
    diagnostic_value: float
    winning_bid: float

    @property
    def on_time(self) -> bool:
        return self.ends_at <= self.latest_useful_at

    @property
    def delay_minutes(self) -> float:
        """Wait from request to procedure start. The framework's headline timeliness number."""
        return (self.starts_at - self.requested_at).total_seconds() / 60.0

    @property
    def deadline_slack_minutes(self) -> float:
        """How much of the useful window was left when the answer landed. Negative is late."""
        return (self.latest_useful_at - self.ends_at).total_seconds() / 60.0


@dataclass(frozen=True, slots=True)
class RequestOutcome:
    """The fate of one request, and what it cost to get there."""

    request_id: str
    agent: AgentKind
    fate: str
    #: The expected management impact this request represented when it was made. Carried on
    #: every outcome, served or not, because it is the DENOMINATOR of the impact rate: scoring
    #: delivered impact over performed impact reads 1.00 on a run that answered one request
    #: out of fifty, which is precisely the flattering non-metric the framework's §31 warns
    #: against.
    requested_value: float = 0.0
    procedure: ProcedureRecord | None = None
    #: Where a diverted request went. Set only for :attr:`Fate.DIVERTED`.
    diverted_to: DiagnosticModality | None = None
    #: How many auctions this request competed in before its fate was settled.
    auctions_entered: int = 0
    note: str = ""
    #: **When the fate actually became determined, on the clinical clock.** The instant the
    #: fate test itself reads — the procedure's end for an answered request (``on_time`` is
    #: ``ends_at <= latest_useful_at``), the deadline for an expiry, the auction's close for a
    #: diversion or an abandonment, the horizon for a pending request.
    #:
    #: This is what a time-based discount must be raised over. Without it the only interval
    #: available to a learner is the number of decisions between the request and its fate,
    #: which is a quantity the POLICY chooses rather than a property of the world: a request
    #: kept bidding for thirty rounds instead of twenty reaches the same fate at the same
    #: moment on the same clock, and a decision-index discount pays it for the extra rounds.
    #: Measured in critical care, a TD policy accrued 1.49x the decision steps of the BASELINE
    #: while moving the clock by 1.03x, and duly learned to stall doomed requests.
    #:
    #: ``None`` only for an outcome built by a caller that predates this field.
    resolved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SimulationMetrics:
    """The framework's §30 evaluation set, computed from outcomes rather than from reward.

    *"Did the learned policy allocate diagnostic machine capacity better than a reasonable
    non-learning policy on previously unseen scenarios?"* — none of these numbers is the
    reward, and that is the point: a policy is compared on what happened to the requests.
    """

    requests: int
    answered: int
    answered_late: int
    diverted: int
    expired: int
    abandoned: int
    pending: int
    auctions: int
    awarded: int
    #: Share of requests answered inside their clinically useful window.
    timeliness: float
    #: Expected management impact actually delivered, over what was requested. The measure
    #: that separates "scanned a lot" from "changed decisions".
    management_impact_rate: float
    #: Mean minutes from request to procedure start, over performed procedures.
    average_delay_minutes: float
    #: Share of machine operating time spent booked, setup and cleanup included.
    utilisation: float
    #: Mean pending queue depth, sampled once per auction.
    mean_queue_depth: float
    #: Share of each agent's shift allowance consumed.
    burn_rate: Mapping[str, float]
    #: Wins per agent — the input to any fairness question, not an answer to it.
    wins: Mapping[str, int]
    #: Share of unserved requests that at least reached another modality.
    alternative_usage: float
    #: Bookings that violated a hard constraint. Must be zero; carried so a run can prove it.
    constraint_violations: int


@dataclass(frozen=True, slots=True)
class SimulationResult:
    machines: tuple[DiagnosticMachineState, ...]
    auctions: tuple[DiagnosticAuctionResult, ...]
    outcomes: tuple[RequestOutcome, ...]
    procedures: tuple[ProcedureRecord, ...]
    budgets: Mapping[AgentKind, BudgetState]
    metrics: SimulationMetrics

    def by_fate(self, fate: str) -> tuple[RequestOutcome, ...]:
        return tuple(o for o in self.outcomes if o.fate == fate)


@dataclass(frozen=True, slots=True)
class Arrival:
    """A request entering the queue at a moment in time."""

    at: datetime
    request: DiagnosticRequest
    context: DiagnosticContext | None = None


@dataclass
class _Pending:
    """A queued request and what the mechanism has done with it so far."""

    arrival: Arrival
    auctions_entered: int = 0


def simulate(
    config: Config,
    profile: ModalityProfile,
    machines: Sequence[DiagnosticMachineState],
    arrivals: Sequence[Arrival],
    budgets: Mapping[AgentKind, BudgetState],
    starts_at: datetime,
    ends_at: datetime,
    policy: DiagnosticHeuristicPolicy | None = None,
    alternative_machines: Sequence[DiagnosticMachineState] = (),
    cadence: timedelta | None = None,
    decision_hook: object | None = None,
) -> SimulationResult:
    """Run one modality's day.

    The clock advances in fixed steps of ``cadence`` — one full auction's length by default,
    so an auction is never opened while the previous one is still running. At each step every
    request that has arrived and not yet been settled competes, one per agent; the winner is
    booked, and the losers' pathway decisions are honoured rather than discarded.

    **Honouring the exits is the part that is easy to get wrong.** A request that withdrew to
    an alternative modality has left this queue for good; one that withdrew to wait for
    capacity has not. Treating them the same — which is what a simulator that only records
    winners does — makes every strategic exit look like an abandonment and understates every
    baseline it is used to score.
    """
    policy = policy or DiagnosticHeuristicPolicy(config)
    # `is None`, not `or`: timedelta(0) is FALSY, so `cadence or default` silently
    # replaced the one value the guard below exists to reject.
    if cadence is None:
        cadence = timedelta(seconds=profile.round_seconds * profile.max_rounds)
    if cadence <= timedelta(0):
        raise ValueError("cadence must be positive")

    state = {m.machine_id: m for m in machines}
    pending: dict[str, _Pending] = {}
    queued = sorted(arrivals, key=lambda a: (a.at, a.request.request_id))
    next_arrival = 0

    outcomes: dict[str, RequestOutcome] = {}
    procedures: list[ProcedureRecord] = []
    auctions: list[DiagnosticAuctionResult] = []
    queue_samples: list[int] = []
    live_budgets = dict(budgets)

    now = starts_at
    while now < ends_at:
        while next_arrival < len(queued) and queued[next_arrival].at <= now:
            arrival = queued[next_arrival]
            pending[arrival.request.request_id] = _Pending(arrival)
            next_arrival += 1

        _expire(pending, outcomes, now)
        queue_samples.append(len(pending))

        contenders = _one_per_agent(pending, profile, list(state.values()), now)
        if contenders:
            outcome = run_diagnostic_auction(
                config,
                profile,
                list(state.values()),
                [p.arrival.request for p in contenders],
                live_budgets,
                policy=policy,
                opened_at=now,
                mode=AuctionMode.SIMULATION,
                contexts={
                    p.arrival.request.request_id: p.arrival.context
                    for p in contenders
                    if p.arrival.context is not None
                },
                charge_budgets=True,
                alternative_machines=alternative_machines,
                decision_hook=decision_hook,  # type: ignore[arg-type]
            )
            # MERGED, not replaced. ``settle_auction`` returns rows only for the agents that
            # held a position in that auction, so assigning its result would delete the budget
            # of any agent that sat this one out — and the next auction it entered would then
            # fail to find it.
            live_budgets = {**live_budgets, **outcome.budgets}
            result = outcome.result
            auctions.append(result)
            for p in contenders:
                p.auctions_entered += 1

            if result.outcome == OUTCOME_AWARDED and result.awarded_interval is not None:
                _award(state, pending, outcomes, procedures, result)

            _honour_exits(result, pending, outcomes)

        now += cadence

    _finalise(pending, outcomes, ends_at, queued[next_arrival:])
    machines_final = tuple(state[m.machine_id] for m in machines)
    return SimulationResult(
        machines=machines_final,
        auctions=tuple(auctions),
        outcomes=tuple(outcomes[k] for k in sorted(outcomes)),
        procedures=tuple(procedures),
        budgets=live_budgets,
        metrics=_metrics(
            outcomes=tuple(outcomes.values()),
            procedures=tuple(procedures),
            auctions=tuple(auctions),
            machines=machines_final,
            budgets=live_budgets,
            queue_samples=queue_samples,
            starts_at=starts_at,
            ends_at=ends_at,
        ),
    )


# -- the steps -----------------------------------------------------------------------------


def _one_per_agent(
    pending: Mapping[str, _Pending],
    profile: ModalityProfile,
    machines: Sequence[DiagnosticMachineState],
    now: datetime,
) -> list[_Pending]:
    """The requests that will bid this round: the most urgent per eligible agent.

    Urgency here is the *deadline*, not the score — picking by utility would let the auction
    choose its own contenders using the same number it is about to bid with, and an agent's
    second patient would never be seen. Earliest deadline first is the queueing discipline;
    the auction decides between agents, not within one.
    """
    by_agent: dict[AgentKind, _Pending] = {}
    for item in sorted(pending.values(), key=_queue_order):
        request = item.arrival.request
        if not profile.is_eligible(request.agent):
            continue
        if not any(m.earliest_start(request, now) is not None for m in machines):
            continue
        by_agent.setdefault(request.agent, item)
    return [by_agent[agent] for agent in sorted(by_agent, key=lambda a: a.value)]


def _queue_order(item: _Pending) -> tuple[datetime, str]:
    request = item.arrival.request
    return request.latest_useful_at, request.request_id


def _award(
    state: dict[str, DiagnosticMachineState],
    pending: dict[str, _Pending],
    outcomes: dict[str, RequestOutcome],
    procedures: list[ProcedureRecord],
    result: DiagnosticAuctionResult,
) -> None:
    """Book the winner onto its machine and retire the request."""
    interval = result.awarded_interval
    assert interval is not None and result.winning_request_id is not None
    item = pending.pop(result.winning_request_id, None)
    if item is None:  # pragma: no cover - the winner is always a contender
        return
    request = item.arrival.request

    machine = state[interval.machine_id]
    state[interval.machine_id] = replace(
        machine, allocations=tuple(sorted(machine.allocations + (interval,),
                                          key=lambda i: i.starts_at))
    )

    starts_at = interval.starts_at + timedelta(minutes=machine.setup_minutes)
    ends_at = interval.ends_at - timedelta(minutes=machine.cleanup_minutes)
    record = ProcedureRecord(
        request_id=request.request_id,
        agent=request.agent,
        machine_id=interval.machine_id,
        modality=result.modality,
        requested_at=request.requested_at,
        starts_at=starts_at,
        ends_at=ends_at,
        latest_useful_at=request.latest_useful_at,
        diagnostic_value=request.diagnostic_value,
        winning_bid=result.winning_bid or 0.0,
    )
    procedures.append(record)
    outcomes[request.request_id] = RequestOutcome(
        request_id=request.request_id,
        agent=request.agent,
        fate=Fate.ANSWERED if record.on_time else Fate.ANSWERED_LATE,
        requested_value=request.diagnostic_value,
        procedure=record,
        auctions_entered=item.auctions_entered,
        # The fate test is ``ends_at <= latest_useful_at``, so the answer landing is the moment
        # this outcome became what it is — not the award, which happens before the scan runs.
        resolved_at=record.ends_at,
    )


def _honour_exits(
    result: DiagnosticAuctionResult,
    pending: dict[str, _Pending],
    outcomes: dict[str, RequestOutcome],
) -> None:
    """Act on what each loser's pathway decision actually committed to.

    Only the final round's decision counts: an agent that continued in round 1 and withdrew in
    round 3 exited once, and its earlier row is a bid, not a fate.

    **The two terminating exits resolve at ``result.closed_at``, not at the event's opening.**
    The decision being honoured is the FINAL round's, taken partway through the event, so
    stamping the opening time would put the outcome BEFORE the decision that caused it and hand
    the trajectory a negative clinical interval.
    """
    if not result.rounds:
        return
    last: dict[str, tuple[DiagnosticPathwayAction, object]] = {}
    for round_ in result.rounds:
        for bid in round_.bids:
            last[bid.request_id] = (bid.pathway, bid.plan)

    for request_id, (pathway, plan) in last.items():
        item = pending.get(request_id)
        if item is None or not pathway.exits:
            continue
        if pathway is DiagnosticPathwayAction.USE_ALTERNATIVE:
            pending.pop(request_id)
            outcomes[request_id] = RequestOutcome(
                request_id=request_id,
                agent=item.arrival.request.agent,
                fate=Fate.DIVERTED,
                requested_value=item.arrival.request.diagnostic_value,
                diverted_to=getattr(plan, "alternative_modality", None),
                auctions_entered=item.auctions_entered,
                note=getattr(plan, "note", ""),
                resolved_at=result.closed_at,
            )
        elif pathway is DiagnosticPathwayAction.WITHDRAW_UNPLANNED:
            pending.pop(request_id)
            outcomes[request_id] = RequestOutcome(
                request_id=request_id,
                agent=item.arrival.request.agent,
                fate=Fate.ABANDONED,
                requested_value=item.arrival.request.diagnostic_value,
                auctions_entered=item.auctions_entered,
                resolved_at=result.closed_at,
            )
        # AWAIT_NEXT_CAPACITY and RE_ENTER_LATER both stay in the queue. That is the whole
        # difference between a strategic exit and an abandonment, and it is the reason this
        # loop exists rather than a blanket "losers are dropped".


def _expire(
    pending: dict[str, _Pending], outcomes: dict[str, RequestOutcome], now: datetime
) -> None:
    """Retire requests whose answer can no longer change anything."""
    for request_id in [
        rid for rid, item in pending.items() if item.arrival.request.latest_useful_at <= now
    ]:
        item = pending.pop(request_id)
        outcomes[request_id] = RequestOutcome(
            request_id=request_id,
            agent=item.arrival.request.agent,
            fate=Fate.EXPIRED,
            requested_value=item.arrival.request.diagnostic_value,
            auctions_entered=item.auctions_entered,
            note="safe diagnostic delay elapsed with no allocation",
            # The useful window closed at ``latest_useful_at``. The sweep that NOTICES it runs
            # on the simulation cadence, so stamping ``now`` here would let the loop's
            # granularity — and, through it, nothing the policy did — move the discount.
            resolved_at=item.arrival.request.latest_useful_at,
        )


def _finalise(
    pending: dict[str, _Pending],
    outcomes: dict[str, RequestOutcome],
    ends_at: datetime,
    unreached: Sequence[Arrival] = (),
) -> None:
    """Everything still live when the clock ran out, and everything it never reached.

    ``unreached`` matters more than it looks. A request that arrives after ``ends_at`` is not
    the mechanism's failure, but dropping it silently would take it out of the denominator of
    every rate — and a scenario whose arrivals run past its own clock would then report a
    flattering timeliness that nothing in the output contradicts.
    """
    _expire(pending, outcomes, ends_at)
    for arrival in unreached:
        outcomes[arrival.request.request_id] = RequestOutcome(
            request_id=arrival.request.request_id,
            agent=arrival.request.agent,
            fate=Fate.PENDING,
            requested_value=arrival.request.diagnostic_value,
            note="arrived after the simulation window closed",
            resolved_at=ends_at,
        )
    for request_id, item in pending.items():
        outcomes[request_id] = RequestOutcome(
            request_id=request_id,
            agent=item.arrival.request.agent,
            fate=Fate.PENDING,
            requested_value=item.arrival.request.diagnostic_value,
            auctions_entered=item.auctions_entered,
            note="simulation ended with the request still live",
            resolved_at=ends_at,
        )
    pending.clear()


# -- metrics -------------------------------------------------------------------------------


def _metrics(
    outcomes: Sequence[RequestOutcome],
    procedures: Sequence[ProcedureRecord],
    auctions: Sequence[DiagnosticAuctionResult],
    machines: Sequence[DiagnosticMachineState],
    budgets: Mapping[AgentKind, BudgetState],
    queue_samples: Sequence[int],
    starts_at: datetime,
    ends_at: datetime,
) -> SimulationMetrics:
    counts = {fate: 0 for fate in (
        Fate.ANSWERED, Fate.ANSWERED_LATE, Fate.DIVERTED, Fate.EXPIRED,
        Fate.ABANDONED, Fate.PENDING,
    )}
    for outcome in outcomes:
        counts[outcome.fate] = counts.get(outcome.fate, 0) + 1

    total = len(outcomes)
    # Denominator is what was ASKED for, not what was performed. Scoring delivered impact
    # over performed impact would read 1.0 on a run that answered one request out of fifty.
    all_impact = sum(o.requested_value for o in outcomes)
    delivered = sum(p.diagnostic_value for p in procedures if p.on_time)

    booked = 0.0
    available = 0.0
    violations = 0
    for machine in machines:
        window_start = max(starts_at, machine.window_starts_at)
        window_end = min(ends_at, machine.window_ends_at)
        if window_end > window_start:
            available += (window_end - window_start).total_seconds()
        ordered = sorted(machine.allocations, key=lambda i: i.starts_at)
        booked += sum((i.ends_at - i.starts_at).total_seconds() for i in ordered)
        violations += sum(
            1 for left, right in zip(ordered, ordered[1:]) if left.overlaps(right)
        )
    violations += sum(1 for p in procedures if not p.on_time)

    unserved = counts[Fate.DIVERTED] + counts[Fate.EXPIRED] + counts[Fate.ABANDONED]
    return SimulationMetrics(
        requests=total,
        answered=counts[Fate.ANSWERED],
        answered_late=counts[Fate.ANSWERED_LATE],
        diverted=counts[Fate.DIVERTED],
        expired=counts[Fate.EXPIRED],
        abandoned=counts[Fate.ABANDONED],
        pending=counts[Fate.PENDING],
        auctions=len(auctions),
        awarded=sum(1 for a in auctions if a.outcome == OUTCOME_AWARDED),
        timeliness=(counts[Fate.ANSWERED] / total) if total else 0.0,
        management_impact_rate=(delivered / all_impact) if all_impact > 0 else 0.0,
        average_delay_minutes=(
            sum(p.delay_minutes for p in procedures) / len(procedures) if procedures else 0.0
        ),
        utilisation=(booked / available) if available > 0 else 0.0,
        mean_queue_depth=(
            sum(queue_samples) / len(queue_samples) if queue_samples else 0.0
        ),
        burn_rate={agent.value: state.burn_rate for agent, state in budgets.items()},
        wins={
            agent.value: sum(1 for a in auctions if a.winner is agent)
            for agent in sorted(budgets, key=lambda a: a.value)
        },
        alternative_usage=(counts[Fate.DIVERTED] / unserved) if unserved else 0.0,
        constraint_violations=violations,
    )


# -- lifecycle and settlement accounting ----------------------------------------------------

#: This family's fate vocabulary mapped onto :class:`~allocation.accounting.Disposition`.
#:
#: ``ANSWERED_LATE`` maps to ``SERVED`` -- the scan happened -- with lateness gated separately as
#: a harm rather than folded into an unserved bucket, which would double-count it. ``PENDING``
#: maps to ``CENSORED``: the horizon closed first, which is a boundary of the observation, not a
#: decision the policy made. It still counts toward ``LifecycleAudit.unresolved`` so a policy
#: cannot pass by converting abandonments into requests it never finishes.
FATE_DISPOSITIONS: Mapping[str, Disposition] = MappingProxyType({
    Fate.ANSWERED: Disposition.SERVED,
    Fate.ANSWERED_LATE: Disposition.SERVED,
    Fate.DIVERTED: Disposition.DIVERTED,
    Fate.EXPIRED: Disposition.EXPIRED,
    Fate.ABANDONED: Disposition.ABANDONED,
    Fate.PENDING: Disposition.CENSORED,
})


def lifecycle_audit(
    arrivals: Sequence[Arrival], outcomes: Sequence[RequestOutcome]
) -> LifecycleAudit:
    """Reconcile one simulated day: ``arrivals == answered + diverted + expired + abandoned +
    censored``, with no request lost, resolved twice, or resolved without arriving.

    Every arrival counts, including one drawn after the window closes -- ``_finalise`` records it
    ``PENDING`` precisely so it stays in the denominator.
    """
    return audit_lifecycle(
        [a.request.request_id for a in arrivals],
        [(o.request_id, o.fate) for o in outcomes],
        FATE_DISPOSITIONS,
    )


def settlement_problems_for(auction: DiagnosticAuctionResult) -> tuple[str, ...]:
    """The settlement problems in one closed auction, as readable strings."""
    winners = [] if auction.winner is None else [auction.winner.value]
    return settlement_problems(
        outcome=auction.outcome,
        awarded_outcome=OUTCOME_AWARDED,
        winners=winners,
        standing_bids={
            agent.value: position.current_bid
            for agent, position in auction.positions.items()
        },
        charges=[
            ChargeRow(
                participant=agent.value,
                standing_bid=auction.positions[agent].current_bid,
                cost=spend.cost,
                won=spend.won,
            )
            for agent, spend in auction.spends.items()
        ],
    )


def settlement_violations(result: SimulationResult) -> int:
    """How many settlement invariants this day's auctions broke. Target 0."""
    return sum(len(settlement_problems_for(a)) for a in result.auctions)


def budget_conservation_problems(result: SimulationResult) -> tuple[str, ...]:
    """Each agent's closing ``spent`` must equal the charges actually applied to it."""
    charged: dict[AgentKind, float] = {}
    for auction in result.auctions:
        if not auction.mode.is_binding and auction.closing_budgets == auction.opening_budgets:
            continue
        for agent, spend in auction.spends.items():
            charged[agent] = charged.get(agent, 0.0) + spend.cost

    out: list[str] = []
    for agent, state in sorted(result.budgets.items(), key=lambda kv: kv[0].value):
        applied = charged.get(agent, 0.0)
        if abs(state.spent - applied) > 1e-6:
            out.append(
                f"{agent.value} closed with spent={state.spent:.6f} but was charged "
                f"{applied:.6f} across {len(result.auctions)} auction(s)"
            )
    return tuple(out)


__all__ = [
    "Arrival",
    "ChargeRow",
    "Disposition",
    "FATE_DISPOSITIONS",
    "Fate",
    "LifecycleAudit",
    "ProcedureRecord",
    "RequestOutcome",
    "SimulationMetrics",
    "SimulationResult",
    "budget_conservation_problems",
    "lifecycle_audit",
    "settlement_problems_for",
    "settlement_violations",
    "simulate",
]
