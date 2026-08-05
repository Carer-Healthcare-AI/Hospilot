from dataclasses import dataclass, field
from typing import Any


@dataclass
class TypedCondition:
    symbol: str  # "ta_query_beds.candidate_count" -- resolves to ta_results[task_id][field]
    op: str      # ">", "<", ">=", "<=", "==", "!="
    value: Any   # threshold; cast to type(value) at eval time

    def evaluate(self, ta_results: dict) -> bool:
        task_id, _, field_name = self.symbol.partition(".")
        if not field_name:
            return True
        data = ta_results.get(task_id)
        if data is None:
            return False
        actual = data.get(field_name)

        # None comparisons -- never try to cast, compare identity directly
        if self.value is None:
            return {"==": actual is None, "!=": actual is not None}.get(self.op, True)

        if actual is None:
            return True  # field not found in prior task's output -- unresolvable condition, default to run

        # A collection compared against a number means "how many?" -- e.g.
        # "ta_get_er_visits.visits > 0" tests non-emptiness. Compare its length,
        # not the container itself (int([...]) would raise and wrongly SKIP).
        # str is intentionally excluded (status == "completed" stays a value compare).
        if isinstance(actual, (list, tuple, set, dict)) and isinstance(self.value, (int, float)) and not isinstance(self.value, bool):
            actual = len(actual)

        try:
            actual = type(self.value)(actual)
        except (TypeError, ValueError):
            return False
        return {
            ">":  actual > self.value,
            "<":  actual < self.value,
            ">=": actual >= self.value,
            "<=": actual <= self.value,
            "==": actual == self.value,
            "!=": actual != self.value,
        }.get(self.op, True)


@dataclass
class Task:
    id: str
    label: str
    outputs: list[str] = field(default_factory=list)

    def schema(self) -> dict:
        return {"id": self.id, "label": self.label, "outputs": self.outputs}


@dataclass
class SubAgent:
    id: str
    label: str
    tasks: list[Task] = field(default_factory=list)
    description: str = ""

    def schema(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "tasks": [t.schema() for t in self.tasks],
        }
