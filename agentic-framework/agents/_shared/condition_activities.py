import logging
from dataclasses import dataclass, field

from temporalio import activity

from api.routes.ws import broadcast

logger = logging.getLogger(__name__)


@dataclass
class EvaluateTaskConditionInput:
    task_id: str
    subagent_id: str
    condition: str
    ta_results: dict = field(default_factory=dict)
    session_id: str = ""


# DEPRECATED -- replaced by typed {symbol, op, value} conditions evaluated in
# pure Python inside should_run_task (_condition_check.py).
# Kept registered for Temporal replay compatibility with old sessions.
# LLM call removed -- always returns True so legacy sessions continue rather than silently skip.
@activity.defn
async def evaluate_task_condition(inp: EvaluateTaskConditionInput) -> bool:
    logger.warning(
        "evaluate_task_condition called on deprecated activity  task_id=%s  condition=%s",
        inp.task_id, inp.condition,
    )
    return True
