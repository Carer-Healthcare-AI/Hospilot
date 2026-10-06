"""Read the world from Hasura and assemble a ``POST /diagnostic/auction`` body.

The black-box replacement for the engine's data source, exactly as ``rl_gateway/assemble.py``
is for beds: instead of injecting a reader into the engine, build the inline request shape.

INVARIANT (absent is not zero, and absent is not a default). A missing clinical value raises
here rather than being filled in. This is stricter than the bed assembler's `_signal`, and
deliberately so: the three diagnostic scores drive the whole ranking, and a default would rank
a real patient against an invented number. The engine refuses them too — this is the second
line, not the only one.

⚠ VERIFY-LATER surface, all of it isolated in this file:

* `MACHINE_REGISTRY` is **fabricated**. No `hospilot.equipment` or imaging-schedule table
  exists in the tenant schema, so there is nothing to read. Replace `machines_for` with a real
  read the moment one exists — everything else here already works off real orders.
* `PROCEDURE_SCORES` is a **stated lookup table**, not fitted and not clinically signed off.
  It exists so Phase 3 can run on real machines before the medical director has agreed the
  real numbers. Every screen driven by it must say so.
* The source keys for imaging orders are assumptions about this tenant's shape.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from rl_gateway_diagnostic.queries import AGENT_MAP, modality_for, participants_for

log = logging.getLogger("rl_gateway_diagnostic.assemble")


class MissingClinicalValue(ValueError):
    """A request could not be scored from tenant data. Do not default — do not send."""


# --- verify-later config ---------------------------------------------------------------

#: FABRICATED. One entry per machine the synthetic hospital owns. Windows are local-day
#: operating hours; setup/cleanup are per-modality turnaround. Replace with a real read.
MACHINE_REGISTRY: tuple[Mapping[str, Any], ...] = (
    {"machine_id": "CT-01", "modality": "ct", "capabilities": ("contrast",),
     "open_hour": 0, "close_hour": 24, "setup_minutes": 3, "cleanup_minutes": 3},
    {"machine_id": "MRI-01", "modality": "mri", "capabilities": ("contrast", "cardiac"),
     "open_hour": 8, "close_hour": 20, "setup_minutes": 10, "cleanup_minutes": 5},
    {"machine_id": "XR-01", "modality": "x_ray", "capabilities": (),
     "open_hour": 0, "close_hour": 24, "setup_minutes": 1, "cleanup_minutes": 1},
    {"machine_id": "XR-02", "modality": "x_ray", "capabilities": ("portable",),
     "open_hour": 0, "close_hour": 24, "setup_minutes": 1, "cleanup_minutes": 1},
    {"machine_id": "US-01", "modality": "ultrasound", "capabilities": ("doppler",),
     "open_hour": 0, "close_hour": 24, "setup_minutes": 2, "cleanup_minutes": 2},
)

#: FABRICATED, stated rather than fitted. procedure keyword -> (yield, impact probability,
#: impact importance, typical minutes). Used only when the order carries no scores of its own.
PROCEDURE_SCORES: Mapping[str, tuple[float, float, float, int]] = {
    "ct head":       (0.80, 0.70, 0.90, 15),
    "ct chest":      (0.75, 0.60, 0.80, 20),
    "ct abdomen":    (0.70, 0.60, 0.75, 20),
    "mri brain":     (0.85, 0.65, 0.85, 40),
    "mri spine":     (0.75, 0.50, 0.70, 45),
    "chest x-ray":   (0.55, 0.45, 0.60, 10),
    "abdominal x-ray": (0.50, 0.40, 0.55, 10),
    "ultrasound abdomen": (0.65, 0.55, 0.70, 20),
    "echo":          (0.70, 0.60, 0.80, 30),
}

#: Deadline by clinical urgency, in minutes from the order. A request with no urgency has no
#: deadline we can defend, and is refused rather than given a generous one.
#:
#: ⚠ THE FLOOR IS STRUCTURAL, NOT CLINICAL. A deadline must leave room for setup + procedure
#: + cleanup, or the request can never be awarded: eligibility is a HARD filter in the engine,
#: so an impossible deadline does not produce a late scan, it produces `no_award` with a full
#: ladder above the reserve and no winner — which reads like a pricing problem and is not one.
#: `immediate` was 30 here and silently did exactly that to every CT (15 + 3 + 3 = 21 minutes
#: of work, against 30 minutes that start elapsing when the order is written). The longest
#: shipped combination is MRI brain: 40 + 10 + 5 = 55. Hence 60.
URGENCY_DEADLINE_MINUTES: Mapping[str, int] = {
    "immediate": 60,
    "urgent": 120,
    "routine": 480,
}


# --- machines ----------------------------------------------------------------------------


def machines_for(modality: str, now: datetime) -> list[dict[str, Any]]:
    """The machines of one modality, with their operating window for `now`'s day.

    ⚠ Reads MACHINE_REGISTRY, which is fabricated. A real implementation reads the equipment
    table and, critically, the existing bookings — `allocations` below is empty here, which
    means this assembler currently believes every machine is free.
    """
    day = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for m in MACHINE_REGISTRY:
        if m["modality"] != modality:
            continue
        out.append(
            {
                "machine_id": m["machine_id"],
                "modality": m["modality"],
                "status": "available",
                "window_starts_at": (day + timedelta(hours=m["open_hour"])).isoformat(),
                "window_ends_at": (day + timedelta(hours=m["close_hour"])).isoformat(),
                "capabilities": list(m["capabilities"]),
                "allocations": [],  # ⚠ no booking source yet
                "setup_minutes": m["setup_minutes"],
                "cleanup_minutes": m["cleanup_minutes"],
            }
        )
    return out


# --- requests ----------------------------------------------------------------------------


def _scores(order: Mapping[str, Any]) -> tuple[float, float, float, int]:
    """Prefer scores carried on the order; fall back to the stated table; else refuse."""
    explicit = (
        order.get("diagnostic_yield"),
        order.get("management_impact_probability"),
        order.get("management_impact_importance"),
    )
    if all(v is not None for v in explicit):
        duration = order.get("estimated_duration_minutes") or 15
        return (float(explicit[0]), float(explicit[1]), float(explicit[2]), int(duration))

    procedure = str(order.get("procedure") or order.get("description") or "").lower()
    for keyword, values in PROCEDURE_SCORES.items():
        if keyword in procedure:
            return values

    raise MissingClinicalValue(
        f"order {order.get('id')!r} procedure {procedure!r} carries no diagnostic scores and "
        "matches no entry in PROCEDURE_SCORES; refusing rather than defaulting"
    )


def _deadline(order: Mapping[str, Any], requested_at: datetime) -> datetime:
    urgency = str(order.get("urgency") or order.get("priority") or "").lower()
    if urgency not in URGENCY_DEADLINE_MINUTES:
        raise MissingClinicalValue(
            f"order {order.get('id')!r} has urgency {urgency!r}, which maps to no deadline; "
            "a request with no defensible deadline flattens the urgency component"
        )
    return requested_at + timedelta(minutes=URGENCY_DEADLINE_MINUTES[urgency])


def request_from_order(order: Mapping[str, Any], department: str) -> dict[str, Any]:
    """One imaging order -> one engine request. Raises rather than inventing a value."""
    agent = AGENT_MAP().get(department)
    if agent is None:
        raise MissingClinicalValue(f"department {department!r} is not a registered bidder")

    procedure = str(order.get("procedure") or order.get("description") or "")
    modality = modality_for(procedure)
    if modality is None:
        raise MissingClinicalValue(
            f"order {order.get('id')!r} procedure {procedure!r} names no known modality"
        )

    raw_at = order.get("ordered_at") or order.get("created_at")
    if not raw_at:
        raise MissingClinicalValue(f"order {order.get('id')!r} has no order timestamp")
    requested_at = datetime.fromisoformat(str(raw_at))

    question = str(order.get("clinical_question") or "").strip()
    if not question:
        # The engine's contract requires it, and it is not decoration: the clinical question
        # is what makes an alternative procedure a real substitute or not.
        raise MissingClinicalValue(
            f"order {order.get('id')!r} carries no clinical_question"
        )

    yld, prob, importance, minutes = _scores(order)
    return {
        "request_id": str(order["id"]),
        "patient_token": str(order.get("patient_token") or order.get("patient_id")),
        "agent": agent,
        "clinical_question": question,
        "requested_procedure": procedure,
        "eligible_modalities": [modality],
        "requested_at": requested_at.isoformat(),
        "latest_useful_at": _deadline(order, requested_at).isoformat(),
        "estimated_duration_minutes": minutes,
        "diagnostic_yield": yld,
        "management_impact_probability": prob,
        "management_impact_importance": importance,
        "required_capabilities": list(order.get("required_capabilities") or ()),
        "alternative_procedures": list(order.get("alternative_procedures") or ()),
        "transport_minutes": int(order.get("transport_minutes") or 0),
        "modality_yields": {},
        "reschedulable": bool(order.get("reschedulable", False)),
    }


# --- the body ----------------------------------------------------------------------------


def build_auction_body(
    modality: str,
    orders: Sequence[tuple[Mapping[str, Any], str]],
    now: datetime,
    regime: str = "normal",
) -> dict[str, Any]:
    """Assemble one auction.

    `orders` is (order, department) pairs — one per bidding department. The engine allocates
    one interval, so a department bidding for two of its own patients at once is a queueing
    question this does not answer: nominate one, exactly as the bed flow does.
    """
    machines = machines_for(modality, now)
    if not machines:
        raise MissingClinicalValue(f"no machines registered for modality {modality!r}")

    # Drop what the engine would drop anyway, but say so. Eligibility is a hard filter there:
    # an infeasible request is removed before bidding, never penalised inside it, so sending
    # one produces a quieter and more confusing result than refusing it here.
    turnaround = max(m["setup_minutes"] + m["cleanup_minutes"] for m in machines)

    eligible = set(participants_for(modality))
    requests: list[dict[str, Any]] = []
    seen: set[str] = set()
    for order, department in orders:
        if department not in eligible:
            log.debug("dropping %s: not a bidder on %s", department, modality)
            continue
        if department in seen:
            log.warning("department %s nominated twice for %s; keeping the first",
                        department, modality)
            continue
        request = request_from_order(order, department)
        latest = datetime.fromisoformat(request["latest_useful_at"])
        needed = timedelta(minutes=request["estimated_duration_minutes"] + turnaround)
        if latest < now + needed:
            log.warning(
                "dropping %s: deadline %s cannot fit %d min of work plus %d min turnaround "
                "starting at %s — the engine would drop it silently and report no_award",
                request["request_id"], request["latest_useful_at"],
                request["estimated_duration_minutes"], turnaround, now.isoformat(),
            )
            continue
        requests.append(request)
        seen.add(department)

    if not requests:
        raise MissingClinicalValue(f"no eligible requests for modality {modality!r}")

    return {
        "machines": machines,
        "requests": requests,
        "regime": regime,
        "opened_at": now.isoformat(),
    }
