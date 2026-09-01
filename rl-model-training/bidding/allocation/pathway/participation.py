"""Who bids in the *next* auction, given what happened in the last one.

**This is the module that makes the six actions mean anything.** Without it every exit has the
same consequence — the same three patients bid again in the next auction regardless of whether
one was moved to HDU, one is waiting on a predicted bed and one was abandoned. Three decisions
with identical downstream behaviour are one decision with three names, and no policy can learn
a preference between them because none of them changes the world.

RL_READINESS §5.2 names this as the first of the six missing pieces: *"The same three patients
bid in every auction today, so a policy sees one state forever."* An arrival process fixes the
*inflow*; this fixes the *outflow*, and the outflow is the half the action space controls.

The state machine, one entry per way an auction can end for an agent::

    won                    RESOLVED    has the bed
    withdraw_alternative   RESOLVED    has a bed elsewhere, for the whole horizon
    re_enter_later         MONITORED   out of the pool until the trigger fires
    await_next_resource    DEFERRED    back in the next auction — that is what it is waiting for
    withdraw_unplanned     UNRESOLVED  back in the pool, nothing arranged
    lost, still active     UNRESOLVED  back in the pool, still needs a bed

Two of these are worth stating plainly because they are what the reward will eventually price:

**RESOLVED is the only state that leaves the queue.** An agent that withdraws to a definitive
alternative stops competing, which frees the next bed for someone else — the whole point of
*"achieve acceptable outcome without consuming the scarce resource"*. It is also the state most
easily reached dishonestly, which is why :class:`~allocation.contracts.Decision` refuses to
construct a ``WITHDRAW_ALTERNATIVE`` that does not name a unit.

**UNRESOLVED accumulates.** A patient nobody could place stays in the pool and bids again, and
their utility rises as they deteriorate. That is correct and it is also the signal worth
watching: :attr:`ParticipationLedger.abandoned` counts how many times the system has failed to
arrange anything at all, which no burn rate or win share reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable, Mapping, Sequence

from allocation.auction.state import AuctionResult
from allocation.config import Config
from allocation.contracts import AgentKind, Candidate, QAction, ReentryTrigger
from allocation.pathway.reentry import AvailabilityReader, News2Reader, ReentryRegistry


class Standing(str, Enum):
    """Where a candidate is between auctions."""

    #: Bidding in the next auction.
    ACTIVE = "active"
    #: Has a bed, here or elsewhere. Out of the queue.
    RESOLVED = "resolved"
    #: Out of the queue until a monitor fires.
    MONITORED = "monitored"
    #: Waiting on a predicted release; bids again next auction.
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class Participation:
    """One candidate's standing, and why."""

    candidate_id: str
    agent: AgentKind
    standing: Standing
    reason: str
    since: datetime
    #: How many auctions this candidate has bid in and not been placed by.
    attempts: int = 0
    #: True when the last exit arranged nothing. Counted separately from ``attempts`` because
    #: losing an auction to a sicker patient and being abandoned are not the same event.
    abandoned_last: bool = False

    @property
    def bidding(self) -> bool:
        return self.standing in (Standing.ACTIVE, Standing.DEFERRED)


@dataclass
class ParticipationLedger:
    """Candidate standings across a session.

    Holds the :class:`ReentryRegistry` rather than sitting beside it, because the two answer
    halves of one question: the registry knows *whether* a monitor fired, this knows *what that
    means for the next auction*. Split across two callers they drift — a fired trigger that
    nobody translates back into a bidder is a monitor that technically worked and changed
    nothing.
    """

    config: Config
    reentry: ReentryRegistry
    _state: dict[str, Participation] = field(default_factory=dict)
    _abandonments: int = 0

    @classmethod
    def for_candidates(
        cls, config: Config, candidates: Sequence[Candidate], now: datetime
    ) -> "ParticipationLedger":
        ledger = cls(config=config, reentry=ReentryRegistry(config))
        for candidate in candidates:
            ledger._state[candidate.candidate_id] = Participation(
                candidate_id=candidate.candidate_id,
                agent=candidate.agent,
                standing=Standing.ACTIVE,
                reason="initial cohort",
                since=now,
            )
        return ledger

    def admit(self, candidate: Candidate, now: datetime, reason: str = "arrived") -> None:
        """Add a newly-arrived candidate to the bidding pool.

        Idempotent: re-admitting somebody already tracked is ignored rather than resetting
        their standing. A patient monitored in HDU who appears again in the arrival stream
        must not be silently returned to the queue — that would defeat the monitor.
        """
        if candidate.candidate_id in self._state:
            return
        self._set(candidate.candidate_id, candidate.agent, Standing.ACTIVE, reason, now)

    # -- who bids next -----------------------------------------------------------------

    def bidders(
        self,
        pool: Sequence[Candidate],
        now: datetime,
        news2: News2Reader | None = None,
        available: AvailabilityReader | None = None,
    ) -> tuple[Candidate, ...]:
        """The candidates eligible for the auction opening at ``now``.

        Monitors are tested first, so a patient who deteriorated in HDU since the last auction
        is back in this one rather than the next. That ordering is the whole value of
        ``RE_ENTER_LATER``: a monitor consulted after the auction opened would always be one
        bed late, which is indistinguishable from not having a monitor.
        """
        for trigger in self.reentry.due(now, news2, available):
            self._reactivate(trigger, now)

        for lapsed in self.reentry.lapsed:
            current = self._state.get(lapsed.candidate_id)
            if current is not None and current.standing is Standing.MONITORED:
                # The monitor expired without firing. The patient is still unplaced, so they
                # return to the pool — silently dropping them is how a "temporary" exit
                # becomes a permanent one.
                self._state[lapsed.candidate_id] = Participation(
                    candidate_id=current.candidate_id,
                    agent=current.agent,
                    standing=Standing.ACTIVE,
                    reason="re-entry monitor expired without firing",
                    since=now,
                    attempts=current.attempts,
                    abandoned_last=current.abandoned_last,
                )

        return tuple(
            c for c in pool
            if (state := self._state.get(c.candidate_id)) is not None and state.bidding
        )

    # -- recording an auction ----------------------------------------------------------

    def record(self, result: AuctionResult, now: datetime) -> None:
        """Update every participant's standing from a closed auction.

        Reads the **last** bid each agent made, because that is the decision that ended their
        participation. An agent that raised twice and then withdrew to HDU is resolved by the
        withdrawal, not by the raises.
        """
        winner = result.winner
        for agent, position in result.positions.items():
            candidate_id = position.candidate_id
            previous = self._state.get(candidate_id)
            attempts = (previous.attempts if previous else 0) + 1

            if winner is not None and agent is winner:
                self._set(candidate_id, agent, Standing.RESOLVED, "won the auction", now,
                          attempts=attempts)
                self.reentry.disarm(candidate_id)
                continue

            decision = self._last_decision(result, agent)
            if decision is None:
                self._set(candidate_id, agent, Standing.ACTIVE, "lost, still unplaced", now,
                          attempts=attempts)
                continue

            q_action, plan = decision
            self._apply(candidate_id, agent, q_action, plan, now, attempts)

    def _apply(self, candidate_id, agent, q_action, plan, now, attempts) -> None:
        if q_action is QAction.WITHDRAW_ALTERNATIVE:
            unit = plan.target_unit if plan else "elsewhere"
            self._set(candidate_id, agent, Standing.RESOLVED,
                      f"placed in {unit} for the allocation horizon", now, attempts=attempts)
            self.reentry.disarm(candidate_id)
            return

        if q_action is QAction.RE_ENTER_LATER and plan is not None and plan.reentry is not None:
            self.reentry.arm(plan.reentry)
            where = plan.reentry.holding_unit or "in place"
            self._set(candidate_id, agent, Standing.MONITORED,
                      f"monitored {where} until {plan.reentry.expires_at:%H:%M}", now,
                      attempts=attempts)
            return

        if q_action is QAction.AWAIT_NEXT_RESOURCE:
            probability = plan.release_probability if plan else None
            self._set(candidate_id, agent, Standing.DEFERRED,
                      f"waiting on a predicted release (p={probability:.2f})"
                      if probability is not None else "waiting on a predicted release",
                      now, attempts=attempts)
            return

        if q_action is QAction.WITHDRAW_UNPLANNED:
            self._abandonments += 1
            self._set(candidate_id, agent, Standing.ACTIVE,
                      "withdrew with nothing arranged", now,
                      attempts=attempts, abandoned=True)
            return

        # Still competing when the auction closed, and did not win.
        self._set(candidate_id, agent, Standing.ACTIVE, "lost, still unplaced", now,
                  attempts=attempts)

    # -- reporting ---------------------------------------------------------------------

    @property
    def abandoned(self) -> int:
        """How many times a candidate left an auction with nothing arranged.

        **The metric this whole layer exists to expose.** Burn rate, win share and ranking
        respect are all silent about it: an auction where the loser was moved safely to HDU and
        one where the loser was left in a corridor produce identical numbers in every one of
        them. A rising count here is the mechanism reporting that it is rationing past what the
        alternatives can absorb.
        """
        return self._abandonments

    @property
    def unresolved(self) -> tuple[Participation, ...]:
        """Candidates still waiting for a bed, longest-waiting first."""
        return tuple(
            sorted(
                (p for p in self._state.values() if p.bidding),
                key=lambda p: -p.attempts,
            )
        )

    def standing_of(self, candidate_id: str) -> Participation | None:
        return self._state.get(candidate_id)

    def counts(self) -> Mapping[Standing, int]:
        out: dict[Standing, int] = {s: 0 for s in Standing}
        for state in self._state.values():
            out[state.standing] += 1
        return out

    # -- internals ---------------------------------------------------------------------

    def _set(
        self,
        candidate_id: str,
        agent: AgentKind,
        standing: Standing,
        reason: str,
        now: datetime,
        attempts: int = 0,
        abandoned: bool = False,
    ) -> None:
        self._state[candidate_id] = Participation(
            candidate_id=candidate_id,
            agent=agent,
            standing=standing,
            reason=reason,
            since=now,
            attempts=attempts,
            abandoned_last=abandoned,
        )

    def _reactivate(self, trigger: ReentryTrigger, now: datetime) -> None:
        previous = self._state.get(trigger.candidate_id)
        self._set(
            trigger.candidate_id, trigger.agent, Standing.ACTIVE,
            "re-entry monitor fired", now,
            attempts=previous.attempts if previous else 0,
        )

    @staticmethod
    def _last_decision(result: AuctionResult, agent: AgentKind):
        """The final ``(q_action, plan)`` this agent recorded, or ``None``.

        ``None`` when the auction ran under a policy that emits no Q-action at all. Treated as
        "still competing" rather than as an exit, because a three-action policy's withdrawal
        arranged nothing and should not be promoted to a strategic one.
        """
        bids = result.bids_for(agent)
        for bid in reversed(bids):
            if bid.q_action is not None:
                return bid.q_action, bid.plan
        return None
