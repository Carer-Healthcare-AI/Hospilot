"""Read the world from Hasura/Fabric + forecasts and assemble a POST /auction request body.

This is the black-box replacement for the engine's DataSource: instead of injecting a reader
into the engine, we build the same information as the inline request shape (BACKEND_HANDOVER
§4.2) that the engine's scenario parser accepts.

INVARIANT #1 (absent is not zero). Every clinical field goes through `_signal`: a missing key
arrives as None, never 0.0. Zero means "this patient is fine" and would rank an untested
patient above a tested one (contracts.Signal). The one exception is unit bed counts, which are
load-bearing for reserve/contention/scarcity — if the unit cannot be resolved we RAISE rather
than substitute another unit's beds.

⚠ VERIFY-LATER surface. The engine's target field names (right-hand side) are fixed by §4.2.
The SOURCE keys below (Fabric REST / GraphQL responses) and WARD_TO_UNIT are assumptions to
confirm against a real response for this tenant; they are all isolated here so correction is a
one-file change. Everything defaults to None when a key is absent, which is the safe direction.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from rl_gateway.mapping import map_agent


# --- verify-later config ---------------------------------------------------------------------

# hospilot `beds.ward` free-text -> the engine's resource unit (compared lowercased).
# Verified against real tenant data (default/carer beds): ICU, Emergency, General Ward,
# Cardiology, Orthopedics, Pediatrics, Private, Semi-Private. A ward with no mapping is
# treated as not-part-of-this-unit.
WARD_TO_UNIT: dict[str, str] = {
    "icu": "icu",
    "emergency": "ed",
    "general ward": "ward",
    "cardiology": "ward",
    "orthopedics": "ward",
    "pediatrics": "ward",
    "private": "ward",
    "semi-private": "ward",
    # engine units that may not be distinct wards in this tenant yet:
    "hdu": "hdu",
    "pacu": "pacu",
    "ed": "ed",
    "er": "ed",
    "ward": "ward",
}

# bed.status values that count as occupied (verified enum: Available/Occupied/Dirty/Cleaning/
# reserved/vacating; compared lowercased). "reserved" is spoken-for so it counts as occupied;
# dirty/cleaning/vacating/available are free-or-freeing.
OCCUPIED_BED_STATUSES = {"occupied", "reserved"}

# Source-key -> engine vitals key (§4.2). Left value is the Fabric /vitals/latest field.
VITALS_KEYS = {
    "temperature": "temperature",
    "pulse": "pulse",
    "heart_rate": "pulse",
    "bp_systolic": "bp_systolic",
    "bp_diastolic": "bp_diastolic",
    "spo2": "spo2",
    "respiratory_rate": "respiratory_rate",
    "gcs": "gcs",
    "on_oxygen": "on_oxygen",  # absent until migration 125; stays None, NOT room air
}


def _signal(row: Mapping[str, Any] | None, key: str) -> Any:
    """The absent-is-None gate. Returns None for a missing row or missing/None value —
    never a coerced 0."""
    if not row:
        return None
    val = row.get(key)
    return val if val is not None else None


# --- hospital state --------------------------------------------------------------------------

def _bed_unit(bed: Mapping[str, Any]) -> str | None:
    ward = (bed.get("ward") or "").strip().lower()
    return WARD_TO_UNIT.get(ward)


def _is_occupied(bed: Mapping[str, Any]) -> bool:
    return str(bed.get("status", "")).strip().lower() in OCCUPIED_BED_STATUSES


async def build_hospital_state(hasura: Any, unit: str, forecaster: Any = None) -> dict[str, Any]:
    """Assemble the `hospital` block for `unit`. Raises if the unit cannot be described."""
    beds: Sequence[Mapping[str, Any]] = await hasura.get_enriched_beds() or []
    unit_beds = [b for b in beds if _bed_unit(b) == unit]
    if not unit_beds:
        # Load-bearing: occupancy sets reserve, contention, scarcity and every budget factor.
        # Substituting another unit's beds would silently reprice the whole auction.
        raise ValueError(
            f"cannot resolve unit {unit!r} in beds (no ward maps to it via WARD_TO_UNIT); "
            f"refusing to substitute another unit's occupancy"
        )

    infection = await hasura.get_active_infection_cases() or []
    isolation_here = sum(
        1 for c in infection if WARD_TO_UNIT.get(str(c.get("ward", "")).strip().lower()) == unit
    )

    demand = discharges = None
    if forecaster is not None:
        demand = await forecaster.demand_4h(unit)
        discharges = await forecaster.discharges_4h(unit)

    return {
        "unit": unit,
        "unit_total_beds": len(unit_beds),
        "unit_occupied_beds": sum(1 for b in unit_beds if _is_occupied(b)),
        "predicted_demand_4h": demand,           # None => engine uses its own fallback
        "expected_discharges_4h": discharges,
        "boarding_count": None,                  # v1: no ED-queue source wired (nullable)
        "lwbs_risk": None,                       # v1: no ED model wired (nullable)
        "active_isolation_cases": isolation_here or None,
    }


# --- candidate / patient data ----------------------------------------------------------------

def _vitals_rows(latest: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """One vitals[] row from the latest reading. Absent fields stay None."""
    if not latest:
        return []
    row: dict[str, Any] = {"at": latest.get("recorded_at") or latest.get("at")}
    for src, dst in VITALS_KEYS.items():
        if src in latest:
            row[dst] = _signal(latest, src)
    row["is_critical"] = bool(latest.get("is_critical")) if "is_critical" in latest else None
    return [row]


def _lab_rows(labs: Sequence[Mapping[str, Any]], patient_token: str) -> list[dict[str, Any]]:
    rows = []
    for lab in labs or []:
        if lab.get("patient_token") != patient_token:
            continue
        name = lab.get("test_name") or lab.get("name")
        value = _signal(lab, "result_value")
        if name is None or value is None:   # §4.2: both required per row; drop partial rows
            continue
        rows.append({
            "test_name": name,
            "result_value": value,
            "at": lab.get("reported_at") or lab.get("at"),
            "unit": lab.get("unit"),
            "flag": lab.get("flag"),
        })
    return rows


def _order_rows(orders: Sequence[Mapping[str, Any]], patient_token: str) -> list[dict[str, Any]]:
    rows = []
    for o in orders or []:
        if o.get("patient_token") != patient_token:
            continue
        med = o.get("medication")   # verified: Fabric key is `medication`, not medication_name
        if med is None:
            continue
        rows.append({
            "medication_name": med,
            "generic_name": o.get("generic_name"),   # not in Fabric shape -> None
            "route": o.get("route"),                 # not in Fabric shape -> None
            "status": o.get("status"),
            "at": o.get("prescribed_at"),
        })
    return rows


async def build_candidate(hasura: Any, spec: Mapping[str, Any]) -> dict[str, Any]:
    """Build one candidate[] entry. `spec` is a nominated patient (one per department):
    {department, candidate_id, patient_token, admission_id?, visit_id?, arrived_at,
     current_unit?, condition_category?, severity_band?}.
    """
    token = spec["patient_token"]
    admission_id = spec.get("admission_id")

    latest_vitals = await hasura.get_latest_vitals(token)
    labs = await hasura.get_recent_lab_results()
    orders = await hasura.pharmacy_get_orders()

    nursing = None
    if admission_id:
        tasks = await hasura.get_pending_nursing_tasks(admission_id)
        nursing = len(tasks) if tasks is not None else None

    return {
        "candidate_id": spec["candidate_id"],
        "agent": map_agent(spec["department"]),   # eligibility asserted at request-build time
        "arrived_at": spec["arrived_at"],
        "patient_token": token,
        "admission_id": admission_id,
        "visit_id": spec.get("visit_id"),
        "current_unit": spec.get("current_unit"),
        "condition_category": spec.get("condition_category"),
        "severity_band": spec.get("severity_band"),
        "vitals": _vitals_rows(latest_vitals),
        "labs": _lab_rows(labs, token),
        "orders": _order_rows(orders, token),
        "pending_nursing_tasks": nursing,
        # operational/financial scalars — nullable, wired as sources are confirmed
        "ward_nurses": None,
        "ot_cases_at_risk": None,
        "expected_los_days": None,
        "icu_day_rate": None,
        "alternative_units": spec.get("alternative_units", []),
        "best_alternative_unit": spec.get("best_alternative_unit"),
        "pacu_capacity_probability": None,
    }
