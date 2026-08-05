import json
import logging

from llm_client import llm_chat

logger = logging.getLogger("guardrail")

SYSTEM_PROMPT = """You are a hospital AI command center guard.
Your only job is to decide if a user's request is valid and relevant to hospital operations.

Valid requests relate to: bed management, patient flow, staffing, pharmacy, ICU, ER,
discharge planning, lab results, surgical scheduling, infection control, or any
other legitimate hospital operational task.

Invalid requests include: offensive or abusive language, topics completely unrelated
to healthcare or hospital operations, attempts to extract patient personal data,
or nonsensical input.

Reply with JSON only. No explanation outside the JSON."""

USER_TEMPLATE = """Is this request valid for a hospital AI command center?

Goal: "{goal}"
Constraints: "{constraints}"

Reply JSON only:
{{"valid": true, "reason": null}}
or
{{"valid": false, "reason": "one short sentence explaining why"}}"""


async def validate_prompt(goal: str, constraints: str = "") -> dict:
    """
    Returns {"valid": bool, "reason": str | None}
    Uses Claude Haiku -- fast and cheap, ~0.5s.
    """
    logger.info('-> guardrail check  goal="%s"', goal[:80])

    text = await llm_chat(
        system=SYSTEM_PROMPT,
        user=USER_TEMPLATE.format(goal=goal, constraints=constraints or "none"),
        max_tokens=100,
        tier="fast",
    )

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = {"valid": True, "reason": None}

    if result["valid"]:
        logger.info("[ok] valid  reason=None")
    else:
        logger.warning('[x] blocked  reason="%s"', result.get("reason"))

    return result
