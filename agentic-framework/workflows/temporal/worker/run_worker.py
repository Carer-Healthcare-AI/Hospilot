"""Temporal worker process (separate container).

Auto-discovers every @activity.defn function under temporal.activities, registers
them plus the generic RunActivityWorkflow, and polls the task queue. The activity
bodies are unchanged -- they run here instead of in-process.

Run with:  python -m temporal.worker.run_worker
"""
import asyncio
import importlib
import inspect
import logging
import pkgutil

from temporalio.activity import _Definition
from temporalio.client import Client
from temporalio.worker import Worker

from config import settings
from logging_config import setup_logging

setup_logging()
logger = logging.getLogger("temporal.worker")


def _collect_activities() -> list:
    """Find all @activity.defn-decorated functions across agents/ and workflows/.

    Walks agents/ recursively (all domain packages) plus the workflow-level
    approval_escalation_activities module, deduped by registered activity name.
    """
    import agents
    extra_modules = ["workflows.approval_escalation_activities"]

    by_name: dict[str, object] = {}
    modules_to_scan = list(pkgutil.walk_packages(agents.__path__, prefix="agents."))

    for info in modules_to_scan:
        try:
            mod = importlib.import_module(info.name)
        except Exception:  # noqa: BLE001
            logger.warning("skipping module %s (import error)", info.name, exc_info=True)
            continue
        for _, obj in inspect.getmembers(mod, inspect.isfunction):
            if not hasattr(obj, "__temporal_activity_definition"):
                continue
            try:
                name = _Definition.from_callable(obj).name
            except Exception:  # noqa: BLE001
                name = obj.__name__
            existing = by_name.get(name)
            if existing is not None and existing is not obj:
                logger.warning("duplicate activity name '%s' (%s vs %s) -- keeping first",
                               name, existing.__module__, obj.__module__)
                continue
            by_name[name] = obj

    for mod_name in extra_modules:
        try:
            mod = importlib.import_module(mod_name)
            for _, obj in inspect.getmembers(mod, inspect.isfunction):
                if not hasattr(obj, "__temporal_activity_definition"):
                    continue
                try:
                    name = _Definition.from_callable(obj).name
                except Exception:  # noqa: BLE001
                    name = obj.__name__
                by_name.setdefault(name, obj)
        except Exception:  # noqa: BLE001
            logger.warning("skipping extra module %s", mod_name, exc_info=True)

    return list(by_name.values())


async def main() -> None:
    logger.info("Temporal worker starting  host=%s  ns=%s  queue=%s",
                settings.temporal_host, settings.temporal_namespace, settings.temporal_task_queue)

    # Activities need Redis (cache) and the DB-stored generated task functions.
    from cache.redis import init_redis
    await init_redis()
    from agents._shared.generated_activities import load_from_db
    for attempt in range(1, 11):
        try:
            loaded = await load_from_db()
            break
        except Exception as exc:
            if attempt == 10:
                raise
            wait = min(5 * attempt, 30)
            logger.warning("load_from_db attempt %d/10 failed (%s), retrying in %ds",
                           attempt, exc, wait)
            await asyncio.sleep(wait)
    logger.info("loaded %d generated task(s) from DB", loaded)

    # The escalation timeout path (auto_reject_approval_activity) drives the
    # LangGraph session graph from inside this worker via resume_session(), which
    # needs a checkpointer. The backend API process inits its own; the worker must
    # init its own too (same DATABASE_URL -> same Postgres checkpoint tables, so a
    # worker-side resume sees the thread the backend parked). Without this, the
    # auto-reject resume crashes with "checkpointer not initialised" and the parked
    # session is wedged forever.
    from workflows.graph.observability import init_checkpointer, init_langfuse, flush_langfuse
    await init_checkpointer()
    # Post-approval agent tasks run as activities in this process; the API's
    # callback handler can't reach them, so Langfuse must be init'd here too.
    init_langfuse()

    activities = _collect_activities()
    from workflows.temporal.workflow.run_activity_workflow import RunActivityWorkflow
    from workflows.temporal.workflow.escalating_approval_workflow import EscalatingApprovalWorkflow
    from workflows.temporal.worker.langfuse_interceptor import LangfuseActivityInterceptor
    from workflows.temporal.worker.org_scope_interceptor import OrgScopeActivityInterceptor

    client = await Client.connect(settings.temporal_host, namespace=settings.temporal_namespace)
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[RunActivityWorkflow, EscalatingApprovalWorkflow],
        activities=activities,
        # Org scope first (outermost): binds each activity to its tenant source
        # BEFORE any inner interceptor or the activity body makes a hasura call.
        interceptors=[OrgScopeActivityInterceptor(), LangfuseActivityInterceptor()],
    )
    logger.info("[ok] Temporal worker ready  activities=%d  queue=%s",
                len(activities), settings.temporal_task_queue)
    try:
        await worker.run()
    finally:
        flush_langfuse()


if __name__ == "__main__":
    asyncio.run(main())
