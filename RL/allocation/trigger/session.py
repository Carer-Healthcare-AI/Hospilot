"""Many auctions across many shifts — the budget lifecycle, exercised.

A single triggered auction can never show what the budget is *for*. It opens at full
allowance, spends once, and stops. Everything AGENT_BUDGET sections 8-9 describe — burn rate,
hourly recovery, exhaustion, the shift roll — only appears across a sequence.

This module is the scheduler that a single run does not have::

    open shift        B = Base x Demand x Fairness x Scarcity
    for each event    recover(elapsed hours) -> run auction -> carry budgets forward
    at a boundary     advance_shift, recomputing the factors
    at shift end      burn rate, banded

**Budgets are charged here even though the auctions are simulations.** The mode gate on
settlement asks whether the budget is *real*, and a session's ledger is its own — see
:func:`~allocation.auction.settle.settle_auction`. A session whose budgets never moved would
report zero burn forever, which is precisely the number it exists to produce.

**Recovery is applied before an auction, not after.** ``recover`` credits elapsed time, so
crediting after the last auction of a shift would inflate the closing balance with time the
shift did not have — and burn rate is measured against that balance.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from allocation.budget.base import BaseBudget, derive_all
from allocation.budget.factors import compute_factors
from allocation.budget.ledger import advance_shift, burn_band, open_shift, recover
from allocation.budget.shifts import Shift, resolve_shift
from allocation.budget.spend import max_affordable_bid
from allocation.config import Config
from allocation.contracts import (
    AgentKind,
    AuctionMode,
    BiddingPolicy,
    BudgetState,
    Candidate,
    DataSource,
)
from allocation.pathway.participation import ParticipationLedger
from allocation.profiles.registry import ResourceProfile
from allocation.trigger.runtime import AllocationRun, run_allocation


@dataclass(frozen=True, slots=True)
class ShiftReport:
    """One shift's budget outcome, per agent."""

    shift: Shift
    auctions: int
    opened: Mapping[AgentKind, float]
    closed: Mapping[AgentKind, float]
    spent: Mapping[AgentKind, float]
    recovered: Mapping[AgentKind, float]
    wins: Mapping[AgentKind, int]
    burn_rate: Mapping[AgentKind, float]
    band: Mapping[AgentKind, str]
    #: Agents that could not afford a whole-point bid at ANY point in the shift.
    #
    #: **Not "remaining hit zero".** The affordability guard clamps every bid to what the
    #: budget can cover and floors it to a whole point, so a correctly-guarded agent never
    #: spends its balance to exactly zero — that definition is unreachable by construction.
    #: Bids are whole points, so the meaningful threshold is ``max_affordable_bid < 1``: the
    #: agent is still solvent and still cannot compete.
    #:
    #: Measured per auction, not at shift close. Hourly recovery credits something before
    #: every auction, so a department that ran dry mid-shift is back above the line by the
    #: end, and reading only the closing balance reports no exhaustion ever.
    exhausted: tuple[AgentKind, ...]

    @property
    def healthy(self) -> bool:
        """Every agent in the working band. The mechanism's health check."""
        return all(b == "working" for b in self.band.values())


@dataclass(frozen=True, slots=True)
class SessionResult:
    """Everything a session produced."""

    runs: tuple[AllocationRun, ...]
    shifts: tuple[ShiftReport, ...]
    bases: Mapping[AgentKind, BaseBudget]
    #: Present when ``track_participation`` was on. Carries the one metric no other report
    #: here exposes: how often a candidate left an auction with nothing arranged.
    participation: "ParticipationLedger | None" = None

    @property
    def wins(self) -> dict[AgentKind, int]:
        out: dict[AgentKind, int] = {}
        for run in self.runs:
            if run.winner is not None:
                out[run.winner] = out.get(run.winner, 0) + 1
        return out

    @property
    def win_share(self) -> dict[AgentKind, float]:
        awarded = sum(self.wins.values())
        return (
            {a: n / awarded for a, n in self.wins.items()} if awarded else {}
        )

    @property
    def unallocated(self) -> int:
        """Auctions that produced no winner. A rising count means the reserve is too high."""
        return sum(1 for run in self.runs if run.winner is None)


def event_schedule(start: datetime, count: int, every: timedelta) -> tuple[datetime, ...]:
    """Evenly spaced release events. The simplest arrival process there is.

    Real bed releases are not evenly spaced, and a burn rate measured against a regular
    schedule will read differently from one measured against a bursty one. This is adequate
    for exercising the lifecycle and is not a demand model.
    """
    if count < 1:
        raise ValueError("a session needs at least one event")
    return tuple(start + every * i for i in range(count))


def run_session(
    config: Config,
    source: DataSource,
    candidates: Sequence[Candidate],
    start: datetime,
    events: Sequence[datetime],
    profile: ResourceProfile | None = None,
    query: str = "",
    policy: BiddingPolicy | None = None,
    mode: AuctionMode = AuctionMode.SIMULATION,
    track_participation: bool = False,
    read_alternatives: bool = False,
) -> SessionResult:
    """Run a sequence of auctions, carrying budgets across events and shifts.

    ``track_participation`` turns on the consequence of the strategic exits: a candidate
    placed in an alternative unit stops bidding, one under a re-entry monitor stays out until
    it fires, and one abandoned stays in. **Off by default and deliberately so** — it changes
    who bids in every auction after the first, which would silently move every existing
    session-level regression. Off, the session keeps its old behaviour of re-running one fixed
    cohort, which is the behaviour those tests were written against.

    On is what any training run wants. With it off, all four exits have identical downstream
    consequences, so no policy can learn a preference between them.
    """
    if not events:
        raise ValueError("no events scheduled")
    if mode.is_binding:
        raise ValueError(
            "a session runs many auctions against one in-memory ledger and cannot be live; "
            "a live sequence must go through the real budget rows one auction at a time."
        )

    agents = tuple(dict.fromkeys(c.agent for c in candidates))
    bases = derive_all(config, agents)
    ledger = (
        ParticipationLedger.for_candidates(config, candidates, start)
        if track_participation
        else None
    )

    hospital = None  # filled from the first run; factors need occupancy
    shift = resolve_shift(config, start)
    budgets = _open(config, bases, agents, shift, occupancy=None, previous=None)

    runs: list[AllocationRun] = []
    reports: list[ShiftReport] = []

    shift_opened = dict(budgets)
    shift_wins: dict[AgentKind, int] = {}
    shift_dry: set[AgentKind] = set()
    shift_auctions = 0
    last_at = shift.start

    for moment in events:
        current = resolve_shift(config, moment)

        if current.shift_id != shift.shift_id:
            # Close the shift that ended, then roll. Recovery for the tail of the old shift
            # is deliberately not credited: it would raise a closing balance that burn rate
            # is measured against.
            reports.append(
                _report(
                    config, shift, shift_auctions, shift_opened, budgets, shift_wins, shift_dry
                )
            )
            budgets = _open(
                config, bases, agents, current,
                occupancy=hospital.occupancy if hospital else None,
                previous=budgets,
            )
            shift, shift_opened, shift_wins, shift_auctions = current, dict(budgets), {}, 0
            shift_dry = set()
            last_at = current.start

        elapsed = max(0.0, (moment - last_at).total_seconds() / 3600.0)
        if elapsed > 0:
            budgets = {a: recover(config, s, elapsed) for a, s in budgets.items()}

        bidding = candidates
        if ledger is not None:
            bidding = ledger.bidders(candidates, moment)
            if not bidding:
                # Every candidate is placed or monitored. A bed with nobody to bid for it is a
                # real outcome and not an error — it is what a session looks like when the
                # alternatives absorbed the demand. Skipped rather than run, because
                # `run_auction` refuses an auction with no eligible bidder, and rightly.
                last_at = moment
                continue

        run = run_allocation(
            config=config,
            source=source,
            candidates=bidding,
            now=moment,
            query=query,
            profile=profile,
            policy=policy,
            mode=mode,
            budgets=budgets,
            charge_budgets=True,
            resource_id=f"icu-bed-{moment:%Y%m%d-%H%M}",
            read_alternatives=read_alternatives,
        )
        runs.append(run)
        if ledger is not None:
            ledger.record(run.outcome.result, moment)

        # MERGED, not replaced. An auction returns budget rows only for the agents that bid in
        # it, and once participation tracking is on that is a subset — a department whose
        # patient is monitored in HDU sits out entirely. Replacing the ledger would drop that
        # department's row, and the next auction it does bid in would be refused for having no
        # budget (or, worse, silently handed a fresh shift allowance mid-shift).
        budgets = {**budgets, **run.outcome.budgets}
        contention = run.outcome.result.contention
        shift_dry.update(
            a for a, s in budgets.items() if _cannot_bid(config, s, contention)
        )
        hospital = run.snapshot.hospital
        shift_auctions += 1
        if run.winner is not None:
            shift_wins[run.winner] = shift_wins.get(run.winner, 0) + 1
        last_at = moment

    reports.append(
        _report(config, shift, shift_auctions, shift_opened, budgets, shift_wins, shift_dry)
    )
    return SessionResult(
        runs=tuple(runs), shifts=tuple(reports), bases=bases, participation=ledger
    )


# -- internals ---------------------------------------------------------------------------


def _cannot_bid(config: Config, state: BudgetState, contention: float) -> bool:
    """True when the budget cannot cover even a one-point bid.

    Bids are whole points, so this is the point at which a department stops being able to
    compete — regardless of the balance still showing on the row.
    """
    return max_affordable_bid(config, state.budget_remaining, contention, won=True) < 1.0


def _open(
    config: Config,
    bases: Mapping[AgentKind, BaseBudget],
    agents: Sequence[AgentKind],
    shift: Shift,
    occupancy: float | None,
    previous: Mapping[AgentKind, BudgetState] | None,
) -> dict[AgentKind, BudgetState]:
    out: dict[AgentKind, BudgetState] = {}
    for agent in agents:
        factors = compute_factors(config, agent, occupancy_4h=occupancy)
        if previous is None:
            out[agent] = open_shift(config, bases[agent], factors, shift)
        else:
            out[agent] = advance_shift(config, previous[agent], bases[agent], factors, shift)
    return out


def _report(
    config: Config,
    shift: Shift,
    auctions: int,
    opened: Mapping[AgentKind, BudgetState],
    closed: Mapping[AgentKind, BudgetState],
    wins: Mapping[AgentKind, int],
    dry: set[AgentKind],
) -> ShiftReport:
    burn = {a: s.burn_rate for a, s in closed.items()}
    return ShiftReport(
        shift=shift,
        auctions=auctions,
        opened={a: s.budget_total for a, s in opened.items()},
        closed={a: s.budget_remaining for a, s in closed.items()},
        spent={a: s.spent for a, s in closed.items()},
        recovered={a: s.recovered for a, s in closed.items()},
        wins={a: wins.get(a, 0) for a in closed},
        burn_rate=burn,
        band={a: burn_band(config, r) for a, r in burn.items()},
        # Worth naming separately from a high burn rate: 1.4 is stressed, 0 remaining is
        # silent — the department simply stops competing and nothing in the burn number says
        # a bid was never made.
        exhausted=tuple(a for a in closed if a in dry),
    )


def with_rounds(profile: ResourceProfile, max_rounds: int) -> ResourceProfile:
    """A copy of the profile with a different round *ceiling*.

    RL-Steps fixes three rounds against a bed thirty minutes out, so this is a *test* knob.
    Raising it does not necessarily buy rounds: ``runner.round_budget`` still clamps each
    auction to what the release lead time and the shortest utility TTL allow, and on the ICU
    profile the 10-minute ER TTL caps any auction at five rounds however high this goes.
    """
    if max_rounds < 1:
        raise ValueError("an auction needs at least one round")
    return replace(profile, max_rounds=max_rounds)
