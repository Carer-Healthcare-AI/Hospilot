import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura

logger = logging.getLogger(__name__)
_SA = "sa_dispensing_validation"


@activity.defn
async def verify_patient_identity(session_id: str) -> dict:
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})
    logs = await hasura.pharmacy_get_dispensing_log(hours=8)
    verified = [e for e in logs if e.get("patient_verified")]
    unverified = [e for e in logs if not e.get("patient_verified")]
    for e in unverified:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Patient identity not verified: {e.get('medication_name')} â€” hold until ID confirmed.",
        })
    result = {
        "verified_count": len(verified),
        "unverified_count": len(unverified),
        "unverified_orders": [{"id": str(e.get("id")), "medication": e.get("medication_name")}
                              for e in unverified[:10]],
    }
    logger.info("verify_patient_identity  session=%s  verified=%d  unverified=%d",
                session_id, len(verified), len(unverified))
    return result


@activity.defn
async def match_medication_prescription(session_id: str) -> dict:
    logs = await hasura.pharmacy_get_dispensing_log(hours=8)
    matched = [e for e in logs if e.get("prescription_matched")]
    mismatched = [e for e in logs if e.get("prescription_matched") is False]
    unmatched_unknown = [e for e in logs if e.get("prescription_matched") is None]
    for e in mismatched:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"Prescription mismatch: {e.get('medication_name')} â€” dispense does not match Rx. Stop and verify.",
        })
    result = {
        "matched_count": len(matched),
        "mismatch_count": len(mismatched),
        "unverified_count": len(unmatched_unknown),
        "mismatch_orders": [{"id": str(e.get("id")), "medication": e.get("medication_name")}
                            for e in mismatched[:10]],
    }
    logger.info("match_medication_prescription  session=%s  matched=%d  mismatch=%d",
                session_id, len(matched), len(mismatched))
    return result


@activity.defn
async def validate_dispensing_dosage(session_id: str) -> dict:
    logs = await hasura.pharmacy_get_dispensing_log(hours=8)
    discrepancies = [e for e in logs if e.get("verification_status") == "discrepancy"]
    for e in discrepancies:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"Dosage discrepancy: {e.get('medication_name')} â€” dispensed dose differs from prescribed. Pharmacist review required.",
        })
    result = {
        "dosage_ok_count": len([e for e in logs if e.get("verification_status") == "verified"]),
        "discrepancy_count": len(discrepancies),
        "discrepancy_orders": [{"id": str(e.get("id")), "medication": e.get("medication_name")}
                               for e in discrepancies[:10]],
    }
    logger.info("validate_dispensing_dosage  session=%s  discrepancies=%d", session_id, len(discrepancies))
    return result


@activity.defn
async def release_or_hold_dispensing(session_id: str) -> dict:
    logs = await hasura.pharmacy_get_dispensing_log(hours=8)
    verified = [e for e in logs
                if e.get("patient_verified")
                and e.get("prescription_matched")
                and e.get("verification_status") == "verified"]
    held = [e for e in logs
            if not e.get("patient_verified")
            or e.get("prescription_matched") is False
            or e.get("verification_status") == "discrepancy"]
    result = {
        "released_count": len(verified),
        "held_count": len(held),
        "held_orders": [{"id": str(e.get("id")), "medication": e.get("medication_name")}
                        for e in held[:10]],
    }
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("release_or_hold_dispensing  session=%s  released=%d  held=%d",
                session_id, len(verified), len(held))
    return result
