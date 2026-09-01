"""The standing monitor behind *Q(Re-enter Later)*.

RL-Steps: *"Withdraw temporarily but continuously monitor conditions and automatically enter a
future auction when circumstances change."*

**The word doing the work is "automatically".** Every other action in the framework completes
inside one auction; this one does not complete at all until some later auction opens. So it
needs something that outlives the auction, holds the condition, and is consulted when the next
bed is released. That is this registry, and its absence is why ``RE_ENTER_LATER`` could not have
been implemented as a variant of ``WITHDRAW``: there was nowhere for the trigger to live.

Where it is consulted::

    auction N     policy chooses RE_ENTER_LATER  ->  registry.arm(trigger)
    ...           patient sits in HDU, deteriorating
    auction N+1   registry.due(now, ...)         ->  candidate re-enters as a bidder

**A trigger that never fires is worse than no trigger**, because it is recorded as a temporary
exit and behaves as a permanent one. Two guards:

* Every trigger expires (``rules/pathway.yaml`` ``reentry.ttl_minutes``). Physiology a monitor
  was armed against is stale within hours, exactly as utilities carry TTLs.
* :meth:`ReentryRegistry.expired` is a first-class result, not a silent drop. A patient whose
  monitor lapsed without ever re-entering is a patient the system quietly stopped tracking, and
  that has to be visible — it is the failure mode this action introduces.

The registry is in-memory and per-session. Production needs it durable for the same reason
``PendingObservation`` is durable: a restart between two auctions must not silently disarm every
monitor in the hospital. Left as a seam rather than pretended solved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Mapping

from allocation.config import Config
from allocation.contracts import AgentKind, Candidate, ReentryTrigger, ResourceType

#: Current NEWS2 for a candidate, or ``None`` when it cannot be computed. ``None`` must not
#: fire the trigger: "we cannot see the patient" is not "the patient deteriorated".
News2Reader = Callable[[str], float | None]

#: Is this unit still available? ``None`` when nobody looked — again not a firing condition.
AvailabilityReader = Callable[[str], bool | None]


@dataclass(frozen=True, slots=True)
class ReentryCheck:
    """Why a monitor did or did not re-open bidding for one candidate."""

    trigger: ReentryTrigger
    fired: bool
    reason: str
    #: NEWS2 at the moment of the check, when it could be read.
    news2: float | None = None

    @property
    def candidate_id(self) -> str:
        return self.trigger.candidate_id


@dataclass
class ReentryRegistry:
    """Armed monitors, keyed by candidate.

    One monitor per candidate: re-arming replaces rather than accumulates. Two live triggers
    for the same patient would let a candidate re-enter one auction twice, which the auction
    layer has no representation for — a :class:`Position` is per agent.
    """

    config: Config
    _armed: dict[str, ReentryTrigger] = field(default_factory=dict)
    _lapsed: list[ReentryTrigger] = field(default_factory=list)

    # -- arming ------------------------------------------------------------------------

    def build_trigger(
        self,
        candidate: Candidate,
        resource_type: ResourceType,
        now: datetime,
        baseline_news2: float | None,
        holding_unit: str | None = None,
    ) -> ReentryTrigger:
        """A trigger from the configured defaults.

        ``baseline_news2`` may be ``None`` — a patient whose vitals cannot be scored can still
        be monitored on the availability condition alone. If neither condition can be set,
        :class:`ReentryTrigger` refuses to construct, which is the correct outcome: there is
        nothing to watch, so the exit is a plain withdrawal and must be recorded as one.
        """
        cfg = self.config.rule("pathway")["reentry"]
        rise = float(cfg["news2_rise"]) if baseline_news2 is not None else None
        return ReentryTrigger(
            candidate_id=candidate.candidate_id,
            agent=candidate.agent,
            resource_type=resource_type,
            armed_at=now,
            expires_at=now + timedelta(minutes=float(cfg["ttl_minutes"])),
            news2_rise=rise,
            on_alternative_lost=bool(cfg["on_alternative_lost"]) and holding_unit is not None,
            baseline_news2=baseline_news2,
            holding_unit=holding_unit,
        )

    def arm(self, trigger: ReentryTrigger) -> None:
        self._armed[trigger.candidate_id] = trigger

    def disarm(self, candidate_id: str) -> None:
        """Remove a monitor — used when the candidate re-enters or is otherwise resolved."""
        self._armed.pop(candidate_id, None)

    # -- checking ----------------------------------------------------------------------

    def check(
        self,
        now: datetime,
        news2: News2Reader | None = None,
        available: AvailabilityReader | None = None,
    ) -> tuple[ReentryCheck, ...]:
        """Test every armed monitor. Expired ones are retired and reported as such.

        Ordering matters: expiry is tested first. A monitor that has lapsed must not fire on
        a condition read after its window, because the decision to re-enter would be made
        against physiology the trigger was never scoped to.
        """
        results: list[ReentryCheck] = []
        for trigger in tuple(self._armed.values()):
            if now >= trigger.expires_at:
                self._armed.pop(trigger.candidate_id, None)
                self._lapsed.append(trigger)
                results.append(
                    ReentryCheck(
                        trigger, False,
                        f"monitor expired at {trigger.expires_at:%H:%M} without firing",
                    )
                )
                continue
            results.append(self._test(trigger, news2, available))
        return tuple(results)

    def due(
        self,
        now: datetime,
        news2: News2Reader | None = None,
        available: AvailabilityReader | None = None,
    ) -> tuple[ReentryTrigger, ...]:
        """Monitors that fired. Each is disarmed — it has done its job."""
        fired = tuple(c.trigger for c in self.check(now, news2, available) if c.fired)
        for trigger in fired:
            self.disarm(trigger.candidate_id)
        return fired

    def _test(
        self,
        trigger: ReentryTrigger,
        news2: News2Reader | None,
        available: AvailabilityReader | None,
    ) -> ReentryCheck:
        current: float | None = None

        if trigger.news2_rise is not None and news2 is not None:
            current = news2(trigger.candidate_id)
            base = trigger.baseline_news2
            if current is not None and base is not None:
                if current - base >= trigger.news2_rise:
                    return ReentryCheck(
                        trigger, True,
                        f"NEWS2 rose {current - base:+.1f} from {base:g} to {current:g} "
                        f"(threshold {trigger.news2_rise:g})",
                        news2=current,
                    )

        if trigger.on_alternative_lost and available is not None and trigger.holding_unit:
            open_now = available(trigger.holding_unit)
            if open_now is False:
                return ReentryCheck(
                    trigger, True,
                    f"holding unit {trigger.holding_unit} is no longer available",
                    news2=current,
                )

        return ReentryCheck(trigger, False, "no condition met", news2=current)

    # -- visibility --------------------------------------------------------------------

    @property
    def armed(self) -> Mapping[str, ReentryTrigger]:
        return dict(self._armed)

    @property
    def lapsed(self) -> tuple[ReentryTrigger, ...]:
        """Monitors that expired without firing.

        Read this. A rising count means patients are being parked under
        ``RE_ENTER_LATER`` and then forgotten, which looks identical to a safe deferral in
        every other metric the system reports.
        """
        return tuple(self._lapsed)

    def candidates_for(
        self, triggers: Iterable[ReentryTrigger], pool: Mapping[str, Candidate]
    ) -> tuple[Candidate, ...]:
        """Resolve fired triggers back to candidates, dropping any no longer in the pool.

        A candidate that has left the pool has been discharged, transferred or otherwise
        resolved between auctions. Re-entering them would bid for a patient who is no longer
        there.
        """
        return tuple(pool[t.candidate_id] for t in triggers if t.candidate_id in pool)

    def agents_for(self, triggers: Iterable[ReentryTrigger]) -> frozenset[AgentKind]:
        return frozenset(t.agent for t in triggers)
