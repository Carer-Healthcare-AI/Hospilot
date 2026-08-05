from contextlib import nullcontext
from dataclasses import dataclass

from temporalio import activity

from cache import redis as cache


@dataclass
class CachePrefetchInput:
    session_id: str
    task_id: str
    result: dict


@dataclass
class GetPrefetchInput:
    session_id: str
    task_id: str


@activity.defn
async def cache_prefetch_result(inp: CachePrefetchInput) -> None:
    await cache.set_session_result(
        inp.session_id,
        f"prefetch:{inp.task_id}",
        inp.result,
    )


@activity.defn
async def get_prefetch_cache(inp: GetPrefetchInput) -> dict:
    from workflows.graph.observability import get_langfuse_client, trace_id_for
    lf = get_langfuse_client()
    tid = trace_id_for(inp.session_id) if lf else None
    _span_cm = (
        lf.start_as_current_span(
            name=f"prefetch:{inp.task_id}", trace_context={"trace_id": tid}
        )
        if (lf and tid) else nullcontext()
    )
    with _span_cm as span:
        result = await cache.get_session_result(inp.session_id, f"prefetch:{inp.task_id}") or {}
        if span is not None:
            try:
                span.update(
                    input={"task_id": inp.task_id},
                    output={
                        "hit": bool(result),
                        "keys": list(result.keys())[:20] if result else [],
                        "sizes": {
                            k: (len(v) if isinstance(v, (list, dict, str)) else 1)
                            for k, v in list(result.items())[:20]
                        } if result else {},
                    },
                )
            except Exception:  # noqa: BLE001
                pass
    return result
