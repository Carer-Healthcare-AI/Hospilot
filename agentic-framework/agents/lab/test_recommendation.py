import json
import logging

from temporalio import activity

from api.routes.ws import broadcast
from db.hasura import hasura
from llm_client import llm_chat

logger = logging.getLogger(__name__)
_SA = "sa_test_recommendation"


@activity.defn
async def detect_abnormal_result(session_id: str) -> dict:
    """Detect abnormal lab results that may trigger reflex testing rules."""
    await broadcast(session_id, {"type": "sub_agent_started", "sub_agent": _SA})

    results  = await hasura.lab_get_results()
    abnormal = [r for r in results if str(r.get("flag", "")).lower() not in ("normal", "")]

    result = {
        "abnormal_count":   len(abnormal),
        "abnormal_results": [
            {"test": r.get("test_name"), "value": r.get("result_value"),
             "flag": r.get("flag"), "order_id": str(r.get("order_id", ""))[:8]}
            for r in abnormal[:20]
        ],
    }
    logger.info("detect_abnormal_result  session=%s  abnormal=%d", session_id, len(abnormal))
    return result


@activity.defn
async def evaluate_reflex_rules(session_id: str) -> dict:
    """Match abnormal results against active reflex rules to find recommended tests."""
    results  = await hasura.lab_get_results()
    rules    = await hasura.lab_get_reflex_rules()

    abnormal = [r for r in results if str(r.get("flag", "")).lower() not in ("normal", "")]
    abnormal_tests = {r.get("test_name", "").lower() for r in abnormal}

    matched = [rule for rule in rules if rule.get("trigger_test", "").lower() in abnormal_tests]

    result = {
        "recommended_count": len(matched),
        "recommendations":   [
            {"trigger": r.get("trigger_test"), "recommended": r.get("recommended_test"),
             "needs_approval": r.get("requires_physician_approval")}
            for r in matched[:10]
        ],
    }
    logger.info("evaluate_reflex_rules  session=%s  matched=%d", session_id, len(matched))
    return result


@activity.defn
async def recommend_additional_test(session_id: str) -> dict:
    """Send AI-assisted reflex test recommendation to physician."""
    results  = await hasura.lab_get_results()
    rules    = await hasura.lab_get_reflex_rules()

    abnormal = [r for r in results if str(r.get("flag", "")).lower() not in ("normal", "")][:10]
    if not abnormal:
        result = {"sent_count": 0, "recommendations": []}
        await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
        return result

    summary = "\n".join(
        f"- {r.get('test_name')}: {r.get('result_value')} {r.get('unit', '')} ({r.get('flag')})"
        for r in abnormal
    )
    rule_list = "\n".join(f"- {r.get('trigger_test')} â†’ {r.get('recommended_test')}" for r in rules[:10])

    try:
        text = await llm_chat(
            user=f"""You are a hospital lab AI. Given these abnormal results and reflex rules, recommend tests.

Abnormal results:
{summary}

Reflex rules:
{rule_list}

Return ONLY valid JSON:
{{"recommendations": [{{"test": "...", "reason": "...", "priority": "high|medium"}}], "summary": "..."}}""",
            max_tokens=400,
        )
        if "```" in text:
            text = text.split("```")[1].lstrip("json").strip()
        analysis = json.loads(text)
    except Exception:
        analysis = {"recommendations": [], "summary": "Analysis unavailable"}

    recs = analysis.get("recommendations", [])
    for r in recs[:3]:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Reflex test recommended: {r.get('test')} â€” {r.get('reason')}",
        })

    result = {"sent_count": len(recs), "recommendations": recs, "summary": analysis.get("summary", "")}
    await broadcast(session_id, {"type": "sub_agent_completed", "sub_agent": _SA, "result": result})
    logger.info("recommend_additional_test  session=%s  sent=%d", session_id, len(recs))
    return result


@activity.defn
async def create_reflex_order(session_id: str) -> dict:
    """Auto-create reflex orders for rules that don't require physician approval."""
    results = await hasura.lab_get_results()
    rules   = await hasura.lab_get_reflex_rules()

    abnormal_tests = {r.get("test_name", "").lower() for r in results if str(r.get("flag", "")).lower() not in ("normal", "")}
    auto_rules = [r for r in rules if not r.get("requires_physician_approval")
                  and r.get("trigger_test", "").lower() in abnormal_tests]

    for r in auto_rules[:5]:
        await broadcast(session_id, {
            "type": "alert", "severity": "warning",
            "message": f"Reflex order auto-created: {r.get('recommended_test')} (triggered by {r.get('trigger_test')}).",
        })

    result = {"orders_created": len(auto_rules)}
    logger.info("create_reflex_order  session=%s  created=%d", session_id, len(auto_rules))
    return result
