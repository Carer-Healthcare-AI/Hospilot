"""Runtime API — what hospilot's agents call while they work.

Fabric serves HTTP from three places, distinguished by caller and cadence:

  runtime/       this package — hospilot's agents, continuously
  writeback/http/  the HIS, on its own schedule, to collect writes we've queued
  initial_sync/  hospilot-backend, once, to seed its cache from scratch

Everything here answers "what is true right now, for this filter" — the questions
the backend's internal DB can't serve because they need lists, joins or aggregates
rather than a single record. One module per domain.

Writes ENTER here too (the POST/PATCH routes) but don't leave from here: they queue a
proposal and exit via writeback/. See writeback/__init__.py for the full pipeline.

Aggregated below into a single `router`, which main.py mounts once. Domain prefixes
don't overlap, so include order is not significant across modules — but WITHIN each
module static sub-paths must stay declared before their `/{id}` sibling or FastAPI
will shadow them.

⚠ patients.py is the only PHI-bearing module; the rest is pseudonymous.
"""

from fastapi import APIRouter

from runtime import (
    admissions,
    appointments,
    beds,
    departments,
    financial,
    labs,
    ot,
    patients,
    pharmacy,
    nursing_tasks,
    visits,
    vitals,
)

router = APIRouter()

for _module in (
    beds,
    admissions,
    vitals,
    visits,
    nursing_tasks,
    labs,
    pharmacy,
    financial,
    patients,
    departments,
    ot,
    appointments,
):
    router.include_router(_module.router)
