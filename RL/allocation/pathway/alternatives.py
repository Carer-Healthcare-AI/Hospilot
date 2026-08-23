"""Which other units could take this patient, and are any of them actually open.

The ``+ Alternative`` half of *Q(Withdraw + Alternative)*. Two questions, and they are not the
same question:

**Is it clinically acceptable?** Answered from the same two tables the Alternative Availability
component reads — ``capability`` (B.7) and ``safe_hold_hours`` (B.4) in ``rules/units.yaml``,
and the ``care_ladder`` that decides what counts as a fallback at all. This module reuses
:class:`~allocation.utility.components.alternative.Alternative` rather than re-implementing
them: two readings of the same tables would eventually disagree, and the disagreement would be
between the utility that priced the bid and the exit that abandoned it.

**Is there a free bed in it?** Answered by reading that unit's own :class:`HospitalState`, which
means a second call to the ``DataSource``. ``FeatureSnapshot`` cannot supply it — a snapshot
describes *the unit being auctioned* and that is deliberate (``DataSource.hospital_state`` takes
a unit precisely so an HDU auction stops reading ICU's 20/20).

**Unknown is not open.** With no reader wired, availability is ``None`` and
``AlternativeOption.usable`` is False, so ``WITHDRAW_ALTERNATIVE`` is infeasible and the policy
cannot choose it. That is the correct production behaviour today and it is not a placeholder:
the failure mode of the other convention — absent meaning free — is a patient withdrawn from
an ICU auction into a full HDU, which is the one outcome this action exists to avoid.

An escalation is never an alternative. Auction an HDU bed and ICU sits above it on the ladder;
withdrawing a patient "to" a scarcer bed they cannot get is not a fallback, and the ladder rule
that removes it here is the same one the utility component applies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Mapping, Sequence

from allocation.config import Config
from allocation.contracts import (
    AlternativeOption,
    Candidate,
    CareNeed,
    DataSource,
    HospitalState,
    PatientData,
)
from allocation.utility.components.alternative import Alternative

#: Reads another unit's bed state, or returns ``None`` when it cannot describe that unit.
#: Deliberately returns ``None`` rather than raising: an alternative unit the data source has
#: never heard of is a gap in coverage, not a failed auction.
UnitReader = Callable[[str], HospitalState | None]


def unit_reader(source: DataSource, at: datetime) -> UnitReader:
    """A :data:`UnitReader` over a ``DataSource``, pinned to one instant.

    Pinned because every alternative in one round must be judged against the same moment. A
    reader that used "now" would let the first unit checked and the last disagree about how
    many beds the hospital had, and the exit would be chosen against a world that never existed.

    ``hospital_state`` raises for a unit it cannot describe — that is its contract, and the
    right one, since a substituted unit's occupancy is worse than none. Here it becomes
    ``None``: this is an optional enrichment of a decision, not the decision itself.
    """
    from allocation.ingest.loop import run as run_async

    cache: dict[str, HospitalState | None] = {}

    def read(unit: str) -> HospitalState | None:
        if unit not in cache:
            try:
                cache[unit] = run_async(source.hospital_state(unit, at))
            except Exception:
                # Includes the deliberate raise for an undescribable unit. Availability stays
                # None, which keeps the alternative unusable rather than assumed free.
                cache[unit] = None
        return cache[unit]

    return read


def available_alternatives(
    config: Config,
    candidate: Candidate,
    data: PatientData,
    target_unit: str,
    horizon_hours: float,
    reader: UnitReader | None = None,
) -> tuple[AlternativeOption, ...]:
    """Every fallback unit for this patient, best first.

    "Best" orders by capability first and safe-hold duration second: a unit that meets every
    need for two hours is a better place to send someone than one that meets half of them for
    a day. Unusable options are kept in the list rather than filtered out, because
    :attr:`Decision.feasible` needs to record that they were considered — an evaluation that
    cannot tell "no alternative existed" from "the alternative was full" cannot explain why a
    policy stopped choosing this action.
    """
    component = Alternative(config, horizon_hours, target_unit=target_unit)
    ladder = tuple(str(u) for u in config.rule("units").get("care_ladder", ()))
    capability: Mapping[str, Sequence[str]] = config.rule("units")["capability"]
    hold_table: Mapping[str, Mapping[str, object]] = config.rule("units")["safe_hold_hours"]

    options: list[AlternativeOption] = []
    for unit in _offered_units(component, data):
        if unit == target_unit:
            continue  # a unit is not an alternative to itself
        if _is_escalation(ladder, unit, target_unit):
            continue  # a scarcer bed is not a fallback — units.yaml's own rule
        if unit not in hold_table or unit not in capability:
            continue  # no B.4/B.7 row: nothing to promise about this unit

        gap = _capability_gap(candidate.needs, capability[unit])
        hours = float(hold_table[unit]["default_hours"])  # type: ignore[index]
        options.append(
            AlternativeOption(
                unit=unit,
                safe_hold_minutes=hours * 60.0,
                available=_availability(unit, data, reader),
                capability_gap=gap,
                source="rules.units" + ("" if reader else " (occupancy unread)"),
            )
        )

    options.sort(key=lambda o: (len(o.capability_gap), -o.safe_hold_minutes, o.unit))
    return tuple(options)


def best_usable(options: Sequence[AlternativeOption]) -> AlternativeOption | None:
    """The first option that is both clinically adequate and confirmed open."""
    return next((o for o in options if o.usable), None)


# -- internals ---------------------------------------------------------------------------


def _offered_units(component: Alternative, data: PatientData) -> tuple[str, ...]:
    """The candidate units, classified through the same matcher the utility uses.

    ``alternative_units`` is the list; ``best_alternative_unit`` is the older single-value
    shorthand. Both are read, in that order, exactly as the component does.
    """
    raw: Sequence[str | None] = tuple(data.alternative_units or ())
    if not raw:
        raw = (data.best_alternative_unit,)

    seen: list[str] = []
    for ward in raw:
        unit = component.classify_unit(ward)
        if unit is not None and unit not in seen:
            seen.append(unit)
    return tuple(seen)


def _is_escalation(ladder: Sequence[str], unit: str, target_unit: str) -> bool:
    """True when ``unit`` sits above ``target_unit`` on the care ladder.

    A unit missing from the ladder is not treated as an escalation, matching the utility
    component: an unlisted unit is a config gap, and a config gap must not silently delete an
    option.
    """
    if unit not in ladder or target_unit not in ladder:
        return False
    return ladder.index(unit) < ladder.index(target_unit)


def _capability_gap(needs: frozenset[CareNeed], provides: Sequence[str]) -> frozenset[CareNeed]:
    """Needs this unit cannot meet.

    A patient with no recorded needs produces an empty gap, which reads as "nothing unmet".
    That is the one place this differs from the utility component, which returns an absent
    Signal instead — and the difference is deliberate. The component is computing a score,
    where absent must never become zero. Here the question is whether a bed is safe to move a
    patient to, and the *availability* tri-state already carries the "we do not know" case.
    Duplicating unknown-ness across two fields would make ``usable`` untestable.
    """
    if not needs:
        return frozenset()
    return frozenset(needs - {CareNeed(c) for c in provides})


def _availability(unit: str, data: PatientData, reader: UnitReader | None) -> bool | None:
    """Is there a free bed in ``unit``? ``None`` when nobody looked.

    PACU is special-cased because it is the one alternative with a purpose-built signal:
    ``PatientData.pacu_capacity_probability`` comes from the forecast layer, and preferring a
    live bed count over it would discard the better input. Above 0.5 is read as available —
    a stated cut, not a fitted one, and it belongs with the other unsigned pathway constants.
    """
    if unit == "pacu" and data.pacu_capacity_probability is not None:
        return data.pacu_capacity_probability > 0.5

    if reader is None:
        return None
    state = reader(unit)
    if state is None:
        return None
    return state.unit_occupied_beds < state.unit_total_beds
