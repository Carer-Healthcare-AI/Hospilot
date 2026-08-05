"""Task execution seam: LangGraph orchestrates, Temporal executes.

`run_activity(fn, *args)` is the single dispatch point every agent task goes
through. When Temporal is enabled it submits the activity to the Temporal worker
via the generic RunActivityWorkflow (durable, independently retried, isolated)
and awaits the result. When disabled (single-process dev/tests) it calls the
activity function in-process -- the original behavior.

`execute_activity` is kept for the existing `args=[...]` call convention and
delegates to `run_activity`.

    await run_activity(fn, single_input)        # one positional (dataclass) input
    await execute_activity(fn, args=[a, b])     # multi-arg activities
"""
import asyncio
import dataclasses
import json
import logging
from contextlib import nullcontext
from typing import Any, Awaitable, Callable
from uuid import uuid4

from langgraph.errors import GraphInterrupt

from config import settings
from workflows.graph.errors import TaskExecutionError
from workflows.graph.exec_context import get_exec_ctx

logger = logging.getLogger(__name__)


def _lf_safe(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return dataclasses.asdict(v)
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, dict):
        return {str(k): _lf_safe(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_lf_safe(i) for i in v]
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return repr(v)[:500]


def _activity_name(fn: Callable[..., Any]) -> str:
    """Resolve the registered Temporal activity name (defaults to __name__)."""
    defn = getattr(fn, "__temporal_activity_definition", None)
    name = getattr(defn, "name", None)
    return name or fn.__name__


async def run_activity(
    fn: Callable[..., Awaitable[Any]],
    *args: Any,
    timeout_seconds: int = 120,
    max_attempts: int = 3,
) -> Any:
    """Execute one agent task. Routes to Temporal when enabled, else in-process.

    On final failure (retries exhausted, or an in-process raise) the exception is
    wrapped in TaskExecutionError carrying the failed task's identity (from the
    per-node exec context), so the node wrapper can surface which task failed and
    trigger failure-reorchestration. GraphInterrupt (HITL) is re-raised untouched.
    """
    name = _activity_name(fn)

    # Wrap the execution in a Langfuse span so every task — whether it runs
    # in-process or via Temporal — appears in the trace nested under its agent.
    # The span is created in the API process where the OTEL context (set by the
    # LangChain callback handler for the agent node) is still active, so it
    # automatically nests under the right agent span.
    from workflows.graph.observability import get_langfuse_client, trace_id_for
    ctx = get_exec_ctx() or {}
    session_id = ctx.get("session_id", "")
    lf = get_langfuse_client()
    tid = trace_id_for(session_id) if (lf and session_id) else None
    _span_cm = (
        lf.start_as_current_span(name=f"task:{name}", trace_context={"trace_id": tid})
        if (lf and tid) else nullcontext()
    )

    from flow_log import log_task_start, log_task_done, dump_task_source
    agent_id = ctx.get("agent_id", "")
    log_task_start(session_id, agent_id, name)
    await dump_task_source(session_id, agent_id, fn)

    with _span_cm as _span:
        if _span is not None:
            try:
                # Keep the session id out of the task's inputs -- it's plumbing, not
                # an argument (e.g. get_available_ambulances takes it as args[0]). It
                # already lives on the trace (session_id) and is echoed in metadata.
                span_args = [a for a in args if not (isinstance(a, str) and a == session_id)]
                _span.update(
                    input={"args": _lf_safe(span_args)},
                    metadata={"session_id": session_id} if session_id else {},
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            if not settings.temporal_enabled:
                result = await fn(*args)
            else:
                from workflows.temporal.client import get_temporal_client
                from workflows.temporal.workflow.run_activity_workflow import RunActivityWorkflow, ActivityRequest

                client = await get_temporal_client()
                result = await client.execute_workflow(
                    RunActivityWorkflow.run,
                    ActivityRequest(
                        name=name,
                        args=list(args),
                        start_to_close_seconds=timeout_seconds,
                        max_attempts=max_attempts,
                    ),
                    id=f"act-{name}-{uuid4().hex[:12]}",
                    task_queue=settings.temporal_task_queue,
                )
            if _span is not None:
                try:
                    _span.update(output=_lf_safe(result))
                except Exception:  # noqa: BLE001
                    pass
            # Mirror the span into the human-readable frontend trace (never raises).
            from workflows.graph.trace import record_step, humanize_title
            await record_step(
                session_id, kind="task", title=humanize_title(ctx.get("task") or name), status="completed",
                agent_id=ctx.get("agent_id"), task_id=ctx.get("task"),
                raw_input={"args": list(args)}, raw_output=result,
            )
            log_task_done(session_id, agent_id, name)
            return result
        except GraphInterrupt:
            raise  # HITL suspend -- must propagate so LangGraph checkpoints
        except Exception as exc:  # noqa: BLE001
            if _span is not None:
                try:
                    _span.update(level="ERROR", status_message=str(exc)[:500])
                except Exception:  # noqa: BLE001
                    pass
            from workflows.graph.trace import record_step, humanize_title
            await record_step(
                session_id, kind="task", title=humanize_title(ctx.get("task") or name), status="failed",
                agent_id=ctx.get("agent_id"), task_id=ctx.get("task"),
                raw_input={"args": list(args)}, error=str(exc),
            )
            raise TaskExecutionError(name, ctx.get("task"), ctx.get("agent_id"), exc) from exc


async def execute_activity(fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Back-compat wrapper preserving the `args=[...]` convention."""
    call_args = kwargs.get("args")
    if call_args is not None:
        return await run_activity(fn, *call_args)
    return await run_activity(fn, *args)


async def with_retry(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    attempts: int = 2,
    base_delay: float = 2.0,
) -> Any:
    """Manual in-process retry. Superseded by Temporal's RetryPolicy when
    temporal_enabled; kept for any in-process call path that still wants it."""
    last: Exception | None = None
    for i in range(attempts):
        if i:
            await asyncio.sleep(base_delay * i)
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning("retry %d/%d failed: %s", i + 1, attempts, exc)
    raise last  # type: ignore[misc]
