"""Nursing tasks — outstanding, overdue, and per-admission counts.

`task` is a streamed entity, so the backend caches individual tasks. These routes
serve the filtered and counted views.
"""

from fastapi import APIRouter, Query

from service import clinical

router = APIRouter()


@router.get("/tasks/incomplete", summary="All nursing tasks not yet completed")
async def tasks_incomplete():
    return await clinical.incomplete_tasks()


@router.get("/tasks/overdue", summary="Nursing tasks past their due time")
async def tasks_overdue():
    return await clinical.overdue_tasks()


@router.get("/tasks/completed-count", summary="Count of completed tasks for one admission")
async def tasks_completed_count(admission: str = Query(...)):
    return {"admission_id": admission, "count": await clinical.completed_task_count(admission)}


@router.get("/tasks", summary="Tasks for one admission, or all incomplete tasks when admission is omitted")
async def tasks(admission: str | None = Query(None)):
    if admission:
        return await clinical.nursing_tasks_for(admission)
    return await clinical.incomplete_tasks()
