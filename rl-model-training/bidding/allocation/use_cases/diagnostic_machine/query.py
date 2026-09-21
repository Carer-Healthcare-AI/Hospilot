"""Safe natural-language routing for the deterministic diagnostic scenarios.

This resolver accepts arbitrary text, but it never invents clinical values.  It can route a
query only when the text supplies enough intent to select one governed simulation template.
An optional future LLM may return the same typed draft; it must not open an auction directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class DiagnosticQueryResolution:
    query: str
    scenario: str | None
    confidence: float
    evidence: tuple[str, ...]
    missing: tuple[str, ...] = ()

    @property
    def runnable(self) -> bool:
        return self.scenario is not None and not self.missing


_INTENTS: Mapping[str, Mapping[str, float]] = {
    "three_way_contention": {
        "ct": 2, "er": 1, "icu": 1, "ot": 1, "compete": 2, "contention": 2,
    },
    "alternative_answers_the_question": {
        "alternative": 3, "ultrasound": 3, "substitute": 2, "gallbladder": 2,
    },
    "deadline_pressure": {
        "deadline": 3, "urgent": 2, "expire": 2, "six": 1, "ct": 1,
    },
    "mri_scarcity": {"mri": 4, "scarce": 2, "scarcity": 2, "spine": 1, "brain": 1},
    "capability_is_a_hard_filter": {
        "capability": 3, "angiography": 4, "contrast": 2, "ctpa": 4,
    },
    "portable_costs_no_transport": {
        "portable": 4, "xray": 3, "x-ray": 3, "transport": 2,
    },
    "degraded_fleet": {
        "downtime": 3, "degraded": 3, "maintenance": 3, "out": 1, "service": 1,
    },
    "operational_pressure": {
        "theatre": 4, "operational": 3, "unblock": 3, "workflow": 2,
    },
}


def resolve(query: str) -> DiagnosticQueryResolution:
    text = query.strip().lower()
    if not text:
        return DiagnosticQueryResolution(query, None, 0.0, (), ("diagnostic query",))
    tokens = set(re.findall(r"[a-z0-9-]+", text))
    scored: list[tuple[float, str, tuple[str, ...]]] = []
    for scenario, vocabulary in _INTENTS.items():
        hits = tuple(sorted(term for term in vocabulary if term in tokens or term in text))
        scored.append((sum(vocabulary[term] for term in hits), scenario, hits))
    scored.sort(reverse=True)
    best_score, best, evidence = scored[0]
    second_score = scored[1][0]
    if best_score < 3:
        return DiagnosticQueryResolution(
            query, None, 0.0, evidence,
            ("a resolvable diagnostic scenario or structured request values",),
        )
    if best_score == second_score:
        tied = tuple(item[1] for item in scored if item[0] == best_score)
        return DiagnosticQueryResolution(
            query, None, 0.5, evidence, (f"unambiguous intent; tied: {', '.join(tied)}",)
        )
    confidence = min(1.0, (best_score - second_score + best_score) / 10.0)
    return DiagnosticQueryResolution(query, best, confidence, evidence)


__all__ = ["DiagnosticQueryResolution", "resolve"]
