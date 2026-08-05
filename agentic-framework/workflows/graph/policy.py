"""Autonomy policy engine (Phase 5).

In autonomous mode, every mid-flow approval interrupt (`hitl.await_decision()`) is
evaluated against a generic, hospital-fillable rule structure to decide one of three
outcomes:

    auto_approve   -- routine: resume the flow immediately, no human park
    require_human  -- park in the Paused queue for a human (today's assisted behavior)
    escalate       -- park + run the EscalatingApprovalWorkflow ladder (high-risk)

This module is a PURE rule engine: `load_policy()` reads the rules once and
`evaluate(context)` returns a `Verdict`. All side effects (resuming, resolving the
approval row, notifying, escalating) live in the runner's `_apply_autonomous_policy`.
Assisted mode never calls this -- it is gated on `sessions.autonomous`.

Rule source: `settings.policy_rules_path` (a JSON file the hospital fills in; see
docs/agentic-framework/AUTONOMY_POLICY_TEMPLATE.md). Empty/missing/invalid -> the
built-in `DEFAULT_POLICY` (safe: only low-risk auto-approves, high-risk escalates,
everything else requires a human).

Rule structure (JSON):
    {
      "default": "require_human",
      "rules": [
        {"match": {"risk": "low"},  "outcome": "auto_approve", "decision": "approved"},
        {"match": {"risk": "high"}, "outcome": "escalate"},
        {"match": {"kind": "staff_approval", "risk": ["low", "medium"]},
         "outcome": "auto_approve", "notify": true}
      ]
    }

A rule matches when EVERY key in its `match` equals the context value (case-insensitive
string compare; a list value is a membership test). First matching rule wins (order =
priority). No match -> `default`. Context keys: `kind`, `agent_id`, `action_type`,
`risk`, `goal`, plus any interrupt-payload extras (bed_id, count, ...).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from config import settings

logger = logging.getLogger(__name__)

# The three outcomes, ordered by severity (used to pick the dominant verdict when a
# superstep parks multiple sibling interrupts).
AUTO_APPROVE = "auto_approve"
REQUIRE_HUMAN = "require_human"
ESCALATE = "escalate"

_SEVERITY = {AUTO_APPROVE: 0, REQUIRE_HUMAN: 1, ESCALATE: 2}
_VALID_OUTCOMES = set(_SEVERITY)

# Built-in fallback: risk-tiered, human-safe. Kinds that inherently need a human
# (patient identification/registration, a user pause) can never auto-resolve.
DEFAULT_POLICY: dict = {
    "default": REQUIRE_HUMAN,
    "rules": [
        {"match": {"kind": "patient_identification"}, "outcome": REQUIRE_HUMAN},
        {"match": {"kind": "patient_registration"}, "outcome": REQUIRE_HUMAN},
        {"match": {"kind": "user_paused"}, "outcome": REQUIRE_HUMAN},
        {"match": {"risk": "low"}, "outcome": AUTO_APPROVE, "decision": "approved"},
        {"match": {"risk": "medium"}, "outcome": REQUIRE_HUMAN},
        {"match": {"risk": "high"}, "outcome": ESCALATE},
    ],
}


@dataclass
class Verdict:
    outcome: str                 # auto_approve | require_human | escalate
    decision: str = "approved"   # resume value delivered on auto_approve
    notify: bool = True          # fire a policy notification for this event
    reason: str = ""             # human-readable trace (goes to WS + audit)
    rule: dict | None = None     # the matched rule, or None if the default fired


# -- Loading ------------------------------------------------------------------
_cache: dict | None = None


def load_policy() -> dict:
    """Load + parse the policy rules once (cached for the process lifetime).

    No-`--reload` deploy: edit the file then restart backend+worker to pick up
    changes. Any failure (missing path, unreadable, bad JSON, wrong shape) falls back
    to DEFAULT_POLICY and logs -- policy evaluation must never break a run."""
    global _cache
    if _cache is not None:
        return _cache

    path = (settings.policy_rules_path or "").strip()
    if not path:
        logger.info("policy: no POLICY_RULES_PATH set -- using built-in DEFAULT_POLICY")
        _cache = DEFAULT_POLICY
        return _cache

    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        policy = _normalise(raw)
        logger.info("policy: loaded %d rule(s) from %s  default=%s",
                    len(policy.get("rules", [])), path, policy.get("default"))
        _cache = policy
    except Exception:  # noqa: BLE001 -- never let a bad policy file break the run
        logger.exception("policy: could not load %s -- falling back to DEFAULT_POLICY", path)
        _cache = DEFAULT_POLICY
    return _cache


def reset_cache() -> None:
    """Drop the cached policy (tests only -- production reloads on process restart)."""
    global _cache
    _cache = None


def _normalise(raw: dict) -> dict:
    """Validate + coerce a loaded policy to the internal shape. Drops malformed rules
    rather than raising, so a partially-bad file still yields a usable policy."""
    if not isinstance(raw, dict):
        raise ValueError("policy root must be a JSON object")
    default = str(raw.get("default", REQUIRE_HUMAN))
    if default not in _VALID_OUTCOMES:
        logger.warning("policy: invalid default %r -- using %s", default, REQUIRE_HUMAN)
        default = REQUIRE_HUMAN
    rules = []
    for i, rule in enumerate(raw.get("rules", []) or []):
        if not isinstance(rule, dict):
            logger.warning("policy: rule #%d is not an object -- skipped", i)
            continue
        outcome = str(rule.get("outcome", ""))
        if outcome not in _VALID_OUTCOMES:
            logger.warning("policy: rule #%d has invalid outcome %r -- skipped", i, outcome)
            continue
        match = rule.get("match") or {}
        if not isinstance(match, dict):
            logger.warning("policy: rule #%d match is not an object -- skipped", i)
            continue
        rules.append({
            "match": match,
            "outcome": outcome,
            "decision": str(rule.get("decision", "approved")),
            "notify": bool(rule.get("notify", True)),
        })
    return {"default": default, "rules": rules}


# -- Evaluation ---------------------------------------------------------------

def _val_matches(want, have) -> bool:
    """A single match-key comparison. String compare is case-insensitive; a list want
    is a membership test (any element matches). Non-string scalars compare by equality
    after str()-normalising so `{"count": 3}` matches an int payload field."""
    have_s = "" if have is None else str(have).strip().lower()
    if isinstance(want, (list, tuple, set)):
        return any(_val_matches(w, have) for w in want)
    return str(want).strip().lower() == have_s


def _rule_matches(match: dict, context: dict) -> bool:
    return all(_val_matches(want, context.get(key)) for key, want in match.items())


def evaluate(context: dict, policy: dict | None = None) -> Verdict:
    """Return the policy Verdict for one parked interrupt.

    `context` is the interrupt payload (kind/agent_id/action_type/risk + extras),
    optionally augmented with `goal`. First matching rule wins; no match -> default.
    Never raises -- any error yields a REQUIRE_HUMAN verdict (fail safe)."""
    try:
        pol = policy or load_policy()
        for rule in pol.get("rules", []):
            if _rule_matches(rule["match"], context):
                return Verdict(
                    outcome=rule["outcome"],
                    decision=rule.get("decision", "approved"),
                    notify=rule.get("notify", True),
                    reason=f"matched rule {rule['match']} -> {rule['outcome']}",
                    rule=rule,
                )
        default = pol.get("default", REQUIRE_HUMAN)
        return Verdict(outcome=default, reason=f"no rule matched -> default {default}")
    except Exception:  # noqa: BLE001
        logger.exception("policy: evaluate failed for context=%s -- defaulting to require_human", context)
        return Verdict(outcome=REQUIRE_HUMAN, reason="policy evaluation error -- fail safe")


def dominant(verdicts: list[Verdict]) -> Verdict:
    """Collapse the verdicts of all interrupts parked in one superstep into a single
    outcome. Auto-approve requires unanimity AND a single shared decision value (so a
    Command(resume=...) can't be mis-delivered across siblings with different decisions);
    otherwise the highest-severity verdict wins (escalate > require_human)."""
    if not verdicts:
        return Verdict(outcome=REQUIRE_HUMAN, reason="no interrupts")
    if len(verdicts) == 1:
        return verdicts[0]
    if all(v.outcome == AUTO_APPROVE for v in verdicts) and len({v.decision for v in verdicts}) == 1:
        return verdicts[0]
    non_auto = [v for v in verdicts if v.outcome != AUTO_APPROVE]
    if not non_auto:
        # All auto-approve but with differing decision values (the unanimity check above
        # failed): a single Command(resume=...) can't safely satisfy siblings expecting
        # different decisions, so park for a human rather than mis-deliver.
        return Verdict(outcome=REQUIRE_HUMAN,
                       reason="mixed auto-approve decisions -- parking for safety")
    return max(non_auto, key=lambda v: _SEVERITY.get(v.outcome, 1))
