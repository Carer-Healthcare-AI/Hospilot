"""Conditional-edge / cascade-skip evaluation -- ported from api.agents.

In the old dispatcher, `_advance_plan` evaluated each target's incoming
conditions (OR logic) and cascading skips at the *target's* turn, against the
merged `context`. We preserve that exactly: every agent node always runs its
guard at entry, evaluating against `state["results"]`. A node that should not
run broadcasts `branch_skipped`, records itself in `_skipped`, and no-ops -- so
the skip cascades to its own children's guards downstream.

Resolution order for a condition string:
  1. Canonical resolver (pure Python, from known agent result fields)
  2. Typed "<field> <op> <number>" mini-syntax (pure Python)
  3. Haiku LLM fallback (for genuinely free-form conditions)

FAIL-OPEN: missing source / unresolved / LLM error all default to RUN (True).
Rationale: matches the task-level layer (TypedCondition.evaluate already does
missing->run); an infra error in the condition evaluator should not orphan a
reachable agent; every agent body no-ops gracefully on empty input; and
_evaluate_conditions ORs multiple conditions so fail-open cannot wrongly skip.
"""

import asyncio
import json
import logging
import operator
import re
from typing import Callable

from llm_client import llm_chat

logger = logging.getLogger(__name__)


def _base_id(agent_id: str) -> str:
    return agent_id.split(":")[0]


# ---------------------------------------------------------------------------
# Canonical condition resolvers
# Grounded in the REAL fields icu_agent / discharge_agent emit at runtime.
# icu_agent:   icu_full (bool), icu_available (int)
# discharge_agent: ready (int)
# icu_full / icu_not_full invert off the SAME field so they can never disagree.
# ---------------------------------------------------------------------------
_CANONICAL_RESOLVERS: dict[str, Callable[[dict], bool]] = {
    "icu_full":     lambda r: bool(r.get("icu_full")) if "icu_full" in r else (r.get("icu_available", 1) == 0),
    "icu_not_full": lambda r: (not r.get("icu_full")) if "icu_full" in r else (r.get("icu_available", 0) > 0),
    "has_discharge_candidates": lambda r: (r.get("ready", 0) or 0) > 0,
    # Registered sub-agent branch tokens (mirrors frontend OPPOSITE_CONDITIONS / POSITIVE_CONDITIONS).
    "dirty_beds":              lambda r: bool(r.get("dirty_beds") or r.get("dirty_count", 0)),
    "no_dirty_beds":           lambda r: not bool(r.get("dirty_beds") or r.get("dirty_count", 0)),
    "beds_available":          lambda r: bool(r.get("candidates") or r.get("available_beds", 0) or r.get("candidate_count", 0)),
    "no_beds_available":       lambda r: not bool(r.get("candidates") or r.get("available_beds", 0) or r.get("candidate_count", 0)),
    "er_critical_patients":    lambda r: bool(r.get("critical_patients")),
    "no_er_critical_patients": lambda r: not bool(r.get("critical_patients")),
    "discharge_ready":         lambda r: (r.get("ready", 0) or 0) > 0,
    "discharge_not_ready":     lambda r: (r.get("ready", 0) or 0) == 0,
    "candidates_found":        lambda r: bool(r.get("candidates") or r.get("candidate_count", 0)),
    "no_candidates_found":     lambda r: not bool(r.get("candidates") or r.get("candidate_count", 0)),
    "high_acuity":             lambda r: bool(r.get("high_acuity") or (r.get("triage_score", 5) or 5) <= 2),
    "low_acuity":              lambda r: not bool(r.get("high_acuity")) and (r.get("triage_score", 5) or 5) > 2,
    "ventilator_needed":       lambda r: bool(r.get("ventilator_needed") or r.get("ventilator_count", 0)),
    "no_ventilator_needed":    lambda r: not bool(r.get("ventilator_needed") or r.get("ventilator_count", 0)),
    "isolation_needed":        lambda r: bool(r.get("isolation_needed") or r.get("isolation_count", 0)),
    "no_isolation_needed":     lambda r: not bool(r.get("isolation_needed") or r.get("isolation_count", 0)),
    "bed_reserved":            lambda r: bool(r.get("bed_id") or r.get("reservation_made") or r.get("beds_reserved", 0)),
    "bed_not_reserved":        lambda r: not bool(r.get("bed_id") or r.get("reservation_made") or r.get("beds_reserved", 0)),
}

_COMPARATORS: dict[str, Callable] = {
    ">=": operator.ge, "<=": operator.le, "==": operator.eq,
    "!=": operator.ne, ">":  operator.gt, "<":  operator.lt,
}
_EXPR_RE = re.compile(r"^\s*([a-zA-Z_][\w.]*)\s*(>=|<=|==|!=|>|<)\s*(-?\d+(?:\.\d+)?)\s*$")


def _resolve_expr(condition: str, src: dict) -> bool | None:
    """Deterministically evaluate '<field> <op> <number>'. Returns None if not that shape."""
    m = _EXPR_RE.match(condition)
    if not m:
        return None
    field, op, raw = m.groups()
    field = field.split(".")[-1]          # tolerate 'ta_x.field' / 'icu_agent.field'
    actual = src.get(field)
    if actual is None:
        return True                        # missing field -> RUN (mirror TypedCondition.evaluate)
    try:
        actual = float(actual)
        thr = float(raw)
    except (TypeError, ValueError):
        return None                        # non-numeric -> let LLM fallback decide
    return _COMPARATORS[op](actual, thr)


def resolve_condition(condition: str, data: dict) -> bool:
    """Synchronous condition check against a flat data dict. Canonical resolvers first,
    then typed expressions, then fail-open. Used for subagent-level conditions where
    ``data`` is the merged view of task results accumulated so far in the agent body.
    No LLM fallback -- subagent conditions must be canonical or typed expressions."""
    key = condition.strip()
    if key in _CANONICAL_RESOLVERS:
        val = bool(_CANONICAL_RESOLVERS[key](data))
        logger.info("subcond  path=canonical  cond=%r -> %s", key, val)
        return val
    expr = _resolve_expr(condition, data)
    if expr is not None:
        logger.info("subcond  path=typed_expr  cond=%r -> %s", condition, expr)
        return expr
    logger.info("subcond  path=unresolvable  cond=%r -> RUN (fail-open)", condition)
    return True


async def _evaluate_condition(condition: str | None, condition_source: str | None, results: dict) -> bool:
    """Returns True if the condition passes (or if there is no condition).
    Resolution order: canonical resolver -> typed '<field> <op> N' -> Haiku LLM fallback.
    FAIL-OPEN: missing source / unresolved / LLM error all default to RUN."""
    if not condition:
        return True
    cs = condition_source or ""
    src = results.get(cs) or results.get(_base_id(cs), {})
    if not src:
        logger.info("cond resolve  path=missing_source  cond=%r src=%r -> RUN", condition, condition_source)
        return True

    key = condition.strip()
    if key in _CANONICAL_RESOLVERS:
        val = bool(_CANONICAL_RESOLVERS[key](src))
        logger.info("cond resolve  path=canonical  cond=%r -> %s", key, val)
        return val

    expr = _resolve_expr(condition, src)
    if expr is not None:
        logger.info("cond resolve  path=typed_expr  cond=%r -> %s", condition, expr)
        return expr

    try:
        reply = await llm_chat(
            user=(
                f"Agent result:\n{json.dumps(src, indent=2)}\n\n"
                f"Condition: \"{condition}\"\n\n"
                "Does the agent result satisfy this condition? Reply with only 'true' or 'false'."
            ),
            max_tokens=5,
        )
        val = reply.lower().startswith("true")
        logger.info("cond resolve  path=llm_fallback  cond=%r -> %s", condition, val)
        return val
    except Exception:
        logger.exception("cond eval failed  cond=%s src=%s -> RUN (fail-open)", condition, condition_source)
        return True


async def _evaluate_conditions(conditions: list[dict], results: dict) -> bool:
    """True if conditions list is empty, or if ANY condition passes (OR logic)."""
    if not conditions:
        return True
    outcomes = await asyncio.gather(*[
        _evaluate_condition(c.get("condition"), c.get("condition_source"), results)
        for c in conditions
    ])
    return any(outcomes)


def _predecessors_completed(required_predecessors: list[str], skipped: dict) -> bool:
    """False only if a predecessor was cascade-skipped (reason=predecessor_skipped).

    Predecessors that were condition-skipped (their own incoming edge condition
    evaluated to False) represent a branch-not-taken, not a cascade. Those should
    NOT propagate to successors that may still be reachable via another path.
    """
    for pred in (required_predecessors or []):
        reason = skipped.get(pred)
        if reason == "predecessor_skipped":
            return False
    return True


async def should_agent_run(cfg: dict, state: dict) -> tuple[bool, str | None]:
    """Replicates _advance_plan's per-target gate.

    Returns (run, skip_reason). skip_reason is the first condition text (or
    'predecessor_skipped') for the branch_skipped event.
    """
    results = state.get("results", {})
    skipped = state.get("_skipped", {})

    if not _predecessors_completed(cfg.get("required_predecessors", []), skipped):
        return False, "predecessor_skipped"

    conditions = cfg.get("conditions", [])
    if not await _evaluate_conditions(conditions, results):
        reason = conditions[0]["condition"] if conditions else "predecessor_skipped"
        return False, reason

    return True, None
