"""Hospital departments — organisational reference data.

Read from the DB's FHIR Organization resources. Not streamed: departments change
rarely, so there's no Kafka topic for them (see ingest/topic_map.py) and callers
read them live.
"""

from fastapi import APIRouter

from service import clinical

router = APIRouter()


@router.get("/departments", summary="All hospital departments")
async def departments():
    return await clinical.departments()
