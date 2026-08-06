"""
Task code generator.

When a user creates a task from the UI, this module generates a real
@activity.defn Python function instead of relying on the LLM at runtime.

Flow:
  1. Read the agent's existing activities file for pattern reference
  2. Build context: available hasura methods for this agent
  3. The quality-tier model (see llm_client.py) generates the function code
  4. Caller writes it to generated_activities.py
"""

import logging
import re
from pathlib import Path

from llm_client import llm_chat
from agents._shared.fetch_tools import AGENT_FETCH_TOOLS
from agents._shared.agent_schemas import get_schema_context

logger = logging.getLogger("task_codegen")

_SRC_ROOT = Path(__file__).parent.parent  # hospilot-backend/src/

_AGENT_ACTIVITIES_FILE: dict[str, str] = {
    "er_agent":             "temporal/activities/er_activities.py",
    "icu_agent":            "temporal/activities/icu_activities.py",
    "bed_agent":            "temporal/activities/bed_activities.py",
    "discharge_agent":      "temporal/activities/discharge_activities.py",
    "staff_agent":          "temporal/activities/staff_activities.py",
    "pharmacy_agent":       "temporal/activities/pharmacy_activities.py",
    "ot_agent":             "temporal/activities/ot_activities.py",
    "revenue_agent":        "temporal/activities/revenue_activities.py",
    "billing_agent":        "temporal/activities/billing_activities.py",
    "housekeeping_agent":   "temporal/activities/housekeeping_activities.py",
}

_TOOL_TO_METHOD: dict[str, str] = {
    "fetch_er_visits":                "get_active_er_visits",
    "fetch_beds":                     "get_enriched_beds",
    "fetch_long_wait_visits":         "get_long_wait_er_visits(minutes=60)",
    "fetch_icu_admissions":           "get_icu_admissions",
    "fetch_available_icu_beds":       "get_available_icu_beds",
    "fetch_dirty_icu_beds":           "get_dirty_icu_beds",
    "fetch_admissions":               "get_discharge_eligible_admissions",
    "fetch_beds_summary":             "get_beds_summary",
    "fetch_discharge_eligible":       "get_discharge_eligible_admissions",
    "fetch_discharge_summaries":      "carerOS_get_discharge_summaries",
    "fetch_admissions_with_wards":    "get_admissions_with_wards",
    "fetch_nursing_tasks":            "get_all_incomplete_tasks",
    "fetch_discharge_with_summaries": "get_discharge_ready_with_summaries",
    "fetch_outstanding_invoices":     "get_outstanding_invoices",
    "fetch_claims":                   "carerOS_get_claims",
    "fetch_daily_collections":        "carerOS_get_daily_collections",
    "fetch_todays_collections":       "get_todays_collections",
    "fetch_ot_surgeries":             "carerOS_get_ot_surgeries",
    "fetch_postop_beds":              "get_available_postop_beds",
    "fetch_critical_vitals":          "get_critical_vitals",
    "fetch_overdue_tasks":            "get_overdue_nursing_tasks",
    "fetch_dirty_beds":               "get_dirty_beds",
    "fetch_recently_discharged_beds": "get_recently_discharged_beds",
}

_CODEGEN_PROMPT = """\
You are writing a Temporal activity function for Hospilot, a hospital AI system.

User wants this task:
  Label      : {label}
  Description: {description}
  Outputs    : {outputs}
  Agent      : {agent_id}
  Task ID    : {task_id}

Write a SINGLE Python async Temporal activity function with EXACTLY this signature:

@activity.defn(name="{task_id}")
async def {func_name}(session_id: str) -> dict:

These names are already imported -- do NOT import them again:
  activity, hasura, cache, broadcast

{schema_context}

Available hasura methods for {agent_id}:
{hasura_methods}

Pattern to follow (copy the broadcast + return style exactly):
{pattern}

Rules:
- Use ONLY the hasura methods listed above
- Use ONLY the real column names from the schema above -- never invent field names
- To calculate elapsed time (wait time, LOS, TAT): use arrived_at or admitted_at or ordered_at with datetime.now(timezone.utc), import datetime inside the function
- At the start: await broadcast(session_id, {{"type": "sub_agent_started", "sub_agent": "{task_id}"}})
- At the end  : await broadcast(session_id, {{"type": "sub_agent_completed", "sub_agent": "{task_id}", "result": result}})
- Return a plain dict named `result` matching the output fields
- Handle empty data gracefully -- return 0 or [] instead of raising
- No Claude calls, no extra imports at module level, no helper functions, one function only

After the closing line of the function, add exactly this line:
GENERATED_TASKS["{task_id}"] = {func_name}

Return ONLY the Python code. No explanation, no markdown fences."""


def _hasura_methods_for(agent_id: str) -> str:
    base = agent_id.split(":")[0]
    tools = AGENT_FETCH_TOOLS.get(base, {})
    if not tools:
        return "  hasura.get_enriched_beds()\n  hasura.get_active_er_visits()"
    return "\n".join(
        f"  await hasura.{_TOOL_TO_METHOD.get(name, name.replace('fetch_', 'get_'))}()"
        for name in tools
    )


def _pattern_for(agent_id: str) -> str:
    base = agent_id.split(":")[0]
    rel  = _AGENT_ACTIVITIES_FILE.get(base)
    if not rel:
        return "(no pattern available)"
    path = _SRC_ROOT / rel
    if not path.exists():
        return "(file not found)"
    text = path.read_text(encoding="utf-8")
    # grab the first complete @activity.defn block (up to 35 lines)
    m = re.search(r"(@activity\.defn.*?)(?=\n@activity\.defn|\nclass |\Z)", text, re.DOTALL)
    if m:
        return "\n".join(m.group(1).splitlines()[:35])
    return text[:600]


async def generate_task_code(
    task_id: str,
    label: str,
    description: str,
    outputs: list[str],
    agent_id: str,
) -> str:
    """
    Generate a @activity.defn Python function for this task.
    Returns the code string ready to append to generated_activities.py.
    """
    func_name = task_id.replace("-", "_")

    prompt = _CODEGEN_PROMPT.format(
        task_id=task_id,
        func_name=func_name,
        label=label,
        description=description or label,
        outputs=", ".join(outputs) if outputs else "summary, result",
        agent_id=agent_id,
        schema_context=get_schema_context(agent_id),
        hasura_methods=_hasura_methods_for(agent_id),
        pattern=_pattern_for(agent_id),
    )

    print(f"\n{'-'*60}")
    print(f"  CODEGEN  task_id  : {task_id}")
    print(f"           agent    : {agent_id}")
    print(f"           label    : {label}")
    print(f"           outputs  : {outputs or ['(auto)']}")
    print(f"  -> calling quality-tier model to generate activity function...")

    code = await llm_chat(user=prompt, max_tokens=1024, tier="quality")
    code = code.strip()

    # strip markdown fences if the model wrapped the code
    if code.startswith("```"):
        code = code.split("```")[1]
        if code.startswith("python"):
            code = code[6:]
        code = code.strip()
    if code.endswith("```"):
        code = code[:-3].strip()

    print(f"  [ok] generated {len(code.splitlines())} lines of Python")
    print(f"{'-'*60}")
    print(code)
    print(f"{'-'*60}\n")

    logger.info("[ok] codegen done  task=%s  lines=%d", task_id, len(code.splitlines()))
    return code
