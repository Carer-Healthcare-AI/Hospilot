"""The entry point. Query or event in, a scored and logged auction out.

Every layer below this one is independently tested; this is the only place they are wired
into a sequence. Two things live here and nowhere else:

**Use-case resolution** (``query.py``) — the sentence at the top of RL-Steps selects a
resource profile. That is the *use case*, not the runtime trigger.

**The end-to-end sequence** (``runtime.py``) — ingest, score, ceiling, budget, auction,
audit, pending observation, in that order, emitting a :class:`~allocation.trigger.steps.Step`
per stage so the run can be checked against the framework section by section.
"""

from __future__ import annotations

from allocation.trigger.query import UnknownUseCase, manual_event, resolve_profile
from allocation.trigger.runtime import AllocationRun, run_allocation
from allocation.trigger.steps import Step, StepTrace

__all__ = [
    "AllocationRun",
    "Step",
    "StepTrace",
    "UnknownUseCase",
    "manual_event",
    "resolve_profile",
    "run_allocation",
]
