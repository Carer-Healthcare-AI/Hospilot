"""Auto-discover every @activity.defn task and classify it for the live E2E sweep.

Mirrors the worker's discovery (workflows/temporal/worker/run_worker.py): walk the
agents package and keep anything carrying __temporal_activity_definition. Classify by
signature — Tier A tasks take only session_id and can be swept with a dummy id; Tier B
tasks take an `inp` dataclass and need a per-type input fixture (second pass).
"""
import importlib
import inspect
import pkgutil
from dataclasses import dataclass

# Tasks that bypass the Fabric approval gate and mutate live records directly
# (bulk_set_triage_scores / set_ai_discharge_note). Never run these against real data.
FENCED = {
    "save_triage_scores",
    "generate_discharge_summaries",
    "check_medication_reconciliation",
}


@dataclass
class TaskRef:
    id: str          # "agents.lab.sample_tracking:check_sample_collection"
    module: str
    name: str
    fn: object
    tier: str        # "A" | "B" | "other"


def _classify(fn) -> str:
    params = list(inspect.signature(fn).parameters.values())
    required = [p for p in params if p.default is inspect._empty]
    if len(required) == 1 and required[0].name == "session_id":
        return "A"
    if params and params[0].name == "inp":
        return "B"
    return "other"


def collect_tasks() -> list[TaskRef]:
    import agents

    seen: dict[str, TaskRef] = {}
    for info in pkgutil.walk_packages(agents.__path__, prefix="agents."):
        try:
            mod = importlib.import_module(info.name)
        except Exception:
            continue
        for name, obj in vars(mod).items():
            if not hasattr(obj, "__temporal_activity_definition"):
                continue
            if name in seen:  # same activity re-exported from another module
                continue
            seen[name] = TaskRef(
                id=f"{info.name}:{name}",
                module=info.name,
                name=name,
                fn=obj,
                tier=_classify(obj),
            )
    return sorted(seen.values(), key=lambda t: t.id)
