"""Declarative advisory conditions -- the engine reads each rule's logic from its
DB `definition` JSON (advisory_rules.definition) instead of a hardcoded function.

A rule's `definition.condition` is one of:

  * declarative -- {source, args, kind, filter, aggregate, operator, threshold,
    detail_template, labels}: a generic spec the `evaluate()` interpreter runs
    against a live data source. Covers the count/field/pct/ratio/max threshold
    rules.
  * handler     -- {"handler": "<rule_key>"}: dispatches to a named Python
    function in EVALUATORS (workflows/graph/advisory_evaluators.py) for the
    complex rules (stateful clocks, multi-source joins, ML forecasts, financial
    sums, composite/exec) that cannot be expressed as data. Behaviour is byte
    identical to the pre-declarative engine because it IS the same function.

`run_condition()` dispatches between the two. `check_rule()` in advisory.py calls
it, falling back to EVALUATORS[rule_key] when a rule has no definition yet.

Contract of both paths matches the evaluators': async (org_id, params) ->
(fired: bool, detail: str, data: dict). `detail` is only surfaced when fired, so
edge-case wording on non-firing paths is immaterial.
"""

import logging
from datetime import datetime, timezone

from cache import redis as cache
from db.hasura import hasura

logger = logging.getLogger(__name__)


# ── shared coercions ──────────────────────────────────────────────────────────

def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _age_minutes(value) -> float | None:
    """Minutes since an ISO timestamp, or None if unparseable/absent."""
    if not value or str(value).upper() in ("NULL", "NONE"):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60


# ── data sources (the one place declarative rules can read from) ──────────────
# name -> async callable(**args). Cache-backed sources may raise when Redis is
# down; _load_source turns any failure into empty data (-> rule does not fire).

SOURCES = {
    "beds_summary":          lambda **k: hasura.get_beds_summary(),
    "er_pressure":           lambda **k: hasura.get_er_pressure(),
    "dirty_beds":            lambda **k: hasura.get_dirty_beds(),
    "enriched_beds":         lambda **k: hasura.get_enriched_beds(),
    "icu_admissions":        lambda **k: hasura.get_icu_admissions(),
    "er_long_wait":          lambda minutes=120, **k: hasura.get_long_wait_er_visits(minutes=minutes),
    "untriaged":             lambda **k: hasura.get_untriaged_visits(),
    "critical_vitals":       lambda **k: hasura.get_critical_vitals(),
    "active_er":             lambda **k: hasura.get_active_er_visits(),
    "ambulances":            lambda **k: cache.get_all_ambulances(),
    "ventilators":           lambda **k: cache.get_all_ventilators(),
    "staff":                 lambda **k: cache.get_all_staff(),
    "staff_roster":          lambda **k: cache.get_all_staff_roster(),
    "pharmacy_inventory":    lambda **k: hasura.pharmacy_get_inventory(),
    "pharmacy_orders":       lambda **k: hasura.pharmacy_get_orders(),
    "pharmacy_controlled_logs": lambda hours=24, **k: hasura.pharmacy_get_controlled_logs(hours=hours),
    "admissions_with_wards": lambda **k: hasura.get_admissions_with_wards(),
}


async def _load_source(name: str, args: dict):
    fn = SOURCES.get(name)
    if fn is None:
        raise KeyError(f"unknown advisory source: {name}")
    try:
        return await fn(**(args or {}))
    except Exception:  # noqa: BLE001 -- down source => empty => not fired
        logger.debug("advisory source %s unavailable", name, exc_info=True)
        return None


# ── predicate matching ────────────────────────────────────────────────────────

def _cmp(a, op: str, b) -> bool:
    if op in (">", ">=", "<", "<="):
        a, b = _num(a), _num(b)
    if op == "==":
        return str(a).lower() == str(b).lower()
    if op == "!=":
        return str(a).lower() != str(b).lower()
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    return False


def _match(row: dict, pred: dict) -> bool:
    if "any" in pred:  # OR group
        return any(_match(row, p) for p in pred["any"])
    field = pred["field"]
    op = pred["op"]
    val = pred.get("value")
    cur = row.get(field)
    if op == "is_null":
        return cur is None or str(cur).upper() in ("", "NULL", "NONE")
    if op == "not_null":
        return cur is not None and str(cur).upper() not in ("", "NULL", "NONE")
    if op == "truthy":
        return cur in (True, "t", "true", "T", "True", 1, "1") or bool(cur) and str(cur).upper() not in ("F", "FALSE", "0", "NULL")
    if op == "falsy":
        return not _match(row, {"field": field, "op": "truthy"})
    if op in ("in", "not_in"):
        opts = {str(v).lower() for v in (val or [])}
        hit = str(cur or "").lower() in opts
        return hit if op == "in" else not hit
    if op == "contains_any":
        s = str(cur or "").lower()
        return bool(s) and any(str(k).lower() in s for k in (val or []))
    if op == "age_gt_minutes":
        age = _age_minutes(cur)
        return age is not None and age > _num(val)
    if op in ("le_field", "ge_field", "lt_field", "gt_field"):
        other = _num(row.get(val))
        return _cmp(_num(cur), op.split("_")[0].replace("le", "<=").replace("ge", ">=")
                    .replace("lt", "<").replace("gt", ">"), other)
    return _cmp(cur, op, val)


def _filter(rows: list, filters: list) -> list:
    return [r for r in rows if isinstance(r, dict) and all(_match(r, f) for f in filters)]


# ── the declarative interpreter ───────────────────────────────────────────────

async def evaluate(cond: dict, org_id: str | None) -> tuple[bool, str, dict]:
    """Run a declarative condition spec and return (fired, detail, data)."""
    data = await _load_source(cond["source"], cond.get("args"))
    kind = cond.get("kind", "list")
    op, threshold = cond["operator"], _num(cond["threshold"])
    agg = cond.get("aggregate")  # dict-source rules use `field` instead
    ctx: dict = dict(cond.get("labels", {}))
    ctx["threshold"] = threshold

    if kind == "dict":
        d = data if isinstance(data, dict) else {}
        present = bool(d) and all(_num(d.get(f)) > 0 for f in cond.get("require_positive", []))
        value = _num(d.get(cond["field"]))
        for k, v in d.items():
            ctx.setdefault(k, v)
        ctx["value"] = value
        fired = present and _cmp(value, op, threshold)
    else:
        rows = data if isinstance(data, list) else []
        total = len(rows)
        matched = _filter(rows, cond.get("filter", []))
        count = len(matched)
        if agg == "count":
            value = count
        elif agg == "pct":
            value = (count / total * 100) if total else 0.0
        elif agg == "pct_of":
            denom = _num(cond.get("denominator")) or _num((cond.get("args") or {}).get("denominator"))
            value = (count / denom * 100) if denom else 0.0
        elif agg == "max_field":
            value = max((_num(r.get(cond["field"])) for r in matched), default=0.0)
        elif agg == "ratio":
            num = len(_filter(rows, cond["numerator_filter"]))
            den = len(_filter(rows, cond["denominator_filter"]))
            value = (num / den * 100) if den else 0.0
            ctx["numerator"], ctx["denominator"] = num, den
        else:
            raise ValueError(f"unknown aggregate: {agg}")
        require_ok = (not cond.get("require_source_nonempty")) or bool(rows)
        ctx.update({"value": value, "count": count, "total": total})
        fired = require_ok and _cmp(value, op, threshold)

    try:
        detail = cond.get("detail_template", "{value} (threshold {threshold})").format(**ctx)
    except Exception:  # noqa: BLE001 -- never crash on a template typo (detail only used when fired)
        detail = f"{ctx.get('value')} (threshold {threshold})"
        logger.warning("advisory detail_template failed for source=%s", cond.get("source"))

    evidence = {k: ctx[k] for k in ("value", "count", "total", "threshold", "numerator", "denominator")
                if k in ctx}
    return fired, detail, evidence


# ── dispatch ──────────────────────────────────────────────────────────────────

async def run_condition(cond: dict, org_id: str | None) -> tuple[bool, str, dict]:
    """Route a rule's condition: named handler (existing evaluator) or declarative.
    Handler thresholds live in the definition (cond.params) -- the single source of
    rule config now that the advisory_rules.params column is gone (migration 071)."""
    handler = cond.get("handler")
    if handler:
        from workflows.graph.advisory_evaluators import EVALUATORS
        fn = EVALUATORS.get(handler)
        if fn is None:
            raise KeyError(f"advisory handler not found: {handler}")
        return await fn(org_id, cond.get("params") or {})
    return await evaluate(cond, org_id)
