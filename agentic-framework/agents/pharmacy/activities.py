import json
import logging
from dataclasses import dataclass

from temporalio import activity

from cache import redis as cache
from db.hasura import hasura
from api.routes.ws import broadcast
from llm_client import llm_chat

logger = logging.getLogger(__name__)

_SYSTEM = """You are a hospital pharmacy reconciliation specialist AI.
Review discharge summaries and check whether medication reconciliation is complete.
Look for: medications listed, dosages specified, duration mentioned, potential drug interactions flagged,
and patient instructions included.
Return ONLY valid JSON, no extra text."""

_USER = """Review this discharge summary for medication completeness:

Admission ID: {admission_id}
Summary:
{summary}

Return:
{{
  "reconciliation_complete": true or false,
  "missing_items": ["list of what's missing"],
  "medications_mentioned": <count or null if not found>,
  "note": "brief clinical note for the pharmacist"
}}"""


@dataclass
class PharmacyCheckInput:
    session_id: str
    admissions: list   # from get_discharge_ready_with_summaries


@activity.defn
async def get_discharge_ready_patients(session_id: str) -> list:
    await broadcast(session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_pharmacy_census",
    })
    admissions = await cache.get_discharge_ready()
    await broadcast(session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_pharmacy_census",
        "result": {"discharge_ready_count": len(admissions)},
    })
    logger.info("pharmacy census  session=%s  discharge_ready=%d", session_id, len(admissions))
    return admissions


@activity.defn
async def check_medication_reconciliation(inp: PharmacyCheckInput) -> list:
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": "sa_pharmacy_check",
    })

    results = []
    for a in inp.admissions:
        summary_obj = a.get("summary")
        summary_text = (summary_obj or {}).get("summary_text") or ""

        if not summary_text:
            results.append({
                "admission_id":           a["id"],
                "reconciliation_complete": False,
                "missing_items":          ["no discharge summary found"],
                "medications_mentioned":  None,
                "note":                   "No discharge summary available for this patient.",
            })
            continue

        text = await llm_chat(
            system=_SYSTEM,
            user=_USER.format(
                admission_id=a["id"][:8],
                summary=summary_text[:2000],
            ),
            max_tokens=512,
            tier="quality",
        )
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        check = json.loads(text)
        check["admission_id"] = a["id"]
        results.append(check)

        # Write AI note back to discharge summary
        if check.get("note"):
            await hasura.set_ai_discharge_note(a["id"], check["note"])

    complete = sum(1 for r in results if r["reconciliation_complete"])
    incomplete = len(results) - complete

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": "sa_pharmacy_check",
        "result": {
            "checked":    len(results),
            "complete":   complete,
            "incomplete": incomplete,
        },
    })
    logger.info("pharmacy check  session=%s  checked=%d  complete=%d  incomplete=%d",
                inp.session_id, len(results), complete, incomplete)
    return results


@activity.defn
async def save_pharmacy_report(inp: PharmacyCheckInput) -> dict:
    await hasura.write_audit(
        session_id=inp.session_id,
        agent_id="pharmacy_agent",
        event_type="medication_reconciliation_checked",
        payload={"admissions": [a["id"] for a in inp.admissions]},
    )
    return {"status": "saved"}
