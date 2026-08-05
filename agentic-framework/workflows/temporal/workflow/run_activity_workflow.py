"""Generic execution workflow.

LangGraph stays the orchestrator; this tiny workflow is just the durable wrapper
that runs ONE agent task as a Temporal activity with a declarative retry policy.
The agent body (in the API process) calls graph.agents._activity.run_activity,
which starts one of these per task and awaits the result.

The activity is dispatched BY NAME. Dataclass inputs round-trip as JSON: the
client serializes the input, it arrives here as a plain value, and the worker
re-materialises it using the registered activity's own type hints. So this
workflow stays fully generic and type-agnostic.
"""
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@dataclass
class ActivityRequest:
    name: str                       # registered @activity.defn name
    args: list = field(default_factory=list)   # positional args for the activity
    start_to_close_seconds: int = 120
    max_attempts: int = 3


@workflow.defn
class RunActivityWorkflow:
    @workflow.run
    async def run(self, req: ActivityRequest):
        return await workflow.execute_activity(
            req.name,
            args=req.args,
            start_to_close_timeout=timedelta(seconds=req.start_to_close_seconds),
            retry_policy=RetryPolicy(
                maximum_attempts=req.max_attempts,
                initial_interval=timedelta(seconds=1),
                backoff_coefficient=2.0,
                # Deterministic programming errors won't succeed on retry -- fail fast.
                non_retryable_error_types=["ValueError", "KeyError", "TypeError"],
            ),
        )
