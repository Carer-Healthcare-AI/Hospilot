"""Graph-level error types.

`TaskExecutionError` is raised at the task-execution seam (graph.agents._activity.
run_activity) when a single agent task fails after its retries are exhausted. It
carries the failed task's identity so the node wrapper can surface *which* task
failed (not just which agent) and trigger the failure-reorchestration flow.

It is distinct from `SessionFatalError` (graph.state): a fatal error halts the
whole session with no recovery, whereas a TaskExecutionError halts the run but
hands control to the planning graph to recommend a revised plan for the user.
"""


class TaskExecutionError(RuntimeError):
    """A single agent task failed after retries. Carries task/agent identity."""

    def __init__(self, activity: str, task_id: str | None, agent_id: str | None, cause: BaseException):
        self.activity = activity
        self.task_id = task_id
        self.agent_id = agent_id
        self.cause = cause
        super().__init__(
            f"task failed  agent={agent_id}  task={task_id}  activity={activity}: {cause}"
        )
