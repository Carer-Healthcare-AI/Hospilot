"""Scenarios — change the inputs without changing the code.

A scenario is a YAML file describing a hospital and its candidates. It builds a
:class:`~allocation.ingest.fixtures.FixtureDataSource`, so it enters the system through the
same ``DataSource`` seam the real Hasura reader will, and every layer above ``ingest/``
behaves identically::

    python -m allocation --scenario scenarios/ward_crash.yaml

Two decisions worth stating, because both prevent a scenario from lying:

**Absent means absent.** Omit a key and the signal is ``None`` — dropped from its component
and the coverage lowered. Writing ``spo2: null`` is the same thing, explicitly. Neither
becomes 0, because a missing SpO2 and an SpO2 of zero are not the same patient. A scenario
cannot make a factor score 0 by leaving it out, which is the mistake it would otherwise be
easiest to make.

**Times are relative.** ``at: -55m`` means 55 minutes before the auction opens. Absolute
timestamps in a scenario file rot: the freshness windows (4 h for vitals, 24 h for labs) are
measured from ``now``, so a file written today silently loses half its inputs next month, and
the utilities drop for a reason nobody would think to look for.

**A scenario describes as many units as it wants to auction.** One unit is written inline::

    hospital:
      unit: icu
      unit_total_beds: 20
      unit_occupied_beds: 20

Several go under ``units:``, with the two hospital-wide fields stated once::

    hospital:
      boarding_count: 7          # ED queue — the same fact whichever bed is auctioned
      lwbs_risk: 0.42
      units:
        - {unit: icu,  unit_total_beds: 20, unit_occupied_beds: 20, active_isolation_cases: 2}
        - {unit: ward, unit_total_beds: 40, unit_occupied_beds: 24, active_isolation_cases: 3}

A file that describes only the ICU can only be auctioned an ICU bed; asking it for a ward bed
raises rather than answering with ICU's occupancy. That is the point of the unit argument on
``DataSource.hospital_state`` — an occupancy is what sets the reserve price, contention,
scarcity and every budget factor, so the wrong unit's is not a rough version of the answer.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from allocation.contracts import (
    AgentKind,
    Candidate,
    CareNeed,
    HospitalState,
    LabResult,
    MedicationOrder,
    PatientData,
    VitalsReading,
)
from allocation.ingest.fixtures import FixtureDataSource

_OFFSET = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*([mhd])\s*$", re.IGNORECASE)
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


class ScenarioError(ValueError):
    """The scenario file is not usable. Always raised with the offending key."""


def parse_moment(value: Any, now: datetime, where: str) -> datetime:
    """``-55m`` / ``+2h`` relative to ``now``, or an ISO timestamp."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=now.tzinfo)

    text = str(value)
    match = _OFFSET.match(text)
    if match:
        amount, unit = match.groups()
        return now + timedelta(**{_UNITS[unit.lower()]: float(amount)})

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ScenarioError(
            f"{where}: cannot read time {value!r}. Use a relative offset like '-55m', "
            f"'+2h', '-1d', or an ISO timestamp."
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=now.tzinfo)


def _enum(enum_cls, value: Any, where: str):
    try:
        return enum_cls(str(value).strip().lower())
    except ValueError as exc:
        allowed = [e.value for e in enum_cls]
        raise ScenarioError(f"{where}: {value!r} is not one of {allowed}") from exc


#: Unit-scoped field -> the ``icu_*`` spelling it replaced. Both are accepted for one release
#: so existing scenario files and API callers keep parsing; the legacy spelling also implies
#: ``unit: icu``, since that is the only unit it could ever have described.
_LEGACY_HOSPITAL_KEYS = {
    "unit_total_beds": "icu_total_beds",
    "unit_occupied_beds": "icu_occupied_beds",
    "predicted_demand_4h": "predicted_icu_demand_4h",
}


_MISSING = object()


def _hospital_key(body: Mapping[str, Any], name: str, default: Any = _MISSING) -> Any:
    """The value under ``name``, falling back to the ``icu_*`` spelling it replaced.

    With no ``default``, raises ``KeyError(name)`` naming the *current* spelling when neither
    is present, so the message teaches the new vocabulary rather than the deprecated one.
    """
    if name in body:
        return body[name]
    legacy = _LEGACY_HOSPITAL_KEYS.get(name)
    if legacy is not None and legacy in body:
        return body[legacy]
    if default is _MISSING:
        raise KeyError(name)
    return default


#: The two fields on :class:`HospitalState` that are facts about the hospital rather than
#: about the unit under auction, so a multi-unit scenario may state them once. ED boarding and
#: left-without-being-seen describe the ED's queue; they do not change with which bed is being
#: auctioned. Every other field does: ``active_isolation_cases`` is divided by *this* unit's
#: bed count, and the two forecasts are this unit's demand and this unit's discharges.
_HOSPITAL_WIDE_KEYS = ("boarding_count", "lwbs_risk")


def _hospitals(body: Mapping[str, Any]) -> dict[str, HospitalState]:
    """The units a scenario's ``hospital:`` block describes, by unit name.

    One unit is written inline, as it always was. Several go under ``units:``, which is what
    lets a file describe both the ICU and the ward — necessary since ``hospital_state`` takes
    the unit, so a ward-bed auction against an ICU-only scenario now fails rather than
    quietly reading ICU's beds.
    """
    if "units" not in body:
        single = _hospital(body)
        return {single.unit: single}

    rows = body["units"]
    if not isinstance(rows, list) or not rows:
        raise ScenarioError("hospital.units: expected a non-empty list of unit states")

    shared = {k: v for k, v in body.items() if k in _HOSPITAL_WIDE_KEYS}
    per_unit = [k for k in body if k != "units" and k not in _HOSPITAL_WIDE_KEYS]
    if per_unit:
        raise ScenarioError(
            f"hospital: {sorted(per_unit)} sit beside 'units:' but are per-unit fields — move "
            f"each into the unit it describes. Only {list(_HOSPITAL_WIDE_KEYS)} may be shared, "
            "because they describe the ED's queue rather than the beds under auction."
        )

    states: dict[str, HospitalState] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ScenarioError(f"hospital.units[{index}]: expected a mapping")
        state = _hospital({**shared, **row})
        if state.unit in states:
            raise ScenarioError(
                f"hospital.units[{index}]: unit {state.unit!r} is described twice. Two "
                "occupancies for one unit at one instant is not a hospital state."
            )
        states[state.unit] = state
    return states


def _hospital(body: Mapping[str, Any]) -> HospitalState:
    try:
        if "unit" in body:
            unit = str(body["unit"]).strip().lower()
        elif any(old in body for old in _LEGACY_HOSPITAL_KEYS.values()):
            # The legacy spelling described ICU by construction — there was no other unit it
            # could mean. Inferring it here is what keeps old files parsing unchanged.
            unit = "icu"
        else:
            # New spelling with no unit named. Not defaultable: guessing would score one
            # unit's beds against another unit's caps.
            raise KeyError("unit")
        return HospitalState(
            unit=unit,
            unit_total_beds=int(_hospital_key(body, "unit_total_beds")),
            unit_occupied_beds=int(_hospital_key(body, "unit_occupied_beds")),
            predicted_demand_4h=_opt_float(_hospital_key(body, "predicted_demand_4h", None)),
            expected_discharges_4h=_opt_float(body.get("expected_discharges_4h")),
            boarding_count=_opt_int(body.get("boarding_count")),
            lwbs_risk=_opt_float(body.get("lwbs_risk")),
            active_isolation_cases=_opt_int(body.get("active_isolation_cases")),
        )
    except KeyError as exc:
        raise ScenarioError(f"hospital: missing required key {exc.args[0]!r}") from exc


def _vitals(rows: Sequence[Mapping[str, Any]], now: datetime, who: str) -> tuple[VitalsReading, ...]:
    return tuple(
        VitalsReading(
            recorded_at=parse_moment(row.get("at", "0m"), now, f"{who}.vitals[{i}].at"),
            temperature=_opt_float(row.get("temperature")),
            pulse=_opt_float(row.get("pulse")),
            bp_systolic=_opt_float(row.get("bp_systolic")),
            bp_diastolic=_opt_float(row.get("bp_diastolic")),
            spo2=_opt_float(row.get("spo2")),
            respiratory_rate=_opt_float(row.get("respiratory_rate")),
            gcs=_opt_float(row.get("gcs")),
            is_critical=row.get("is_critical"),
            on_oxygen=row.get("on_oxygen"),
        )
        for i, row in enumerate(rows)
    )


def _labs(rows: Sequence[Mapping[str, Any]], now: datetime, who: str) -> tuple[LabResult, ...]:
    out = []
    for i, row in enumerate(rows):
        try:
            out.append(
                LabResult(
                    test_name=str(row["test_name"]),
                    result_value=float(row["result_value"]),
                    reported_at=parse_moment(row.get("at", "0m"), now, f"{who}.labs[{i}].at"),
                    unit=row.get("unit"),
                    flag=row.get("flag"),
                )
            )
        except KeyError as exc:
            raise ScenarioError(f"{who}.labs[{i}]: missing {exc.args[0]!r}") from exc
    return tuple(out)


def _orders(rows: Sequence[Mapping[str, Any]], now: datetime, who: str) -> tuple[MedicationOrder, ...]:
    return tuple(
        MedicationOrder(
            medication_name=str(row.get("medication_name", "")),
            generic_name=row.get("generic_name"),
            route=row.get("route"),
            status=row.get("status"),
            prescribed_at=parse_moment(row["at"], now, f"{who}.orders[{i}].at")
            if "at" in row
            else None,
        )
        for i, row in enumerate(rows)
    )


def _candidate(body: Mapping[str, Any], now: datetime) -> Candidate:
    cid = str(body.get("candidate_id", "")) or "<unnamed>"
    try:
        agent = _enum(AgentKind, body["agent"], f"{cid}.agent")
        needs = frozenset(
            _enum(CareNeed, need, f"{cid}.needs") for need in body.get("needs", ())
        )
        return Candidate(
            candidate_id=cid,
            patient_token=str(body.get("patient_token", f"tok-{cid}")),
            agent=agent,
            visit_id=body.get("visit_id"),
            admission_id=body.get("admission_id"),
            arrived_at=parse_moment(body["arrived_at"], now, f"{cid}.arrived_at"),
            current_unit=body.get("current_unit"),
            condition_category=body.get("condition_category"),
            severity_band=body.get("severity_band"),
            needs=needs,
        )
    except KeyError as exc:
        raise ScenarioError(f"candidate {cid}: missing required key {exc.args[0]!r}") from exc


def load_scenario(
    path: str | Path, now: datetime
) -> tuple[FixtureDataSource, tuple[Candidate, ...], str]:
    """Read a scenario file. Returns the source, its candidates, and its description."""
    file = Path(path)
    if not file.exists():
        raise ScenarioError(f"no scenario file at {file}")

    body = yaml.safe_load(file.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise ScenarioError(f"{file}: expected a YAML mapping at the top level")

    return build_scenario(body, now, where=str(file), default_description=file.stem)


def build_scenario(
    body: Mapping[str, Any],
    now: datetime,
    where: str = "scenario",
    default_description: str = "scenario",
) -> tuple[FixtureDataSource, tuple[Candidate, ...], str]:
    """Build a scenario from an already-parsed mapping.

    Split out from :func:`load_scenario` so a scenario arriving as a **JSON request body** and
    one arriving as a **YAML file** are parsed by the same code. Two parsers would be two
    definitions of "absent", and the whole point of this module is that absent stays absent —
    a second implementation would drift on exactly the case that matters least visibly and
    costs most (a missing SpO2 quietly becoming 0.0).

    ``where`` prefixes every error, so a caller is told which document was wrong.
    """
    if "hospital" not in body:
        raise ScenarioError(f"{where}: missing 'hospital'")
    if not body.get("candidates"):
        raise ScenarioError(f"{where}: missing 'candidates' — an auction needs bidders")

    if not isinstance(body["hospital"], dict):
        raise ScenarioError(
            f"{where}: 'hospital' must be a mapping — one unit inline, or several under "
            "'units:'"
        )
    hospitals = _hospitals(body["hospital"])

    candidates: list[Candidate] = []
    patients: dict[str, PatientData] = {}

    for raw in body["candidates"]:
        candidate = _candidate(raw, now)
        if candidate.candidate_id in patients:
            raise ScenarioError(f"duplicate candidate_id {candidate.candidate_id!r}")
        candidates.append(candidate)
        who = candidate.candidate_id
        patients[who] = PatientData(
            candidate=candidate,
            vitals=_vitals(raw.get("vitals", ()), now, who),
            labs=_labs(raw.get("labs", ()), now, who),
            orders=_orders(raw.get("orders", ()), now, who),
            pending_nursing_tasks=_opt_int(raw.get("pending_nursing_tasks")),
            ward_nurses=_opt_int(raw.get("ward_nurses")),
            ot_cases_at_risk=_opt_int(raw.get("ot_cases_at_risk")),
            expected_los_days=_opt_float(raw.get("expected_los_days")),
            icu_day_rate=_opt_float(raw.get("icu_day_rate")),
            best_alternative_unit=raw.get("best_alternative_unit"),
            alternative_units=tuple(str(u) for u in (raw.get("alternative_units") or ())),
            pacu_capacity_probability=_opt_float(raw.get("pacu_capacity_probability")),
        )

    return (
        FixtureDataSource(hospital=hospitals, patients=patients),
        tuple(candidates),
        str(body.get("description", default_description)),
    )


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)
