"""Human-readable execution trace for the frontend.

Wherever the orchestration emits a Langfuse span (task activities, agent nodes,
run/skip decisions) we also call `record_step(...)`. It turns the raw arg/result
dicts into a *humanized* step record -- a readable title, a one-line summary, and
label/value field lists -- then:

  1. persists it to a per-session Redis list (`cache.append_trace_step`), so a
     reconnecting / late-joining client can fetch the whole trace, and
  2. broadcasts a `{"type": "trace_step", ...}` event over the existing WebSocket
     channel (`api.routes.ws.broadcast`) for live display.

The persisted record and the broadcast event carry the SAME already-humanized
payload, so the frontend renders identical content whether it streamed live or
fetched after the fact via `GET /api/sessions/{id}/trace`.

This is independent of Langfuse (it works with tracing disabled) and is fully
defensive: a trace failure must never break the run, mirroring the try/except
discipline around every Langfuse span call.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

# Keys we never surface to the end user (graph/internal plumbing).
_INTERNAL_PREFIX = "_"
_MAX_FIELDS = 12          # cap fields per input/output block
_MAX_STR = 200            # truncate long string values
_MAX_LIST_INLINE = 5      # lists this short are shown comma-joined
_LIST_PREVIEW = 3         # otherwise preview this many + "… (N total)"

# Tokens that read better cased a specific way when they form a whole word.
_ACRONYMS = {
    "id": "ID", "ids": "IDs", "icu": "ICU", "er": "ER", "ot": "OT",
    "sql": "SQL", "eta": "ETA", "tat": "TAT", "spo2": "SPO2", "gcs": "GCS",
    "bp": "BP", "uhid": "UHID", "tpa": "TPA", "ehr": "EHR", "fhir": "FHIR",
    "api": "API", "url": "URL", "ai": "AI", "los": "LOS",
}

_STATUS_WORD = {
    "running": "Running",
    "completed": "Completed",
    "failed": "Failed",
    "skipped": "Skipped",
}

# When previewing a list of objects, pick the first present of these keys as each
# item's short identifier ("what it is"), rather than collapsing to "N items".
_LABEL_KEYS = (
    "name", "full_name", "patient_name", "display_name", "title", "label",
    "bed_number", "bed_id", "bed", "room", "room_type", "ward", "ward_type",
    "vehicle_no", "vehicle_type",
    "chief_complaint", "complaint", "task", "reason", "code", "number",
    "invoice_number", "claim_id", "appointment_id", "slot",
    "patient_token", "uhid", "id",
)
# A second, qualifying detail appended in parentheses when present (severity/state).
_QUALIFIER_KEYS = (
    "triage_score", "ctas", "severity", "priority", "status", "state",
    "is_critical", "risk_score",
)
# Int keys whose name reads as a meaningful metric are surfaced in the summary.
_METRIC_HINTS = (
    "critical", "available", "occupied", "eligible", "pending", "matched",
    "reserved", "ready", "blocked", "unpaid", "overdue", "count", "total",
    "admitted", "discharged", "triaged", "saved", "assigned",
)

# Orchestration plumbing that leaks into inputs/outputs as if it were data. These
# are dropped from the rendered field rows. session_id is dropped too -- it's shown
# once at the top of the trace view, so repeating it on every step card is noise.
# agent_id / task_id stay on the step payload's top level (for grouping); they are
# only stripped here so they don't also appear as field rows.
_PLUMBING_KEYS = frozenset({
    "ta_results", "ctx", "context", "goal", "subgoal", "order",
    "task_label", "task_outputs", "available_tasks", "subagent_id",
    "session_id", "agent_id", "task_id",
})

# A bare-id key -> the readable sibling key `trace_enrich.enrich_output` adds next
# to it. When both are present in a step's dict, `to_fields` drops the bare-id row
# so the resolved detail replaces it rather than doubling the field count.
_ENRICHED_SUPERSEDES = {
    "patient_token": "patient",
    "token": "patient",
    "bed_id": "bed",
    "bed_ids": "bed_details",
    "patients": "patient_details",
    "patient_tokens": "patient_details",
    "assigned_vehicle_no": "assigned_ambulance",
    "vehicle_no": "ambulance",
    "approval_id": "approval",
}

# Vital-sign keys -> short display labels, so a decision task can show the clinical
# data it ranked on (e.g. "SpO2 88, HR 140, BP 80/60, RR 30").
_VITAL_LABELS = (
    ("spo2", "SpO2"),
    ("pulse", "HR"),
    ("heart_rate", "HR"),
    ("bp_systolic", "BP"),          # paired with bp_diastolic below
    ("respiratory_rate", "RR"),
    ("resp_rate", "RR"),
    ("temperature", "Temp"),
    ("temp", "Temp"),
    ("gcs", "GCS"),
)


# -- JSON-safe coercion --------------------------------------------------------
# Same shape as _lf_safe in workflows.graph.agents._activity, duplicated here so
# this module has no import dependency on the activity seam (which imports us).

def _coerce(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {k: _coerce(val) for k, val in dataclasses.asdict(v).items()}
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, dict):
        return {str(k): _coerce(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_coerce(i) for i in v]
    # Pydantic models (e.g. FHIR resources) -> plain dict, dropping the many empty
    # fields so a raw `Encounter(fhir_comments=None, ...)` repr never reaches the UI.
    dump = getattr(v, "model_dump", None)
    if callable(dump):
        try:
            return _coerce(dump(mode="json", exclude_none=True))
        except Exception:  # noqa: BLE001 -- fall through to json/repr
            pass
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return repr(v)[:_MAX_STR]


def _is_primitive(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool)) or v is None


# -- Null / unresolved-value handling ------------------------------------------
# Two rules the frontend trace enforces:
#   * inputs/outputs field rows: a null / unresolved scalar renders as "N/A"
#     (never "NULL", "None", "unspecified", ...), so an empty value is legible
#     rather than leaking a raw DB/placeholder token.
#   * one-line summaries: we only mention what we could actually resolve --
#     clauses that point at a null/unresolved value ("to unspecified",
#     "ETA: NULL mins") are dropped entirely rather than shown as N/A.

_NA = "N/A"

# Exact scalar values (case-insensitive) that mean "no data" -> normalized to N/A
# in field rows. Kept conservative: bare "na"/"nil" are excluded to avoid turning
# legitimate short codes into N/A.
_NULLISH = frozenset({
    "null", "none", "nan", "n/a", "unspecified", "unknown",
    "undefined", "tbd", "not specified", "not available",
})

# Standalone null *tokens* replaced with N/A wherever they appear inside a string
# value (e.g. a "NULL" coming straight from a SQL result serialized as text).
_NULL_TOKEN_RE = re.compile(r"\bnull\b|\bnone\b|\bnan\b", re.IGNORECASE)

# Alternation of null-ish words used for summary clause-dropping.
_NULL_ALT = "|".join(re.escape(t) for t in sorted(_NULLISH, key=len, reverse=True))
# A prepositional clause whose object is null-ish: " to unspecified", " from NULL".
_PREP_NULL_RE = re.compile(
    rf"\s*\b(?:to|at|from|for|in|on|by|via|of|near|with|as)\s+(?:{_NULL_ALT})\b",
    re.IGNORECASE,
)
# A "Label: value" segment whose value is null-ish, plus an optional trailing unit
# word: "ETA: NULL mins", "Priority: unspecified".
_LABEL_NULL_RE = re.compile(
    rf"\s*\b[A-Za-z][\w ]*?:\s*(?:{_NULL_ALT})\b(?:\s+[A-Za-z%]+)?",
    re.IGNORECASE,
)


def _is_nullish(s: str) -> bool:
    return s.strip().lower() in _NULLISH


def _normalize_null_tokens(s: str) -> str:
    """Replace standalone NULL/None/NaN tokens inside a string with N/A."""
    return _NULL_TOKEN_RE.sub(_NA, s)


def _sanitize_summary(text: str) -> str:
    """Drop clauses that reference null/unresolved values from a one-line summary,
    then tidy the dangling punctuation/whitespace the removals leave behind. Only
    what could be resolved survives -- "... to unspecified at unspecified. ETA:
    NULL mins from Bay 2" becomes "... from Bay 2". Best-effort and never raises."""
    if not text or not isinstance(text, str):
        return text
    try:
        s = _LABEL_NULL_RE.sub("", text)
        s = _PREP_NULL_RE.sub("", s)
        s = _normalize_null_tokens(s)              # any bare NULL left -> N/A
        s = re.sub(r"\s+", " ", s)
        s = re.sub(r"\(\s*[,;]?\s*\)", "", s)      # empty "()" left by a drop
        s = re.sub(r"\(\s+", "(", s)
        s = re.sub(r"\s+([.,;:)])", r"\1", s)      # space before punctuation
        s = re.sub(r"([.,;:])(?:\s*[.,;:])+", r"\1", s)  # collapse punctuation runs
        s = re.sub(r"(^|[.!?]\s+)([a-z])",
                   lambda m: m.group(1) + m.group(2).upper(), s)  # recapitalize
        return s.strip(" ,;:")
    except Exception:  # noqa: BLE001 -- tracing must never break the run
        return text


# -- Humanizers ----------------------------------------------------------------

def humanize_title(name: str) -> str:
    """Activity/agent identifier -> readable title.

    `ta_find_available_beds` -> "Find available beds"; `bed_agent` -> "Bed agent".
    """
    if not name:
        return "Step"
    s = str(name)
    for prefix in ("ta_", "sa_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.replace("_", " ").strip()
    if not s:
        return "Step"
    return s[0].upper() + s[1:]


def humanize_label(key: str) -> str:
    """snake_case key -> "Title case" label, upper-casing known acronyms.

    `bed_ids` -> "Bed IDs"; `patient_token` -> "Patient token".
    """
    if not key:
        return ""
    words = str(key).strip("_").split("_")
    out: list[str] = []
    for i, w in enumerate(words):
        if not w:
            continue
        wl = w.lower()
        if wl in _ACRONYMS:
            out.append(_ACRONYMS[wl])
        elif i == 0:
            out.append(w[0].upper() + w[1:])
        else:
            out.append(w)
    return " ".join(out) or str(key)


def _item_label(item: Any) -> str:
    """Short identifier for one list item -- "what it is", not a count.

    A patient dict -> "Ravi K. (CTAS-2)"; a bed dict -> "A-101 (Available)".
    Picks the first present name-ish key, then optionally appends one qualifier
    (severity/status) in parens. Falls back to the first primitive value.
    """
    d = _coerce(item)
    if _is_primitive(d):
        return humanize_value(d)
    if not isinstance(d, dict):
        return ""
    visible = {k: val for k, val in d.items() if not str(k).startswith(_INTERNAL_PREFIX)}

    def _usable(val: Any) -> bool:
        return val is not None and _is_primitive(val) and str(val).strip() != ""

    base = ""
    for k in _LABEL_KEYS:
        if k in visible and _usable(visible[k]):
            base = str(visible[k]).strip()
            break
    if not base:  # fall back to the first usable primitive value
        for val in visible.values():
            if _usable(val):
                base = str(val).strip()
                break
    if not base:
        return ""
    base = base if len(base) <= _MAX_STR else base[:_MAX_STR].rstrip() + "…"

    for k in _QUALIFIER_KEYS:
        if k in visible and k not in _LABEL_KEYS and _usable(visible[k]):
            qual = str(visible[k]).strip()
            if qual != base:
                return f"{base} ({humanize_value(visible[k])})"
    return base


def _clinical_detail(item: Any) -> str:
    """Decision-basis vitals for one item -- the clinical data a ranking or triage
    step acted on. Reads from the item itself or a nested `vitals` dict. Returns ""
    when there are no vitals to show. Severity/score is left to `_item_label`'s
    qualifier so it isn't shown twice.

    e.g. {"spo2": 88, "pulse": 140, "bp_systolic": 80, "bp_diastolic": 60}
         -> "SpO2 88, HR 140, BP 80/60".
    """
    d = _coerce(item)
    if not isinstance(d, dict):
        return ""
    src = d
    vitals = d.get("vitals")
    if isinstance(vitals, dict) and vitals:
        src = vitals

    def _usable(val: Any) -> bool:
        return val is not None and _is_primitive(val) and str(val).strip() != ""

    parts: list[str] = []
    seen: set[str] = set()
    for key, label in _VITAL_LABELS:
        if key in src and _usable(src[key]) and label not in seen:
            val = humanize_value(src[key])
            if label == "BP" and _usable(src.get("bp_diastolic")):
                val = f"{val}/{humanize_value(src['bp_diastolic'])}"
            parts.append(f"{label} {val}")
            seen.add(label)
    return ", ".join(parts)


def _forecast_detail(item: Any) -> str:
    """Surface the one usable number a ward-capacity forecast returns. The
    /bed/ward-capacity model populates predicted_occupied_beds and leaves the rest
    (available beds, utilization, capacity status) null, so the ward item would
    otherwise render as a bare ward_type with no forecast."""
    d = _coerce(item)
    if not isinstance(d, dict):
        return ""
    v = d.get("predicted_occupied_beds")
    if v is not None and _is_primitive(v) and str(v).strip() != "":
        return f"{humanize_value(v)} occupied (predicted)"
    return ""


def _list_item(item: Any) -> str:
    """Render one list item: its label, plus decision-basis detail when present."""
    label = _item_label(item)
    detail = _forecast_detail(item) or _clinical_detail(item)
    if label and detail:
        return f"{label} — {detail}"
    return label or detail


def humanize_value(v: Any) -> str:
    """Render a (coerced) value as a compact, readable string."""
    if v is None:
        return _NA
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        s = v.strip()
        if _is_nullish(s):
            return _NA
        s = _normalize_null_tokens(s)
        return s if len(s) <= _MAX_STR else s[:_MAX_STR].rstrip() + "…"
    if isinstance(v, list):
        if not v:
            return "none"
        if all(_is_primitive(i) for i in v):
            if len(v) <= _MAX_LIST_INLINE:
                return ", ".join(humanize_value(i) for i in v)
            head = ", ".join(humanize_value(i) for i in v[:_LIST_PREVIEW])
            return f"{head} … ({len(v)} total)"
        # List of objects: preview what the items actually ARE (+ decision-basis
        # detail like vitals when present), not "N items".
        labels = [lbl for lbl in (_list_item(i) for i in v) if lbl]
        if labels:
            if len(labels) <= _MAX_LIST_INLINE:
                return ", ".join(labels)
            head = ", ".join(labels[:_LIST_PREVIEW])
            return f"{head} … (+{len(v) - _LIST_PREVIEW} more, {len(v)} total)"
        return f"{len(v)} items"
    if isinstance(v, dict):
        if not v:
            return "none"
        visible = {k: val for k, val in v.items() if not str(k).startswith(_INTERNAL_PREFIX)}
        if not visible:
            return "none"
        if len(visible) <= 4 and all(_is_primitive(val) for val in visible.values()):
            return ", ".join(f"{humanize_label(k)}: {humanize_value(val)}" for k, val in visible.items())
        # A patient/decision context dict: show the clinical detail it carries
        # (vitals/scores) rather than just naming its fields.
        detail = _forecast_detail(v) or _clinical_detail(v)
        if detail:
            label = _item_label(v)
            return f"{label} — {detail}" if label else detail
        # Larger/nested dict: name the fields ("what's in it"), not "N fields".
        labels = [humanize_label(k) for k in visible]
        shown = ", ".join(labels[:_MAX_LIST_INLINE + 1])
        extra = len(labels) - min(len(labels), _MAX_LIST_INLINE + 1)
        return f"{shown} (+{extra} more)" if extra else shown
    return humanize_value(_coerce(v))


# Free-text fields authored by task code (may embed null/unresolved clauses):
# their values get the same summary sanitization as the one-line step summary.
_FREETEXT_KEYS = frozenset({
    "summary", "message", "rationale", "escalation_reason", "reason",
    "note", "notes", "description", "decision",
})


def to_fields(data: Any) -> list[dict]:
    """Coerced value -> [{label, value}], dropping internal `_`-prefixed keys and
    orchestration plumbing (`_PLUMBING_KEYS`). Used for both inputs and outputs."""
    data = _coerce(data)
    if not isinstance(data, dict):
        return [{"label": "Value", "value": humanize_value(data)}]
    fields: list[dict] = []
    for k, v in data.items():
        if str(k).startswith(_INTERNAL_PREFIX) or k in _PLUMBING_KEYS:
            continue
        # Drop a bare-id row once its resolved sibling is present (see _ENRICHED_SUPERSEDES).
        sib = _ENRICHED_SUPERSEDES.get(k)
        if sib is not None and sib in data:
            continue
        value = humanize_value(v)
        if k in _FREETEXT_KEYS and isinstance(v, str):
            value = _sanitize_summary(value) or _NA
        fields.append({"label": humanize_label(k), "value": value})
        if len(fields) >= _MAX_FIELDS:
            break
    return fields


def _input_fields(raw_input: Any, session_id: str | None = None) -> list[dict]:
    """Build input fields, unwrapping the run_activity `{"args": [...]}` envelope so
    a single dataclass arg's own fields become the inputs (not one opaque 'Args').

    The session id is orchestration plumbing, not an input: activities like
    `ta_get_available_ambulances` receive it as a bare positional arg. It is shown
    once at the top of the trace, so we drop it from the args here (dataclass args
    already have it stripped by `to_fields` via `_PLUMBING_KEYS`)."""
    data = _coerce(raw_input)
    if isinstance(data, dict) and set(data.keys()) == {"args"} and isinstance(data["args"], list):
        args = data["args"]
        if session_id:
            args = [a for a in args if not (isinstance(a, str) and a == session_id)]
        if not args:
            return []
        if len(args) == 1 and isinstance(args[0], dict):
            return to_fields(args[0])
        if len(args) == 1:
            return [{"label": "Input", "value": humanize_value(args[0])}]
        return [{"label": f"Arg {i + 1}", "value": humanize_value(a)} for i, a in enumerate(args[:_MAX_FIELDS])]
    return to_fields(data)


def _summarize(status: str, output: Any, error: str | None) -> str:
    """One-line plain-language gloss describing what the step actually produced.

    Composes up to a few segments from the result dict: the primary collection with
    a short item preview ("65 triage results (Ravi K., Anita S. …)") and notable
    metric fields ("5 critical", "12 available"), falling back to a message field.
    """
    word = _STATUS_WORD.get(status, status.capitalize() if status else "Step")
    if status == "failed" and error:
        err = error.strip()
        return f"Failed — {err if len(err) <= 120 else err[:120].rstrip() + '…'}"

    out = _coerce(output)
    parts: list[str] = []
    if isinstance(out, dict):
        # Same supersede rule as to_fields: prefer the resolved sibling over the bare id.
        out = {k: v for k, v in out.items()
               if not (k in _ENRICHED_SUPERSEDES and _ENRICHED_SUPERSEDES[k] in out)}
        primary_key = ""
        # Primary collection: first non-empty list, with a 1-2 item preview.
        for k, v in out.items():
            if str(k).startswith(_INTERNAL_PREFIX):
                continue
            if isinstance(v, list) and v:
                primary_key = k
                seg = f"{len(v)} {humanize_label(k).lower()}"
                preview = [lbl for lbl in (_item_label(i) for i in v[:2]) if lbl]
                if preview:
                    tail = " …" if len(v) > len(preview) else ""
                    seg += f" ({', '.join(preview)}{tail})"
                parts.append(seg)
                break
        # Notable scalar metrics: counts/flags whose key reads as meaningful.
        for k, v in out.items():
            if str(k).startswith(_INTERNAL_PREFIX) or k == primary_key:
                continue
            kl = str(k).lower()
            if not any(hint in kl for hint in _METRIC_HINTS):
                continue
            if isinstance(v, bool):
                if v:
                    parts.append(humanize_label(k).lower())
            elif isinstance(v, int):
                parts.append(f"{v} {humanize_label(k).lower()}")
            if len(parts) >= 3:
                break
        # Fallback: a human-authored message/summary/decision string.
        if not parts:
            for key in ("message", "summary", "mode", "decision", "reason"):
                if isinstance(out.get(key), str) and out[key].strip():
                    # Drop null/unresolved clauses so the summary only states what
                    # was actually resolved (no "to unspecified", "ETA: NULL mins").
                    msg = _sanitize_summary(out[key].strip())
                    if not msg:
                        continue
                    parts.append(msg if len(msg) <= 80 else msg[:80].rstrip() + "…")
                    break
    note = ", ".join(parts)
    return f"{word} — {note}" if note else word


# -- Public entry point --------------------------------------------------------

async def record_step(
    session_id: str,
    *,
    kind: str,                 # "agent" | "task" | "decision"
    title: str,
    status: str,               # "running" | "completed" | "failed" | "skipped"
    agent_id: str | None = None,
    task_id: str | None = None,
    raw_input: Any = None,
    raw_output: Any = None,
    error: str | None = None,
    seq: int | None = None,
) -> int | None:
    """Build a humanized step, persist it, and broadcast it live. Never raises.

    Returns the step's `seq` (or None if it could not be recorded). Pass that seq
    back on a later call to emit the SAME step at a new lifecycle stage: the UI
    upserts by seq, so a `running` step reusing its seq is replaced in place by
    its `completed`/`failed` record rather than leaving a stale running card. When
    `seq` is None a fresh sequence number is allocated.
    """
    if not session_id:
        return None
    try:
        from cache import redis as cache
        from api.routes.ws import broadcast

        if seq is None:
            seq = await cache.next_trace_seq(session_id)

        # Resolve bare IDs (patient token / bed / ambulance / approval) into readable
        # detail BEFORE humanizing, so the humanized step -- persisted to Redis AND
        # broadcast over the WebSocket below -- shows names/ward/crew, not opaque ids.
        # Fully defensive: enrich_* never raise, and this block degrades to raw on any
        # import/lookup failure so tracing is never broken.
        enriched_output, enriched_input = raw_output, raw_input
        try:
            from workflows.graph.trace_enrich import enrich_output, enrich_input
            from workflows.graph.exec_context import get_exec_ctx
            ectx = get_exec_ctx() or {}
            if raw_output is not None:
                enriched_output = await enrich_output(raw_output, ectx)
            if raw_input is not None:
                enriched_input = await enrich_input(raw_input, ectx)
        except Exception:  # noqa: BLE001
            enriched_output, enriched_input = raw_output, raw_input

        step = {
            "seq": seq,
            "ts": time.time(),
            "kind": kind,
            "agent_id": agent_id,
            "task_id": task_id,
            "title": title,
            "status": status,
            "summary": _summarize(status, enriched_output, error),
            "inputs": _input_fields(enriched_input, session_id) if enriched_input is not None else [],
            "outputs": to_fields(enriched_output) if enriched_output is not None else [],
            "error": error,
        }
        await cache.append_trace_step(session_id, step)
        await broadcast(session_id, {"type": "trace_step", **step})
        # Current-step tracking for the Execution queue (Phase 2). The latest step
        # is the flow's "current step"; under concurrent agents last-writer-wins,
        # which is the intended single-line summary. Best-effort -- a failure here
        # must not undo the trace we already persisted + broadcast.
        try:
            await cache.set_current_step(session_id, {
                "seq": step["seq"], "ts": step["ts"], "kind": kind,
                "agent_id": agent_id, "task_id": task_id,
                "title": title, "status": status, "summary": step["summary"],
            })
        except Exception:  # noqa: BLE001
            logger.warning("current_step update failed  session=%s", session_id, exc_info=True)
        return seq
    except Exception:  # noqa: BLE001 -- tracing must never break the run
        logger.warning("record_step failed -- continuing untraced  session=%s", session_id, exc_info=True)
        return None
