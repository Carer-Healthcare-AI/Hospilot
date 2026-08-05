"""Materialize an approved pipeline into the runtime subagent_preplan format.

Turns ``pipeline.agents[].sub_agents[].tasks`` into the per-instance Redis preplan
dict that ``graph.nodes`` seeds as ``ctx["_task_plan"]``, so execution runs EXACTLY
the approved tasks: ``graph.planning.plan_subagent`` no-ops on a filled slot and
``should_run_task`` runs only the listed tasks, gated by their typed conditions.

Condition source per task (in precedence order):
  1. a condition already on the task entry -- the 3-stage planner's stage-3 output
     is preplan-shaped and carries one (``plan_subagent_tasks`` return).
  2. the typed ``condition: ta_x.field <op> <value>`` hint parsed from the catalog
     label -- fallback for an edited/externally-supplied pipeline whose tasks are
     just ``{id, label}``.
  3. ``None`` -- the planner's selection itself is the gate.

CRITICAL: omitted catalog sub-agents are written as ``{"__planned__": True}``, NOT
left absent. The agent bodies do ``task_plan.setdefault(sa_id, {})`` for every
catalog sub-agent, and ``{}`` means "run all" -- leaving them absent would silently
defeat the binding (Risk R1).
"""

import re

from workflows.planner import SUB_AGENTS


def _agent_base_id(agent_id: str) -> str:
    return agent_id.split(":")[0]


_COND_RE = re.compile(
    r"condition:\s*(?P<sym>ta_[a-z0-9_]+\.[a-z0-9_]+)\s*"
    r"(?P<op>==|!=|>=|<=|>|<)\s*"
    r"(?P<val>true|false|null|-?\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _coerce(raw: str):
    low = raw.lower()
    if low == "null":
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(raw)
    except ValueError:
        try:
            return float(raw)
        except ValueError:
            return raw


def _parse_label_condition(label: str) -> dict | None:
    m = _COND_RE.search(label or "")
    if not m:
        return None
    return {"symbol": m.group("sym"), "op": m.group("op"), "value": _coerce(m.group("val"))}


# Built once at import from the static catalog (matches how the agent bodies build
# their _X_TASKS from SUB_AGENTS).
CATALOG_TASK_CONDITIONS: dict[str, dict | None] = {}
CATALOG_TASK_OUTPUTS: dict[str, list] = {}
CATALOG_SUBAGENTS: dict[str, list[str]] = {}  # base agent id -> [subagent ids]
for _base, _sas in SUB_AGENTS.items():
    CATALOG_SUBAGENTS[_base] = [sa.id for sa in _sas]
    for _sa in _sas:
        for _t in _sa.tasks:
            CATALOG_TASK_CONDITIONS[_t.id] = _parse_label_condition(_t.label)
            CATALOG_TASK_OUTPUTS[_t.id] = _t.outputs


def _task_id(t) -> str:
    return t if isinstance(t, str) else t.get("id", "")


def _task_condition(t):
    if isinstance(t, dict) and t.get("condition") is not None:
        return t["condition"]            # stage-3 / edited pipeline carried it
    return CATALOG_TASK_CONDITIONS.get(_task_id(t))


def _task_label(t) -> str:
    tid = _task_id(t)
    if isinstance(t, dict) and t.get("label"):
        return t["label"]
    return tid


def materialize_preplans(pipeline: dict) -> dict[str, dict]:
    """``pipeline`` -> ``{node_id: subagent_preplan}`` for agents present in SUB_AGENTS.

    DB-registry agents (not in SUB_AGENTS) are skipped -- their bodies fall back to
    DB-driven planning.
    """
    out: dict[str, dict] = {}
    for agent in pipeline.get("agents", []):
        node_id = agent["id"]
        base = _agent_base_id(node_id)
        catalog_ids = CATALOG_SUBAGENTS.get(base)
        if not catalog_ids:
            continue
        selected = {sa["id"]: sa for sa in agent.get("sub_agents", []) if sa.get("id")}
        preplan: dict = {}
        for sa_id in catalog_ids:
            if sa_id not in selected:
                preplan[sa_id] = {"__planned__": True}
                continue
            slot: dict = {}
            sa_condition = (selected[sa_id].get("condition") or "").strip()
            if sa_condition:
                slot["__condition__"] = sa_condition
            for t in selected[sa_id].get("tasks", []):
                tid = _task_id(t)
                if not tid:
                    continue
                slot[tid] = {
                    "condition": _task_condition(t),
                    "label":     _task_label(t),
                    "outputs":   CATALOG_TASK_OUTPUTS.get(tid, []),
                }
            # Selected sub-agent with NO tasks must skip all (run nothing), not "run all"
            # -- an empty {} slot means "plan pending" to should_run_task. Use the
            # planner-selected-nothing sentinel instead.
            preplan[sa_id] = slot or {"__planned__": True}
        # Preserve the plan's sub-agent order (filtered to the catalog).
        preplan["__subagent_order__"] = [
            sa["id"] for sa in agent.get("sub_agents", [])
            if sa.get("id") in catalog_ids
        ]
        out[node_id] = preplan
    return out
