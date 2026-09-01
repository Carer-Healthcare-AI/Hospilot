"""Scoring an auction after the fact. RL-Steps sections 23-24.

*"The auction result itself does not tell RL whether its policy was good. The hospital must
observe what happened afterward."*

Two structural consequences that shape everything downstream:

**The reward arrives hours late.** An auction closing at 13:06 is scored at 17:06. So the
training pipeline is inherently batch, and an episode exists in an unscored state for most of
its life. :class:`PendingObservation` is that state, and it is durable — a process restart in
between must not lose it.

**Most episodes will be incomplete, and that is not a bug to route around.** ``no_mortality``
has no source (F-01), so ``complete`` is False on every episode until a disposition field
exists. The correct response is to keep them out of the training set, not to impute the term.
Imputing it as "no death" makes the policy optimistic about exactly the outcome it is supposed
to be avoiding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Protocol

from allocation.audit.records import OutcomeRow
from allocation.config import Config
from allocation.reward.terms import horizon_hours, load_terms

#: Handled outside the observation map — see :func:`score`.
MORTALITY_TERM = "no_mortality"


@dataclass(frozen=True, slots=True)
class PendingObservation:
    """An auction awaiting its outcome window.

    Persisted, not held in memory: the gap between close and observation is hours, and an
    episode lost to a restart is an episode that cannot be recovered — the hospital state that
    would have been read has moved on.
    """

    auction_id: str
    closed_at: datetime
    horizon_hours: float

    @property
    def due_at(self) -> datetime:
        return self.closed_at + timedelta(hours=self.horizon_hours)

    def is_due(self, now: datetime) -> bool:
        return now >= self.due_at


class ObservationSource(Protocol):
    """Reads what actually happened in the window after an auction closed.

    Returns ``{term_name: True | False | None}``. ``None`` means *not known* — the caller must
    never substitute a default. Eight of the nine section 23 terms map to live tables; the
    ninth is F-01.
    """

    async def observe(
        self, auction_id: str, closed_at: datetime, horizon_hours: float
    ) -> Mapping[str, bool | None]: ...


def score(
    config: Config,
    auction_id: str,
    observations: Mapping[str, bool | None],
    observed_at: datetime | None = None,
    mortality_observed: bool | None = None,
    mortality_source: str | None = None,
) -> OutcomeRow:
    """Turn observations into an :class:`OutcomeRow`.

    Unknown terms are excluded from the total and listed in ``missing_terms``. An observation
    for a term that is not configured raises rather than being silently dropped — a typo in a
    term name would otherwise quietly zero part of the objective.
    """
    terms = load_terms(config)

    unknown = sorted(set(observations) - set(terms))
    if unknown:
        raise KeyError(
            f"observations for terms not in reward.yaml: {unknown}. "
            "A misspelt term would silently remove points from the objective."
        )

    awarded: dict[str, float] = {}
    missing: list[str] = []

    for name, term in terms.items():
        # Mortality does not come through the observation map. It is the term that decides
        # the sign of the episode, so it takes an explicit, separately-sourced argument and
        # can never be set by something that merely forgot to mention it.
        if name == MORTALITY_TERM:
            continue

        seen = observations.get(name)
        if seen is None:
            # A term is only *missing* if it could have applied. A theatre term on an auction
            # with no surgical bidder is not an observation gap, so silence about a term that
            # was never passed in is not counted against the episode.
            if name in observations:
                missing.append(name)
            continue
        if seen:
            awarded[name] = term.points

    if mortality_observed is None:
        missing.append(MORTALITY_TERM)
    elif mortality_observed is True:
        awarded[MORTALITY_TERM] = terms[MORTALITY_TERM].points

    missing = sorted(set(missing))

    return OutcomeRow(
        auction_id=auction_id,
        observed_at=observed_at or datetime.now(tz=None).astimezone(),
        horizon_hours=horizon_hours(config),
        terms=awarded,
        reward_total=sum(awarded.values()),
        mortality_observed=mortality_observed,
        mortality_source=mortality_source,
        complete=not missing,
        missing_terms=tuple(missing),
    )


def pending_for(config: Config, auction_id: str, closed_at: datetime) -> PendingObservation:
    return PendingObservation(
        auction_id=auction_id, closed_at=closed_at, horizon_hours=horizon_hours(config)
    )


def due(pending: tuple[PendingObservation, ...], now: datetime) -> tuple[PendingObservation, ...]:
    return tuple(p for p in pending if p.is_due(now))
