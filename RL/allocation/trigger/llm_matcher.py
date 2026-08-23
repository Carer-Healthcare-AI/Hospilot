"""Semantic fallback for use-case resolution — an LLM behind the token matcher.

The token matcher (:mod:`allocation.profiles.registry`) is deterministic, offline and free,
and it stays first: this module is consulted only after it finds nothing. It asks Claude
which registered resource the sentence names, with the answer constrained by structured
output to the registered resource types plus ``"unknown"`` — the model cannot invent a
profile that does not exist.

Three rules keep the convenience from becoming a hazard:

**Off by default.** Resolution must stay deterministic in tests and offline runs, so the
fallback only runs when ``ALLOCATION_LLM_QUERY`` is set to a truthy value. Without it,
:func:`~allocation.trigger.query.resolve_profile` behaves exactly as before this module
existed — no network, no new dependency exercised.

**"unknown" raises, exactly like today.** The prompt gives ``"unknown"`` as a first-class
answer and tells the model to prefer it over a guess. Anything short of a confident match
degrades to the same :class:`~allocation.trigger.query.UnknownUseCase` the token matcher
raises. There is still no default profile. Any failure — SDK not installed, no credentials,
timeout, refusal — degrades the same way rather than surfacing a new error class.

**It is consulted whenever the tokens are inconclusive**, which is two cases, not one: no
profile matched at all, or several matched and none more closely than the others. The second
is the ambiguity a bed family creates — a sentence can name two units — and on that path the
model's answer is accepted **only if** it is one of the profiles the tokens already found.
The tokens establish which readings are plausible; the model chooses between them.

**Every LLM resolution is visible in the trace.** Resolutions are memoized per normalised
query, and :func:`~allocation.trigger.query.matched_tokens` reads that memo, so the first
line of a run says the resolution was semantic rather than pretending tokens matched.
"""

from __future__ import annotations

import json
import os

from allocation.contracts import ResourceType
from allocation.profiles.registry import REGISTRY, normalise

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_MODEL_ENV = "ALLOCATION_LLM_MODEL"
_DEFAULT_MODEL = "claude-opus-5"

#: normalised query -> what the LLM resolved it to (None = it answered "unknown" or failed).
#: Doubles as the record matched_tokens() reads to label a resolution "llm" in the trace.
_memo: dict[str, ResourceType | None] = {}


def enabled() -> bool:
    """Whether the fallback may run at all. Explicit opt-in via ALLOCATION_LLM_QUERY."""
    return os.environ.get("ALLOCATION_LLM_QUERY", "").strip().lower() in _TRUTHY


def cached(query: str) -> ResourceType | None:
    """What this process's LLM resolved the query to, without making a call."""
    return _memo.get(normalise(query))


def resolve(query: str) -> ResourceType | None:
    """The registered resource the LLM says the query names, or ``None``.

    ``None`` means the caller raises exactly as it would without this module — disabled,
    unavailable, failed and "unknown" are deliberately indistinguishable to the caller.
    """
    if not enabled():
        return None
    key = normalise(query)
    if key in _memo:
        return _memo[key]
    resolved = _ask_claude(query)
    _memo[key] = resolved
    return resolved


def _schema() -> dict[str, object]:
    """Structured-output schema: the registered resource types, plus "unknown"."""
    values = [profile.resource_type.value for profile in REGISTRY.all()]
    return {
        "type": "object",
        "properties": {
            "resource_type": {"type": "string", "enum": [*values, "unknown"]},
        },
        "required": ["resource_type"],
        "additionalProperties": False,
    }


def _system_prompt() -> str:
    """Built from the registry so a second resource type needs no edit here.

    The resource *list* was always generated. The prose around it was not, and it used to
    state that HDU and PACU beds are "alternative pathways, not ICU beds — they are
    ``unknown``". Once those became registered resources in their own right, the prompt named
    them in its list and forbade them two paragraphs later. The rule that replaced it —
    *resolve to the unit the query names* — is one the generated list cannot contradict.
    """
    lines = [
        "You classify a hospital resource-allocation query to the single registered",
        'resource it asks to auction, or to "unknown".',
        "",
        "Registered resources:",
    ]
    for profile in REGISTRY.all():
        lines.append(f'- "{profile.resource_type.value}": {profile.description}')
    lines += [
        "",
        "RESOLVE TO THE UNIT THE QUERY NAMES. Each resource above is a bed in one specific",
        "unit, and they are not interchangeable — an HDU bed is not an ICU bed. A query",
        'naming HDU resolves to "hdu_bed", never to "icu_bed", and the same holds for every',
        "other unit. Do not treat any unit as a substitute for, or a lesser version of,",
        "another.",
        "",
        "NAME THE UNIT, NOT THE BIDDER. Departments compete for beds, so a query often names",
        "a department and a unit in the same sentence. The department is who is asking; the",
        "unit is what is being auctioned.",
        '  - "Should ER or OT get the ICU bed?" -> "icu_bed" (ER and OT are bidders)',
        '  - "ICU wants to step a patient down to the ward" -> "ward_bed"',
        '  - "ER, OT and ICU/Ward demand compete for one limited ICU bed" -> "icu_bed"',
        "",
        'ANSWER "unknown" WHEN NO UNIT IS IDENTIFIABLE. "Who should get the next available',
        'bed?" names a bed but no unit, so it is "unknown" unless the surrounding sentence',
        "identifies one. Ventilators, dialysis chairs, ambulances, theatre slots and anything",
        'else not listed above are "unknown" too.',
        "",
        'Prefer "unknown" over a guess: a wrong resolution scores the wrong resource against',
        'the wrong caps and nothing downstream can detect it, while an "unknown" merely asks',
        "the operator to name the unit. Semantics count, not keywords:",
        '  - "a spot is opening up in intensive care at 3pm" -> "icu_bed" (never says "bed")',
        '  - "we will have a step-down space free after the ward round" -> "hdu_bed"',
        '  - "recovery bay 4 is clear" -> "pacu_bed"',
    ]
    return "\n".join(lines)


def _ask_claude(query: str) -> ResourceType | None:
    """One classification call. Every failure path returns None, never raises."""
    try:
        import anthropic
    except ImportError:
        return None  # optional extra not installed — pip install .[llm]

    try:
        client = anthropic.Anthropic(timeout=15.0, max_retries=1)
        response = client.beta.messages.create(
            model=os.environ.get(_MODEL_ENV, _DEFAULT_MODEL),
            max_tokens=1024,
            # Thinking is ON by default on Claude Opus 5, and max_tokens caps thinking and
            # response text *together* — so a 1024 budget meant for `{"resource_type": ...}`
            # was being shared with reasoning this task does not need. A long think would
            # return stop_reason "max_tokens", the guard below would return None, and the
            # query would degrade to UnknownUseCase with nothing to distinguish it from a
            # genuine "unknown". Disabling is permitted here because effort is `low`; the API
            # rejects thinking: disabled only at `xhigh`/`max`. Raise effort and this must go.
            thinking={"type": "disabled"},
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _schema()}},
            # A safety-classifier decline re-runs on Anthropic's recommended fallback model
            # instead of dead-ending; if the whole chain declines we return None below.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=_system_prompt(),
            messages=[{"role": "user", "content": query.strip()}],
        )
        if response.stop_reason != "end_turn":
            return None  # refusal or truncation — nothing trustworthy to read
        text = next(block.text for block in response.content if block.type == "text")
        value = json.loads(text)["resource_type"]
    except Exception:
        return None  # a failing fallback degrades to the matcher's own error, never a new one

    if value == "unknown":
        return None
    try:
        return ResourceType(value)
    except ValueError:
        return None
