"""Multi-tenant org scoping for Temporal activities.

Agent activities run in THIS worker process, but the org they belong to lives in
the API process's exec-context contextvar -- which does NOT cross the Temporal
dispatch boundary (only the activity name + args do). Without this, a hasura
tenant-table write inside an activity (e.g. create_approval_task) falls back to
the default Carer source and, for a non-default tenant, the session row it
references lives in a different source -> foreign-key violation.

This interceptor resolves the session's org from the activity args (the same
session id the langfuse interceptor extracts) and binds it into exec_context so
every hasura tenant call made inside the activity routes to the right source.
Always runs (independent of Langfuse) and never raises -- on any failure it
leaves the default source in place, the pre-existing behaviour.
"""
import logging

from temporalio import activity
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

from workflows.temporal.worker.langfuse_interceptor import _extract_session_id

logger = logging.getLogger("temporal.worker.orgscope")


class _OrgScopeActivityInbound(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput):
        try:
            name = activity.info().activity_type
            session_id = _extract_session_id(name, input.args)
            if session_id:
                # Late imports: keep worker import graph light and avoid cycles.
                from workflows.graph.runner import org_of_session
                from workflows.graph.exec_context import set_exec_ctx

                org = await org_of_session(session_id)
                set_exec_ctx(session_id, "", org_id=org or "")
        except Exception:  # noqa: BLE001
            logger.warning("org scope resolution failed -- using default source",
                           exc_info=True)
        return await super().execute_activity(input)


class OrgScopeActivityInterceptor(Interceptor):
    """Worker interceptor that pins each activity to its session's org source."""

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _OrgScopeActivityInbound(next)
