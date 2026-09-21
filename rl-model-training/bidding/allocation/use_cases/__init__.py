"""Isolated resource-allocation use cases built on shared mechanical infrastructure."""

from allocation.use_cases.registry import USE_CASES, UseCase, get_use_case

__all__ = ["USE_CASES", "UseCase", "get_use_case"]
