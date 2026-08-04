"""ER visits and triage, plus the derived ER pressure metric.

`visit` is a streamed entity; these routes cover the ER-specific views and the
triage proposals. /er/pressure is a computed aggregate, cached nowhere.

Static sub-paths are declared before `/{visit_id}` so they aren't shadowed.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from service import clinical
from writeback import proposals

router = APIRouter()


@router.get("/visits/er", summary="Current ER visits")
async def visits_er():
    return await clinical.er_visits()


@router.get("/visits/untriaged", summary="ER visits with no triage score yet")
async def visits_untriaged():
    return [v for v in await clinical.er_visits() if v.get("triage_score") is None]


@router.get("/er/pressure", summary="Derived ER load metric (volume vs capacity)")
async def er_pressure():
    return await clinical.er_pressure()


class BulkTriage(BaseModel):
    items: list[dict]


@router.post("/visits/triage/bulk", summary="Set triage scores for many visits (queued as pending changes)")
async def visits_triage_bulk(body: BulkTriage):
    await proposals.bulk_set_triage_scores(body.items)
    return {"ok": True, "count": len(body.items)}


class TriageScore(BaseModel):
    score: int


@router.post("/visits/{visit_id}/triage", summary="Set one visit's triage score (queued as a pending change)")
async def set_triage(visit_id: str, body: TriageScore):
    await proposals.set_triage_score(visit_id, body.score)
    return {"ok": True, "id": visit_id, "score": body.score}
