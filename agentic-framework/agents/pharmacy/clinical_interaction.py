import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura
from llm_client import llm_chat

logger = logging.getLogger(__name__)
_SA = "sa_clinical_interaction"


@activity.defn
async def check_polypharmacy(session_id: str) -> dict:
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})
    orders = await hasura.pharmacy_get_pending_orders()
    patient_meds: dict[str, list] = {}
    for o in orders:
        token = str(o.get("patient_token") or o.get("id"))
        patient_meds.setdefault(token, []).append(o.get("medication_name"))
    polypharmacy = {t: meds for t, meds in patient_meds.items() if len(meds) >= 5}
    for token, meds in polypharmacy.items():
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Polypharmacy detected (patient {token[:8]}): {len(meds)} concurrent medications â€” interaction check required.",
        })
    result = {
        "polypharmacy_count": len(polypharmacy),
        "total_patients": len(patient_meds),
        "polypharmacy_tokens": list(polypharmacy.keys())[:5],
    }
    logger.info("check_polypharmacy  session=%s  polypharmacy=%d", session_id, len(polypharmacy))
    return result


@activity.defn
async def run_interaction_check(session_id: str) -> dict:
    rules = await hasura.pharmacy_get_interaction_rules()
    orders = await hasura.pharmacy_get_pending_orders()
    order_meds = {(o.get("generic_name") or o.get("medication_name") or "").lower()
                  for o in orders}
    major_hits, moderate_hits = [], []
    for rule in rules:
        a = (rule.get("drug_a") or "").lower()
        b = (rule.get("drug_b") or "").lower()
        if a in order_meds and b in order_meds:
            severity = rule.get("severity")
            entry = {"drug_a": rule.get("drug_a"), "drug_b": rule.get("drug_b"),
                     "severity": severity, "action": rule.get("action_required")}
            if severity == "major":
                major_hits.append(entry)
            elif severity == "moderate":
                moderate_hits.append(entry)

    prompt = (
        f"A hospital pharmacy has {len(orders)} active orders. "
        f"Drug-drug interaction scan found {len(major_hits)} MAJOR and {len(moderate_hits)} MODERATE interactions. "
        "Provide a one-sentence triage note for the pharmacist."
    )
    ai_note = ""
    try:
        ai_note = await llm_chat(user=prompt, max_tokens=120)
    except Exception as exc:
        logger.warning("run_interaction_check AI error: %s", exc)

    for hit in major_hits:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"MAJOR interaction: {hit['drug_a']} + {hit['drug_b']} â€” action: {hit['action']}.",
        })
    result = {
        "major_interaction_count": len(major_hits),
        "moderate_interaction_count": len(moderate_hits),
        "ai_triage_note": ai_note,
        "interactions": major_hits[:10] + moderate_hits[:5],
    }
    logger.info("run_interaction_check  session=%s  major=%d  moderate=%d",
                session_id, len(major_hits), len(moderate_hits))
    return result


@activity.defn
async def check_allergy_conflict(session_id: str) -> dict:
    orders = await hasura.pharmacy_get_pending_orders()
    # Allergy field on order (populated if EHR data present)
    allergy_conflicts = [o for o in orders
                         if o.get("allergy_flag") or o.get("known_allergy")]
    for o in allergy_conflicts:
        await broadcast(session_id, {
            "type": "alert", "severity": "critical",
            "message": f"Allergy conflict: {o.get('medication_name')} contra-indicated for patient â€” hold order.",
        })
    result = {
        "allergy_conflict_count": len(allergy_conflicts),
        "conflict_orders": [{"id": str(o.get("id")), "medication": o.get("medication_name")}
                            for o in allergy_conflicts[:10]],
    }
    logger.info("check_allergy_conflict  session=%s  conflicts=%d", session_id, len(allergy_conflicts))
    return result


@activity.defn
async def approve_safe_dispense(session_id: str) -> dict:
    rules = await hasura.pharmacy_get_interaction_rules()
    orders = await hasura.pharmacy_get_pending_orders()
    order_meds = {(o.get("generic_name") or o.get("medication_name") or "").lower()
                  for o in orders}
    major_count = sum(
        1 for r in rules
        if r.get("severity") == "major"
        and (r.get("drug_a") or "").lower() in order_meds
        and (r.get("drug_b") or "").lower() in order_meds
    )
    safe_count = max(0, len(orders) - major_count)
    result = {
        "approved_count": safe_count,
        "withheld_count": major_count,
    }
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("approve_safe_dispense  session=%s  approved=%d  withheld=%d",
                session_id, safe_count, major_count)
    return result
