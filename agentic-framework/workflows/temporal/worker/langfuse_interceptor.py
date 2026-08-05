"""Langfuse tracing for Temporal activities.

The API process traces planning + graph orchestration via the LangChain callback
handler. Once a plan is approved, every agent task runs as a Temporal activity in
THIS worker process -- a different process the orchestrator's callback handler can
never reach. This interceptor wraps every activity execution in a Langfuse span,
pinned to the SAME deterministic trace id derived from the session id, so worker
spans nest into the one per-session trace. Fully defensive: any failure falls back
to running the activity untraced.
"""
import dataclasses
import logging

from opentelemetry import context as otel_context
from temporalio import activity
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
)

logger = logging.getLogger("temporal.worker.langfuse")
_MAX_REPR = 2000


def _safe(obj):
    """Best-effort conversion to a JSON-friendly value for span input/output."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _safe(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)[:_MAX_REPR]


def _extract_session_id(name: str, args) -> str | None:
    """Find the session id in an activity's decoded args (covers every dispatch shape)."""
    if name == "run_generated_task" and len(args) >= 2 and isinstance(args[1], str):
        return args[1]
    for a in args:
        sid = getattr(a, "session_id", None)
        if isinstance(sid, str) and sid:
            return sid
        if isinstance(a, dict):
            sid = a.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
    str_args = [a for a in args if isinstance(a, str)]
    if len(str_args) == 1:
        return str_args[0]
    return None


class _LangfuseActivityInbound(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput):
        from workflows.graph.observability import get_langfuse_client, trace_id_for

        client = get_langfuse_client()
        if client is None:
            return await super().execute_activity(input)

        try:
            name = activity.info().activity_type
            session_id = _extract_session_id(name, input.args)
            tid = trace_id_for(session_id) if session_id else None
        except Exception:
            return await super().execute_activity(input)

        span_kwargs = {"name": f"activity:{name}"}
        if tid:
            span_kwargs["trace_context"] = {"trace_id": tid}

        try:
            span_cm = client.start_as_current_span(**span_kwargs)
        except Exception:
            logger.warning("langfuse span start failed -- running untraced", exc_info=True)
            return await super().execute_activity(input)

        # Clear any stale OTEL context left over from a prior activity in this
        # worker thread; otherwise start_as_current_span inherits a parent span
        # from a different trace and the new span appears orphaned in the UI.
        _ctx_token = otel_context.attach(otel_context.Context())
        try:
            with span_cm as span:
                if session_id:
                    try:
                        span.update_trace(session_id=session_id)
                    except Exception:
                        pass
                try:
                    # Keep the session id out of the activity's inputs -- it's
                    # plumbing (often a bare positional arg), already carried on the
                    # trace via update_trace and echoed here in metadata.
                    span_args = [
                        a for a in input.args
                        if not (session_id and isinstance(a, str) and a == session_id)
                    ]
                    span.update(
                        input={"activity": name, "args": _safe(span_args)},
                        metadata={"session_id": session_id} if session_id else {},
                    )
                except Exception:
                    pass
                # On exception the `with` records the error and re-raises, so
                # Temporal still sees the failure and applies its retry policy.
                result = await super().execute_activity(input)
                try:
                    span.update(output=_safe(result))
                except Exception:
                    pass
                return result
        finally:
            otel_context.detach(_ctx_token)


class LangfuseActivityInterceptor(Interceptor):
    """Worker interceptor that traces every activity into the session's trace."""

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _LangfuseActivityInbound(next)
