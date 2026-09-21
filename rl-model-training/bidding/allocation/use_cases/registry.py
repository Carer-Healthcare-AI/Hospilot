"""Public registry of allocation resource families.

The registry is intentionally metadata-only. Importing it must not eagerly import a simulator,
policy, or optional dependency from every use case. New resource families register their module
and command here; their clinical semantics remain inside their own package.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class UseCase:
    """Stable discovery metadata for one allocation use case."""

    name: str
    title: str
    module: str
    description: str


_USE_CASES = {
    "bed": UseCase(
        name="bed",
        title="Bed allocation",
        module="allocation.use_cases.bed",
        description="Auction scarce beds across eligible hospital departments.",
    ),
    "diagnostic_machine": UseCase(
        name="diagnostic_machine",
        title="Diagnostic-machine allocation",
        module="allocation.use_cases.diagnostic_machine",
        description="Allocate CT, MRI, X-ray and ultrasound capacity intervals.",
    ),
}

USE_CASES: Mapping[str, UseCase] = MappingProxyType(_USE_CASES)


def get_use_case(name: str) -> UseCase:
    """Return registered metadata, with a useful error for an unknown family."""

    try:
        return USE_CASES[name]
    except KeyError as exc:
        valid = ", ".join(sorted(USE_CASES))
        raise KeyError(f"unknown allocation use case {name!r}; choose one of: {valid}") from exc


__all__ = ["USE_CASES", "UseCase", "get_use_case"]
