import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_prescription_validation"
_REQUIRED_FIELDS = ("medication_name", "dosage", "route", "frequency", "prescribed_by")

# Dosage safety limits (mg) â€” simplified reference
_DOSE_LIMITS: dict[str, tuple[float, float]] = {
    "paracetamol": (250, 1000),
    "metoprolol":  (12.5, 200),
    "amlodipine":  (2.5, 10),
    "morphine":    (2, 20),
    "vancomycin":  (250, 2000),
}


@activity.defn
async def validate_prescription_completeness(session_id: str) -> dict:
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})
    orders = await hasura.pharmacy_get_pending_orders()
    complete, incomplete = [], []
    for o in orders:
        missing = [f for f in _REQUIRED_FIELDS if not o.get(f)]
        if missing:
            incomplete.append({**o, "missing_fields": missing})
            await broadcast(session_id, {
                "type": "alert", "severity": "warning",
                "message": f"Incomplete prescription: {o.get('medication_name')} missing {missing} â€” returned for correction.",
            })
        else:
            complete.append(o)
    result = {
        "complete_count": len(complete),
        "incomplete_count": len(incomplete),
        "incomplete_orders": [{"id": str(o.get("id")), "medication": o.get("medication_name"),
                               "missing": o.get("missing_fields")} for o in incomplete[:10]],
    }
    logger.info("validate_prescription_completeness  session=%s  complete=%d  incomplete=%d",
                session_id, len(complete), len(incomplete))
    return result


@activity.defn
async def validate_dosage_range(session_id: str) -> dict:
    orders = await hasura.pharmacy_get_pending_orders()
    safe, unsafe = [], []
    for o in orders:
        med_key = (o.get("generic_name") or o.get("medication_name") or "").lower().split()[0]
        dosage_str = o.get("dosage") or ""
        try:
            dose_mg = float("".join(c for c in dosage_str if c.isdigit() or c == "."))
        except ValueError:
            continue
        limits = _DOSE_LIMITS.get(med_key)
        if limits and not (limits[0] <= dose_mg <= limits[1]):
            unsafe.append(o)
            await broadcast(session_id, {
                "type": "alert", "severity": "critical",
                "message": f"Unsafe dose: {o.get('medication_name')} {dosage_str} â€” outside range {limits[0]}-{limits[1]}mg. Pharmacist review required.",
            })
        else:
            safe.append(o)
    result = {
        "safe_count": len(safe),
        "unsafe_dose_count": len(unsafe),
        "unsafe_orders": [{"id": str(o.get("id")), "medication": o.get("medication_name"),
                           "dosage": o.get("dosage")} for o in unsafe[:10]],
    }
    logger.info("validate_dosage_range  session=%s  safe=%d  unsafe=%d",
                session_id, len(safe), len(unsafe))
    return result


@activity.defn
async def detect_duplicate_medications(session_id: str) -> dict:
    orders = await hasura.pharmacy_get_pending_orders()
    seen: dict[str, list] = {}
    for o in orders:
        key = f"{o.get('patient_token')}:{(o.get('generic_name') or o.get('medication_name') or '').lower()}"
        seen.setdefault(key, []).append(o)
    duplicates = [group for group in seen.values() if len(group) > 1]
    flat_dupes = [o for group in duplicates for o in group]
    for group in duplicates:
        med = group[0].get("medication_name")
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Duplicate detected: {med} ordered {len(group)} times for same patient â€” generating alert.",
        })
    result = {
        "duplicate_count": len(duplicates),
        "duplicates": [{"medication": g[0].get("medication_name"), "count": len(g)} for g in duplicates[:10]],
    }
    logger.info("detect_duplicate_medications  session=%s  duplicate_groups=%d", session_id, len(duplicates))
    return result


@activity.defn
async def approve_or_hold_prescription(session_id: str) -> dict:
    orders = await hasura.pharmacy_get_pending_orders()
    on_hold = [o for o in orders if o.get("status") == "on_hold"]
    pending = [o for o in orders if o.get("status") == "pending"]
    result = {
        "approved_count": len(pending),
        "held_count": len(on_hold),
        "held_orders": [{"id": str(o.get("id")), "medication": o.get("medication_name")} for o in on_hold[:10]],
    }
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("approve_or_hold_prescription  session=%s  approved=%d  held=%d",
                session_id, len(pending), len(on_hold))
    return result
