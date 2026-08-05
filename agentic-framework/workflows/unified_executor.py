"""
Unified code-gen executor.

Replaces direct LLM calls that process structured patient data.

Flow per call:
  1. Check in-memory cache
  2. Check task_registry DB
  3. Generate via LLM (schema only — no patient data)
  4. Store in DB + memory cache
  5. Run via RestrictedPython locally

LLM sees: field names and types only.
Patient data never leaves the process.
"""

import json
import logging
import math
import re

from RestrictedPython import compile_restricted, safe_builtins, safe_globals
from RestrictedPython.Eval import default_guarded_getiter, default_guarded_getitem
from RestrictedPython.Guards import safe_globals as _rp_safe_globals, guarded_iter_unpack_sequence

from llm_client import llm_chat
from db.hasura import hasura

logger = logging.getLogger("unified_executor")

_cache: dict[str, str] = {}

_TASK_SUBAGENT: dict[str, str] = {
    "exec__assign_ambulance":                "sa_ambulance_dispatch",
    "exec__assess_discharge":                "sa_discharge_ready",
    "exec__forecast_capacity":               "sa_bed_pred_forecast",
    "exec__analyze_staffing":                "sa_ratio_monitor",
    "exec__rank_beds":                       "sa_bed_ranking",
    "exec__rank_icu_admissions":             "sa_icu_census",
    "exec__analyze_icu":                     "sa_icu_stepdown",
    "exec__triage_er_visits":                "sa_er_triage",
    "exec__predict_ot_delays":               "sa_ot_turnaround",
    "exec__coordinate_ot_staff":             "sa_ot_turnaround",
    "exec__handle_ot_emergencies":           "sa_ot_emergency",
    "exec__optimise_ot_slots":               "sa_ot_scheduling",
    "exec__balance_ot_load":                 "sa_ot_scheduling",
    "exec__predict_denial_risk":             "sa_rev_denial_prevention",
    "exec__generate_billing_recommendations":"sa_billing_optimization",
    "exec__analyse_supply_risk":             "sa_stock_monitor",
    "exec__detect_infection_clusters":       "sa_icu_census",
    "exec__optimize_package_utilization":    "sa_rev_optimization",
    "exec__escalation_recommendations_rev":  "sa_rev_denial_prevention",
}

_SAFE_GLOBALS = {
    **safe_globals,
    "__builtins__": {
        **safe_builtins,
        "len": len,
        "range": range,
        "enumerate": enumerate,
        "zip": zip,
        "map": map,
        "filter": filter,
        "sorted": sorted,
        "min": min,
        "max": max,
        "sum": sum,
        "abs": abs,
        "round": round,
        "int": int,
        "float": float,
        "str": str,
        "bool": bool,
        "list": list,
        "dict": dict,
        "set": set,
        "tuple": tuple,
        "isinstance": isinstance,
        "any": any,
        "all": all,
    },
    "_getiter_": default_guarded_getiter,
    "_getitem_": default_guarded_getitem,
    "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
    "_getattr_": getattr,
    "_write_": lambda x: x,
    "_inplacevar_": lambda op, x, y: (
        x + y if op == "+=" else
        x - y if op == "-=" else
        x * y if op == "*=" else
        x / y if op == "/=" else
        x // y if op == "//=" else
        x % y if op == "%=" else
        x ** y if op == "**=" else
        (_ for _ in ()).throw(TypeError(f"unsupported in-place op: {op}"))
    ),
    "re": re,
    "math": math,
}

_CODEGEN_PROMPT = """\
Write a single pure Python function for a hospital operations system.

Task ID   : {task_id}
Description: {description}

Input schema (field names and types — no real values):
{input_schema}

Output fields: {output_fields}

Function signature (exact):
def execute(inp: dict) -> dict:

Rules:
- Pure computation only — no imports, no I/O, no network calls, no side effects
- Available builtins: len, range, enumerate, zip, map, filter, sorted, min, max,
  sum, abs, round, int, float, str, bool, list, dict, set, tuple, isinstance, any, all
- Available modules: re, math (already in scope — do NOT import them)
- Use inp.get(field, default) for all field access — never assume a field exists
- ALWAYS end with a return statement that returns a dict with exactly these keys: {output_fields}
- Every code path must reach a return statement — never fall off the end of the function
- Handle empty lists and None values gracefully with safe defaults, not None returns
- Never call a method directly on a value that could be None — coerce to a safe default first: (value or "").lower(), not value.lower()
- Write each statement on a single line; do not use backslash line continuation or multi-line expressions

Return ONLY the function code. No explanation, no markdown fences."""


async def _generate(task_id: str, description: str, input_schema: dict, output_fields: list[str]) -> str:
    logger.info("codegen  task=%s", task_id)
    prompt = _CODEGEN_PROMPT.format(
        task_id=task_id,
        description=description,
        input_schema=json.dumps(input_schema, indent=2),
        output_fields=", ".join(output_fields),
    )
    code = await llm_chat(user=prompt, max_tokens=4096, tier="quality")
    code = code.strip()
    if code.startswith("```"):
        code = code.split("```")[1]
        if code.startswith("python"):
            code = code[6:]
        code = code.strip()
    if code.endswith("```"):
        code = code[:-3].strip()
    logger.info("codegen done  task=%s  lines=%d", task_id, len(code.splitlines()))
    return code


_LABEL_PROMPT = """\
A hospital operations system runs the task described below. Write a short, user-facing
label and a one-line description for it, suitable for showing to hospital staff in a plan.

Task description:
{description}

Requirements:
- label: 3-6 words naming what the task does (e.g. "Predict claim denial risk")
- description: one plain-English sentence
- Plain language only. No system or code jargon -- no ids, field names, output keys,
  "cache", "queue", "exec", "registry", or anything technical.
- Sentence-case both (capitalise only the first word and proper nouns).

Return ONLY a JSON object: {{"label": "...", "description": "..."}}"""


async def _generate_meta(task_id: str, description: str) -> tuple[str, str]:
    """Generate a short, non-technical label + description for the plan UI.

    Returns ("", "") on any failure so the DB layer falls back to a readable id.
    """
    try:
        text = await llm_chat(
            user=_LABEL_PROMPT.format(description=description or task_id),
            max_tokens=200, tier="quality",
        )
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        data = json.loads(text)
        label = (data.get("label") or "").strip()
        desc = (data.get("description") or "").strip()
        if label:
            logger.info("label gen  task=%s  label=%r", task_id, label)
            return label, (desc or label)
    except Exception as exc:
        logger.warning("label gen failed  task=%s  err=%s -- using id fallback", task_id, exc)
    return "", ""


def _run(task_id: str, code: str, inp: dict) -> dict:
    byte_code = compile_restricted(code, f"<exec:{task_id}>", "exec")
    local_vars: dict = {}
    exec(byte_code, _SAFE_GLOBALS, local_vars)  # noqa: S102
    return local_vars["execute"](inp)


async def execute(
    task_id: str,
    description: str,
    input_schema: dict,
    output_fields: list[str],
    input_data: dict,
) -> dict:
    # 1. memory cache
    code = _cache.get(task_id)

    # 2. DB cache
    if not code:
        row = await hasura.fetch_function_code(task_id)
        if row and row.get("function_code"):
            code = row["function_code"]
            _cache[task_id] = code

    # Generate the function code plus a user-facing label/description, then store all
    # three on the registry row. The label/description are what the plan UI shows, so
    # they are generated in plain language rather than left as the raw "exec__..." id.
    async def _regen() -> str:
        subagent_id = _TASK_SUBAGENT.get(task_id, "")
        if not subagent_id:
            raise ValueError(f"unified_executor: no subagent mapping for task_id={task_id!r} — add it to _TASK_SUBAGENT")
        new_code = await _generate(task_id, description, input_schema, output_fields)
        label, desc = await _generate_meta(task_id, description)
        await hasura.upsert_executor_code(task_id, subagent_id, new_code, label=label, description=desc)
        _cache[task_id] = new_code
        return new_code

    # 3. generate + store
    if not code:
        code = await _regen()

    # 4. run
    needs_regen = False
    result = None
    try:
        result = _run(task_id, code, input_data)
        if not isinstance(result, dict):
            logger.warning("codegen returned non-dict (%s)  task=%s -- invalidating", type(result).__name__, task_id)
            needs_regen = True
    except SyntaxError as exc:
        logger.warning("codegen syntax error  task=%s  err=%s -- invalidating", task_id, exc)
        needs_regen = True

    if needs_regen:
        _cache.pop(task_id, None)
        code = await _regen()
        result = _run(task_id, code, input_data)
        if not isinstance(result, dict):
            raise RuntimeError(f"codegen produced invalid result after retry  task={task_id}  type={type(result).__name__}")
    return result


def invalidate(task_id: str) -> None:
    """Force regeneration on next call (e.g. after schema change)."""
    _cache.pop(task_id, None)
