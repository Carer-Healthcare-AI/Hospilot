"""Assembling what the three exits need, once per candidate per round.

This is the object handed to :meth:`StrategicPolicy.decide_q`. It exists so that the policy
**chooses among what it is given and never looks anything up** — the same discipline that makes
``FeatureSnapshot`` one immutable read per round. A policy that queried HDU itself would be a
second, unsynchronised read of the world, and it could contradict the Alternative Availability
component that already scored that same unit into the utility it is bidding on.

Built per candidate, not per auction, because two of the three fields are patient-specific:
alternatives depend on the patient's care needs, and the release probability is scoped to *this*
patient's safe waiting window. Only the underlying discharge rate is shared.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Mapping, Sequence

from allocation.config import Config
from allocation.contracts import (
    AlternativeOption,
    Candidate,
    FeatureSnapshot,
    ReentryTrigger,
    ResourceType,
)
from allocation.features import news2 as news2_features
from allocation.pathway.alternatives import UnitReader, available_alternatives, best_usable
from allocation.pathway.forecast import NextRelease, next_release


@dataclass(frozen=True, slots=True)
class RoundOptions:
    """Concrete :class:`~allocation.contracts.PathwayOptions` for one candidate in one round."""

    alternatives: tuple[AlternativeOption, ...]
    next_release_at: datetime | None
    next_release_probability: float | None
    safe_wait_minutes: float | None
    #: The full derivation behind the release estimate, including the assumption it rests on.
    release: NextRelease | None = None
    #: NEWS2 at this snapshot, the baseline a re-entry monitor is armed against. ``None`` when
    #: the patient's vitals cannot be scored — which does not block ``RE_ENTER_LATER``, it
    #: only removes the deterioration condition and leaves the availability one.
    baseline_news2: float | None = None
    #: Builds the trigger for ``RE_ENTER_LATER``. A factory rather than a pre-built object
    #: because the holding unit is the policy's choice, and rather than letting the policy
    #: construct one itself because the TTL, the rise threshold and the baseline are all
    #: world-reads that belong on this side of the seam. Returns ``None`` when no condition
    #: can be armed at all — a monitor with nothing to watch is a plain withdrawal.
    make_reentry: Callable[[str | None], ReentryTrigger | None] | None = None

    @property
    def best_alternative(self) -> AlternativeOption | None:
        """The best option that is both clinically adequate and confirmed open."""
        return best_usable(self.alternatives)

    @property
    def has_alternative(self) -> bool:
        return self.best_alternative is not None

    def alternative_note(self) -> str:
        """Why there is no usable alternative, when there is none.

        Three states that look identical in a boolean and are not: nothing was offered, what
        was offered cannot meet the patient's needs, and what was offered is full. Only the
        third is a reason to expect the action to become available again later.
        """
        if not self.alternatives:
            return "no alternative unit offered for this patient"
        if self.best_alternative is not None:
            return ""
        unmet = [o.unit for o in self.alternatives if o.capability_gap]
        unread = [o.unit for o in self.alternatives if o.available is None]
        full = [o.unit for o in self.alternatives if o.available is False]
        parts = []
        if full:
            parts.append(f"full: {', '.join(full)}")
        if unmet:
            parts.append(f"cannot meet care needs: {', '.join(unmet)}")
        if unread:
            parts.append(f"occupancy unread: {', '.join(unread)}")
        return "; ".join(parts)


def build_options(
    config: Config,
    candidate: Candidate,
    snapshot: FeatureSnapshot,
    target_unit: str,
    horizon_hours: float,
    resource_type: ResourceType | None = None,
    reader: UnitReader | None = None,
) -> RoundOptions:
    """Everything the exits need for one candidate, from one snapshot."""
    data = snapshot.for_candidate(candidate.candidate_id)

    alternatives = available_alternatives(
        config, candidate, data, target_unit, horizon_hours, reader=reader
    )
    alternatives = _drop_too_brief(config, alternatives)

    wait = safe_wait_minutes(config, candidate, horizon_hours)
    release = (
        next_release(config, snapshot.hospital, snapshot.taken_at, wait)
        if wait is not None
        else None
    )
    baseline = current_news2(config, snapshot, candidate)

    return RoundOptions(
        alternatives=alternatives,
        next_release_at=release.expected_at if release else None,
        next_release_probability=release.probability if release else None,
        safe_wait_minutes=wait,
        release=release,
        baseline_news2=baseline,
        make_reentry=_reentry_factory(
            config, candidate, snapshot.taken_at, baseline,
            resource_type or ResourceType(f"{target_unit}_bed"),
        ),
    )


def current_news2(
    config: Config, snapshot: FeatureSnapshot, candidate: Candidate
) -> float | None:
    """NEWS2 from the latest reading in this snapshot, or ``None``.

    The same scorer the Urgency component uses, over the same snapshot, so the baseline a
    monitor is armed against is the number the utility was priced on. Computing it separately
    would let the trigger and the bid disagree about the patient.
    """
    data = snapshot.for_candidate(candidate.candidate_id)
    if not data.vitals:
        return None
    latest = max(data.vitals, key=lambda v: v.recorded_at)
    return news2_features.score_reading(latest, config.threshold("news2_bands")).points


def _reentry_factory(
    config: Config,
    candidate: Candidate,
    now: datetime,
    baseline_news2: float | None,
    resource_type: ResourceType,
) -> Callable[[str | None], ReentryTrigger | None]:
    """Close over everything a trigger needs except the holding unit."""
    cfg = config.rule("pathway")["reentry"]

    def make(holding_unit: str | None) -> ReentryTrigger | None:
        if not bool(cfg.get("enabled", True)):
            return None
        if holding_unit is None and not bool(cfg.get("allow_without_holding_unit", True)):
            return None

        rise = float(cfg["news2_rise"]) if baseline_news2 is not None else None
        on_lost = bool(cfg["on_alternative_lost"]) and holding_unit is not None
        if rise is None and not on_lost:
            # Nothing to watch. ReentryTrigger would refuse to construct, and returning None
            # lets the policy fall through to WITHDRAW_UNPLANNED rather than record a monitor
            # that could never fire.
            return None

        return ReentryTrigger(
            candidate_id=candidate.candidate_id,
            agent=candidate.agent,
            resource_type=resource_type,
            armed_at=now,
            expires_at=now + timedelta(minutes=float(cfg["ttl_minutes"])),
            news2_rise=rise,
            on_alternative_lost=on_lost,
            baseline_news2=baseline_news2,
            holding_unit=holding_unit,
        )

    return make


def safe_wait_minutes(
    config: Config, candidate: Candidate, horizon_hours: float
) -> float | None:
    """How long this patient can safely wait where they currently are.

    From ``rules/units.yaml`` ``safe_hold_hours``, keyed on the patient's *current* unit — the
    same table that says how long an alternative could hold them, asked about where they
    already are. RL-Steps states waiting windows as narrative facts ("its safe waiting window
    is 45 min", "patient can safely wait ~2 hours") with no rule behind them; this is the only
    table in the system that answers the question at all.

    ``None`` when the current unit is unrecorded or has no B.4 row. That makes
    ``AWAIT_NEXT_RESOURCE`` infeasible for that patient, which is right: waiting is a claim
    about safety, and it must not be available by default to a patient nobody can vouch for.

    Capped at the allocation horizon. A ward row promising 24 hours does not make waiting
    beyond the window this bed is being allocated over a meaningful choice — the auction has
    no representation for a patient who waits past every bed in the horizon.
    """
    unit = candidate.current_unit
    if not unit:
        return None
    table: Mapping[str, Mapping[str, object]] = config.rule("units")["safe_hold_hours"]
    row = table.get(unit)
    if row is None:
        return None
    hours = float(row["default_hours"])  # type: ignore[arg-type]
    if hours <= 0:
        return 0.0
    return min(hours, horizon_hours) * 60.0


def _drop_too_brief(
    config: Config, options: Sequence[AlternativeOption]
) -> tuple[AlternativeOption, ...]:
    """Remove alternatives whose safe-hold window is too short to be worth the move.

    A bay that can hold a patient for ten minutes is not a care pathway; withdrawing into it
    buys a lost auction and an immediate re-auction. The threshold is
    ``rules/pathway.yaml`` ``alternative.min_safe_hold_minutes`` and is unsigned like the rest
    of that file.
    """
    floor = float(config.rule("pathway")["alternative"]["min_safe_hold_minutes"])
    return tuple(o for o in options if o.safe_hold_minutes >= floor)
