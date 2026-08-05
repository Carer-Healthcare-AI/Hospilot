import asyncio
import logging
from dataclasses import dataclass, field

from temporalio import activity

from api.routes.ws import broadcast
from workflows.dynamic_task import execute_dynamic_task

logger = logging.getLogger(__name__)

AGENT_STEPS: dict[str, list[tuple[str, str]]] = {
    "er_agent": [
        ("sa_er_triage",   "Queried ER queue -- 4 patients waiting, 1 flagged for re-triage"),
        ("sa_er_boarding", "Boarding check -- no SLA breaches at this time"),
    ],
    "icu_agent": [
        ("sa_icu_census",   "ICU occupancy: 8/10 beds occupied -- 2 available"),
        ("sa_icu_stepdown", "Step-down candidates: 1 patient eligible for PCU transfer"),
    ],
    "staff_agent": [
        ("sa_ratio_monitor", "Nurse-patient ratios within acceptable range on all floors"),
        ("sa_float_pool",    "Float pool: 2 nurses available for redeployment"),
    ],
    "discharge_agent": [
        ("sa_discharge_ready",    "3 patients flagged as discharge-ready today"),
        ("sa_discharge_barriers", "1 patient has transport barrier -- case manager notified"),
    ],
    "pharmacy_agent": [
        ("sa_stock_monitor", "Drug inventory checked -- no critical shortages"),
    ],
}


@dataclass
class GenericAgentInput:
    session_id: str
    agent_id: str
    order: int
    label: str


@activity.defn
async def run_generic_stub(inp: GenericAgentInput) -> dict:
    steps = AGENT_STEPS.get(inp.agent_id, [("sa_generic", f"{inp.agent_id} completed")])

    await broadcast(inp.session_id, {
        "type": "agent_started",
        "agent_id": inp.agent_id,
        "label": inp.label,
    })

    for sub_id, result_msg in steps:
        await broadcast(inp.session_id, {"type": "sub_agent_started", "sub_agent": sub_id})
        await asyncio.sleep(1.5)
        await broadcast(inp.session_id, {
            "type": "sub_agent_completed",
            "sub_agent": sub_id,
            "result": {"summary": result_msg},
        })

    logger.info("[ok] stub complete  agent=%s  session=%s", inp.agent_id, inp.session_id)
    return {"status": "completed", "agent_id": inp.agent_id}


@dataclass
class DynamicTaskInput:
    task_id: str
    task_label: str
    task_outputs: list[str]
    agent_id: str
    session_id: str
    context: dict = field(default_factory=dict)


@activity.defn
async def run_dynamic_task(inp: DynamicTaskInput) -> dict:
    """
    Temporal activity: execute a dynamic (user-added) task via Claude Sonnet.

    Called from any agent workflow when a task_id has no hardcoded handler:

        if task_id not in HARDCODED_TASK_HANDLERS:
            result = await workflow.execute_activity(
                run_dynamic_task,
                DynamicTaskInput(
                    task_id=task_id,
                    task_label=task["label"],
                    task_outputs=task["outputs"],
                    agent_id=agent_id,
                    session_id=session_id,
                    context=ta_results,
                ),
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )

    The activity broadcasts sub_agent_started / sub_agent_completed WS events
    so the frontend shows progress like any other task.
    """
    await broadcast(inp.session_id, {
        "type": "sub_agent_started",
        "sub_agent": inp.task_id,
        "label": inp.task_label,
        "dynamic": True,
    })

    result = await execute_dynamic_task(
        task_id=inp.task_id,
        task_label=inp.task_label,
        task_outputs=inp.task_outputs,
        agent_id=inp.agent_id,
        context=inp.context,
    )

    await broadcast(inp.session_id, {
        "type": "sub_agent_completed",
        "sub_agent": inp.task_id,
        "result": result,
        "dynamic": True,
    })

    logger.info(
        "[ok] dynamic task activity done  task=%s  status=%s  session=%s",
        inp.task_id, result.get("status"), inp.session_id,
    )
    return result
