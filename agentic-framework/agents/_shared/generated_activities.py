# Hospilot -- generated task loader.
# Functions are stored in DB (task_registry.function_code) and exec()'d at worker startup.
# Do not append functions here manually -- use the registry API to create tasks.

import logging

from temporalio import activity
from db.hasura import hasura     # noqa: F401
from cache import redis as cache  # noqa: F401
from api.routes.ws import broadcast     # noqa: F401

logger = logging.getLogger("task_loader")

GENERATED_TASKS: dict = {}

# Shared exec() namespace -- every name used at module level in generated code.
_EXEC_NS: dict = {
    "activity":        activity,
    "hasura":          hasura,
    "cache":           cache,
    "broadcast":       broadcast,
    "GENERATED_TASKS": GENERATED_TASKS,
}


def _exec_code(task_id: str, code: str) -> bool:
    """Compile and exec a generated function into _EXEC_NS. Returns True on success."""
    try:
        exec(compile(code.strip(), f"<task:{task_id}>", "exec"), _EXEC_NS)
        logger.info("LOADER loaded  task=%s", task_id)
        return True
    except Exception as exc:
        logger.error("exec failed  task=%s  err=%s", task_id, exc)
        return False


async def load_from_db() -> int:
    """
    Fetch all generated task function codes from DB and exec() them.
    Called once at worker startup before Worker() is created.
    """
    rows = await hasura.fetch_all_function_codes()
    if not rows:
        logger.info("LOADER no generated tasks in DB")
        return 0

    logger.info("LOADER loading %d generated task(s) from DB", len(rows))
    loaded = sum(
        1 for row in rows
        if (row.get("function_code") or "").strip()
        and _exec_code(row["id"], row["function_code"])
    )
    logger.info("LOADER %d/%d loaded", loaded, len(rows))
    return loaded


@activity.defn(name="run_generated_task")
async def run_generated_task(task_id: str, session_id: str) -> dict:
    """
    Dispatcher activity -- registered once at worker startup.
    Handles ANY generated task without requiring a worker restart.
    If the function isn't loaded yet (task created after startup), fetches from DB on demand.
    """
    if task_id not in GENERATED_TASKS:
        logger.info("LOADER on-demand load  task=%s", task_id)
        row = await hasura.fetch_function_code(task_id)
        if row and row.get("function_code"):
            _exec_code(task_id, row["function_code"])

    func = GENERATED_TASKS.get(task_id)
    if not func:
        logger.error("generated task not found  task=%s", task_id)
        return {"status": "error", "message": f"generated task {task_id} not found"}

    return await func(session_id)
