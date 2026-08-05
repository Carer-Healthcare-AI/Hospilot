"""
Dynamic task executor -- handles tasks added via the UI (is_dynamic=True in task_registry).

Two guardrail checkpoints:

  Checkpoint 1 -- Creation time (validate_new_task):
    Called by POST /api/registry/tasks before writing to the DB.
    The fast-tier model checks whether the task's data requirements are
    satisfiable from this agent's declared data sources (manifest.py).
    Rejects the task if it needs tables/keys not available to the agent.

  Checkpoint 2 -- Execution time (execute_dynamic_task):
    Called by the Temporal activity when a task_id has no hardcoded handler.
    The fast-tier model checks whether live session context has enough data
    for the task. If OK -> the quality-tier model executes the task using the
    label as instruction. If not -> returns status=skipped_insufficient_data
    (pipeline continues).
"""

import json
import logging

from llm_client import llm_chat, llm_json_prefill, llm_agentic_loop
from agents._shared.manifest import get_manifest
from agents._shared.fetch_tools import get_fetch_tools
from agents._shared.agent_schemas import get_schema_context

logger = logging.getLogger("dynamic_task")

# -- Prompt templates ----------------------------------------------------------

_CREATION_GUARDRAIL = """\
You are a backend validator for Hospilot, a hospital AI orchestration system.

A user wants to add a new task to the {agent_label} agent in the registry.
Your job: decide if this task can be executed using ONLY the data sources \
already available to this agent.

Task label      : {label}
Task description: {description}
Expected outputs: {outputs}

Data available to {agent_label}:
  Redis keys    : {redis_keys}
  Session context fields from prior tasks: {context_fields}

{schema_context}

Rules:
- If the task can be answered purely from the listed columns -> valid: true
- If it needs a column, table, or external API NOT listed above -> valid: false
- If the user references a field that doesn't exist but the same data CAN be derived \
from available columns (e.g. wait_time from arrived_at), mark valid: true and explain in reason
- Uncertainty: if you are unsure, mark valid: true (don't block on guesses)

Reply with ONLY valid JSON, no markdown:
{{"valid": true/false, "reason": "<one sentence>", "missing_sources": ["<source>", ...]}}"""


_EXECUTION_GUARDRAIL = """\
You are checking whether a Hospilot pipeline task has enough context to run.

Task  : {label}
Outputs expected: {outputs}
Available session context keys: {context_keys}

Can this task produce meaningful results from the available context?
Reply with ONLY valid JSON:
{{"can_execute": true/false, "reason": "<brief reason>"}}"""


_EXECUTION_PROMPT = """\
You are a sub-agent in Hospilot, a hospital AI pipeline.

Your task: {label}
Produce a JSON object with these output fields: {outputs}

Session context (data from prior tasks in this pipeline):
{context}

Instructions:
- Use actual values from the context wherever possible.
- If a value cannot be determined, use null.
- Return ONLY valid JSON matching the output fields -- no prose, no markdown."""

_EXECUTION_PROMPT_TOOLS = """\
You are a sub-agent in Hospilot, a hospital AI pipeline.

Your task: {label}
Produce a JSON object with these output fields: {outputs}

Session context (data from prior tasks in this pipeline):
{context}

You have access to live data-fetch tools. Use them ONLY if the session context \
above is missing data required to complete the task. Prefer context data first.

When finished, return ONLY valid JSON matching the output fields. No prose, no markdown, \
no code fences."""


# -- Quality-tier execution helpers ---------------------------------------------

async def _execute_prefill(label: str, outputs: list[str], context_str: str) -> str:
    """Simple execution: ask the model to return raw JSON without prose."""
    return await llm_json_prefill(
        user=_EXECUTION_PROMPT.format(
            label=label,
            outputs=", ".join(outputs) if outputs else "summary",
            context=context_str,
        ),
        max_tokens=1024,
        tier="quality",
    )


async def _execute_with_tools(
    task_id: str,
    label: str,
    outputs: list[str],
    context_str: str,
    tool_schemas: list[dict],
    tool_impls: dict,
) -> str:
    """Agentic tool-use loop: the model may call data-fetch tools when context
    is insufficient. Runs up to 4 rounds before returning whatever text it has."""
    return await llm_agentic_loop(
        user=_EXECUTION_PROMPT_TOOLS.format(
            label=label,
            outputs=", ".join(outputs) if outputs else "summary",
            context=context_str,
        ),
        tool_schemas=tool_schemas,
        tool_impls=tool_impls,
        max_tokens=2048,
        max_rounds=4,
        tier="quality",
        context_label=task_id,
    )


# -- Checkpoint 1: creation-time guardrail -------------------------------------

async def validate_new_task(
    agent_id: str,
    agent_label: str,
    label: str,
    description: str,
    outputs: list[str],
) -> dict:
    """
    Run Claude Haiku to check if a new task is compatible with this agent's
    data sources. Called before writing to task_registry.

    Returns:
        {"valid": bool, "reason": str, "missing_sources": list[str]}
    """
    manifest = get_manifest(agent_id)
    if not manifest:
        return {
            "valid": False,
            "reason": f"Agent '{agent_id}' not found in data manifest -- cannot validate.",
            "missing_sources": [],
        }

    prompt = _CREATION_GUARDRAIL.format(
        agent_label=agent_label,
        label=label,
        description=description or label,
        outputs=", ".join(outputs) if outputs else "none specified",
        redis_keys=", ".join(manifest.redis_keys),
        context_fields=", ".join(manifest.context_fields),
        schema_context=get_schema_context(agent_id),
    )

    try:
        text = await llm_chat(user=prompt, max_tokens=256, tier="fast")
        text = text.strip()
        # Strip markdown fences if model wraps JSON in ```json ... ```
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        result = json.loads(text)
        logger.info(
            "creation guardrail  agent=%s  task=%s  valid=%s  reason=%s",
            agent_id, label, result.get("valid"), result.get("reason", ""),
        )
        return result
    except json.JSONDecodeError:
        # Haiku returned non-JSON -- don't block on guardrail parse errors
        logger.warning("creation guardrail parse error  raw=%.120s", text)
        return {"valid": True, "reason": "guardrail parse error -- allowed through", "missing_sources": []}
    except Exception as exc:
        logger.warning("creation guardrail failed  err=%s -- allowing through", exc)
        return {"valid": True, "reason": f"guardrail unavailable ({exc})", "missing_sources": []}


# -- Checkpoint 2: execution-time fallback ------------------------------------

async def execute_dynamic_task(
    task_id: str,
    task_label: str,
    task_outputs: list[str],
    agent_id: str,
    context: dict,
) -> dict:
    """
    Fallback executor for task IDs that have no hardcoded Temporal activity.

    1. Haiku checks whether live session context is sufficient.
    2. If yes -> Sonnet executes the task using the label as instruction.
    3. If no  -> returns skipped_insufficient_data (pipeline continues normally).
    """
    context_keys = [k for k in context if not k.startswith("_")]

    # -- Execution-time guardrail (fast tier) ----------------------------------
    try:
        guard_text = await llm_chat(
            user=_EXECUTION_GUARDRAIL.format(
                label=task_label,
                outputs=", ".join(task_outputs) if task_outputs else "none",
                context_keys=", ".join(context_keys) or "none",
            ),
            max_tokens=128, tier="fast",
        )
        guard_text = guard_text.strip()
        if guard_text.startswith("```"):
            guard_text = guard_text.split("```")[1]
            if guard_text.startswith("json"):
                guard_text = guard_text[4:]
            guard_text = guard_text.strip()
        guard = json.loads(guard_text)
    except Exception as exc:
        logger.warning("execution guardrail error  task=%s  err=%s -- proceeding", task_id, exc)
        guard = {"can_execute": True}

    manifest = get_manifest(agent_id)
    tool_schemas = manifest.tool_schemas if manifest else []
    tool_impls   = get_fetch_tools(agent_id)

    if not guard.get("can_execute", True) and not tool_schemas:
        # Skip only when context is insufficient AND there are no fetch tools to compensate
        logger.warning(
            "dynamic task skipped -- insufficient context  task=%s  reason=%s",
            task_id, guard.get("reason", ""),
        )
        return {
            "status": "skipped_insufficient_data",
            "task_id": task_id,
            "reason": guard.get("reason", "Insufficient session context"),
        }

    # -- Execute with Claude Sonnet (tool-use loop) ----------------------------
    context_str = json.dumps(
        {k: v for k, v in context.items() if not k.startswith("_")},
        indent=2,
        default=str,
    )[:4000]

    try:
        if tool_schemas:
            result_text = await _execute_with_tools(
                task_id, task_label, task_outputs, context_str, tool_schemas, tool_impls
            )
        else:
            result_text = await _execute_prefill(task_label, task_outputs, context_str)

        # Normalise: strip markdown fences and find the JSON object
        if result_text.startswith("```"):
            result_text = result_text.split("```")[1]
            if result_text.startswith("json"):
                result_text = result_text[4:]
            result_text = result_text.strip()
        j_start = result_text.find("{")
        if j_start > 0:
            result_text = result_text[j_start:]
        j_end = result_text.rfind("}")
        if j_end != -1 and j_end < len(result_text) - 1:
            result_text = result_text[:j_end + 1]

        result = json.loads(result_text)
    except json.JSONDecodeError:
        result = {"raw_output": result_text}
    except Exception as exc:
        logger.error("dynamic task execution failed  task=%s  err=%s", task_id, exc)
        return {"status": "failed", "task_id": task_id, "error": str(exc)}

    logger.info(
        "[ok] dynamic task executed  task=%s  outputs=%s",
        task_id, list(result.keys()),
    )
    return {"status": "completed", "task_id": task_id, **result}
