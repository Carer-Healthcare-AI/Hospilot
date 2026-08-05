import logging
import uuid

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from api.routes.auth import require_active_user, require_role
from db.hasura import hasura
from workflows.dynamic_task import validate_new_task
from workflows.task_codegen import generate_task_code
from workflows.task_writer import restart_worker

logger = logging.getLogger("registry")
# The task registry is control-plane/GLOBAL: a mutation here changes the
# catalog (and restarts the worker) for EVERY tenant. Reads/validation need any
# active user; mutations are super_admin-only.
router = APIRouter(dependencies=[Depends(require_active_user)])


class AddTaskRequest(BaseModel):
    agent_id: str
    agent_label: str
    subagent_id: str
    label: str
    description: str = ""
    outputs: list[str] = []


class AddSubAgentRequest(BaseModel):
    agent_id: str
    label: str
    description: str = ""
    capabilities: list[str] = []
    is_prefetch_eligible: bool = False


@router.post("/registry/tasks/validate")
async def validate_task_endpoint(body: AddTaskRequest):
    """
    Dry-run validation: checks if a proposed task is compatible with the
    agent's data sources without writing to the DB.
    Returns 200 if valid, 400 with reason if not.
    """
    result = await validate_new_task(
        agent_id=body.agent_id,
        agent_label=body.agent_label,
        label=body.label,
        description=body.description,
        outputs=body.outputs,
    )
    if not result.get("valid", True):
        raise HTTPException(status_code=400, detail=result)
    return result


@router.post("/registry/tasks", status_code=201,
             dependencies=[Depends(require_role("super_admin"))])
async def add_task(body: AddTaskRequest):
    """
    Add a dynamic task to the registry (super_admin -- the catalog is global).
    Runs creation-time guardrail first -- rejects if task needs unavailable data.
    On success writes to hospilot.task_registry with is_dynamic=true.
    """
    # Checkpoint 1: creation-time guardrail
    validation = await validate_new_task(
        agent_id=body.agent_id,
        agent_label=body.agent_label,
        label=body.label,
        description=body.description,
        outputs=body.outputs,
    )
    if not validation.get("valid", True):
        logger.warning(
            "task rejected by guardrail  agent=%s  label=%s  reason=%s",
            body.agent_id, body.label, validation.get("reason"),
        )
        raise HTTPException(status_code=400, detail=validation)

    task_id = f"ta_gen_{uuid.uuid4().hex[:8]}"

    print(f"\n{'='*60}")
    print(f"  NEW TASK REQUEST")
    print(f"  label    : {body.label}")
    print(f"  agent    : {body.agent_id}  /  subagent: {body.subagent_id}")
    print(f"  outputs  : {body.outputs or '(not specified)'}")
    print(f"  task_id  : {task_id}")
    print(f"{'='*60}")

    # Generate real Python activity code
    try:
        code = await generate_task_code(
            task_id=task_id,
            label=body.label,
            description=body.description,
            outputs=body.outputs,
            agent_id=body.agent_id,
        )
    except Exception as exc:
        print(f"  REGISTRY [x] codegen failed: {exc}")
        print(f"{'='*60}\n")
        logger.error("codegen failed  task=%s  err=%s", task_id, exc)
        raise HTTPException(status_code=500, detail=f"Code generation failed: {exc}")

    # Save to DB -- function_code stored alongside task metadata
    task = await hasura.insert_task_to_registry(
        task_id=task_id,
        subagent_id=body.subagent_id,
        label=body.label,
        description=body.description,
        outputs=body.outputs,
        function_code=code,
    )
    logger.info("[ok] task registered  id=%s  subagent=%s  label=%s", task_id, body.subagent_id, body.label)

    # Signal worker to restart -- it will reload all functions from DB on startup
    restarted = restart_worker()
    print(f"  REGISTRY [ok] task saved to DB  id={task_id}  worker_restarted={restarted}")
    print(f"{'='*60}\n")
    return {**task, "generated_code": code}


@router.delete("/registry/tasks/{task_id}",
               dependencies=[Depends(require_role("super_admin"))])
async def delete_task_endpoint(task_id: str):
    """
    Remove a task from the registry (soft delete -- sets is_active=false).
    Works for both dynamically-added and built-in catalog tasks; the task stops
    appearing in the agent registry / planner immediately. super_admin-only:
    the catalog is global across tenants.
    """
    deleted = await hasura.deactivate_task_in_registry(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")

    restarted = restart_worker()
    logger.info("[ok] task deactivated  id=%s  worker_restarted=%s", task_id, restarted)
    return {"id": task_id, "deleted": True}


@router.post("/registry/subagents", status_code=201)
async def add_subagent(body: AddSubAgentRequest):
    """
    Add a sub-agent to an existing agent in the registry.
    Sub-agents are pure grouping metadata (no generated code), so unlike tasks
    this needs no codegen and no worker restart -- the planner reads the registry
    live per session. Returns the created sub-agent in registry (tree) shape.
    """
    subagent_id = f"sa_gen_{uuid.uuid4().hex[:8]}"
    sa = await hasura.insert_subagent_to_registry(
        subagent_id=subagent_id,
        agent_id=body.agent_id,
        label=body.label,
        description=body.description,
        capabilities=body.capabilities,
        is_prefetch_eligible=body.is_prefetch_eligible,
    )
    if not sa:
        raise HTTPException(status_code=500, detail="Failed to create sub-agent")
    logger.info("[ok] subagent registered  id=%s  agent=%s  label=%s", subagent_id, body.agent_id, body.label)
    # Shape matches RegistrySubAgent (fetch_agent_registry) so the UI can drop it
    # straight into the tree; a fresh sub-agent has no tasks yet.
    return {
        "id": sa.get("id", subagent_id),
        "label": sa.get("label", body.label),
        "description": sa.get("description", body.description),
        "capabilities": sa.get("capabilities", body.capabilities) or [],
        "is_prefetch_eligible": sa.get("is_prefetch_eligible", body.is_prefetch_eligible),
        "tasks": [],
    }


@router.delete("/registry/subagents/{subagent_id}")
async def delete_subagent_endpoint(subagent_id: str):
    """
    Remove a sub-agent (soft delete -- is_active=false) and cascade-deactivate
    its tasks so none are left orphaned. It stops appearing in the registry /
    planner immediately. Restart the worker because child tasks (which carry
    generated code) were deactivated.
    """
    deleted = await hasura.deactivate_subagent_in_registry(subagent_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Sub-agent '{subagent_id}' not found")

    restarted = restart_worker()
    logger.info("[ok] subagent deactivated  id=%s  worker_restarted=%s", subagent_id, restarted)
    return {"id": subagent_id, "deleted": True}
