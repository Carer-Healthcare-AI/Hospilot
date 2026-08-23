"""Turning snapshots into JSON that can be read back years later.

Two rules, both learned from what makes old logs useless:

**Absent stays absent.** A :class:`~allocation.contracts.Signal` with no value serialises to
``{"value": null, "source": ..., "note": ...}``, never to ``0.0`` and never to a dropped key.
The note travels with it, so a reader in 2028 can tell "no oxygen flag existed then" from "the
patient was on room air".

**Nothing is lost to a type.** Enums serialise by value, datetimes to ISO 8601 with offset,
frozensets to sorted lists so two identical snapshots compare equal.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping

from allocation.contracts import FeatureSnapshot, PathwayPlan, Signal, UtilityBreakdown


def jsonable(value: Any) -> Any:
    """Recursively convert to JSON-safe primitives."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(jsonable(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: jsonable(getattr(value, f.name)) for f in fields(value)}
    return str(value)


def signal_to_dict(signal: Signal) -> dict[str, Any]:
    """A signal, with its absence preserved rather than flattened."""
    return {"value": signal.value, "source": signal.source, "note": signal.note}


def hospital_state(snapshot: FeatureSnapshot) -> dict[str, Any]:
    state = snapshot.hospital
    return {
        **jsonable(state),
        # Derived properties are stored too: a reader should not have to know the formula for
        # occupancy to interpret the row.
        "occupancy": state.occupancy,
        "isolation_pressure": state.isolation_pressure,
    }


def patient_data(snapshot: FeatureSnapshot) -> dict[str, Any]:
    """Every raw row that fed a utility, per candidate."""
    return {cid: jsonable(data) for cid, data in snapshot.patients.items()}


def factor_signals(breakdown: Mapping[str, UtilityBreakdown]) -> dict[str, Any]:
    """Per candidate, per component, every factor with its weight, value and provenance."""
    out: dict[str, Any] = {}
    for candidate_id, utility in breakdown.items():
        components: dict[str, Any] = {}
        for component in utility.components:
            components[component.component.value] = {
                "cap": component.cap,
                "points": component.points,
                "coverage": component.coverage,
                "factors": {
                    factor.name: {"weight": factor.weight, **signal_to_dict(factor.signal)}
                    for factor in component.factors
                },
            }
        out[candidate_id] = {"total": utility.total, "components": components}
    return out


def component_points(utility: UtilityBreakdown) -> dict[str, float]:
    return {c.component.value: c.points for c in utility.components}


def component_coverage(utility: UtilityBreakdown) -> dict[str, float]:
    return {c.component.value: c.coverage for c in utility.components}


def pathway_plan(plan: "PathwayPlan | None") -> dict[str, Any]:
    """Flatten what an exit committed to, for the bid row.

    Empty for a non-exit and for ``WITHDRAW_UNPLANNED`` — the two cases that arranged nothing.
    That emptiness is load-bearing: the reward observer reads this column to decide whether an
    arranged-care term may be awarded at all, so an exit that promised nothing must serialise
    to nothing rather than to a row of nulls that could be mistaken for an unread plan.

    The re-entry trigger is flattened rather than nested because every other column here is
    flat, and because the two facts worth querying later — did a monitor get armed, and did it
    ever fire — are answerable only if ``reentry_expires_at`` sits beside the fired timestamp
    that ``pathway.reentry`` records.
    """
    if plan is None:
        return {}

    out: dict[str, Any] = {}
    if plan.target_unit is not None:
        out["target_unit"] = plan.target_unit
    if plan.safe_hold_minutes is not None:
        out["safe_hold_minutes"] = plan.safe_hold_minutes
    if plan.expected_release_at is not None:
        out["expected_release_at"] = jsonable(plan.expected_release_at)
    if plan.release_probability is not None:
        out["release_probability"] = plan.release_probability
    if plan.note:
        out["note"] = plan.note

    trigger = plan.reentry
    if trigger is not None:
        out["reentry_armed_at"] = jsonable(trigger.armed_at)
        out["reentry_expires_at"] = jsonable(trigger.expires_at)
        out["reentry_holding_unit"] = trigger.holding_unit
        out["reentry_news2_rise"] = trigger.news2_rise
        out["reentry_baseline_news2"] = trigger.baseline_news2
        out["reentry_on_alternative_lost"] = trigger.on_alternative_lost
    return out
