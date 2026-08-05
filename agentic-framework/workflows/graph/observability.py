"""Shared singletons for the LangGraph runtime:

  - the durable Postgres checkpointer (AsyncPostgresSaver) -- enables crash
    recovery and resuming approval-interrupted sessions across HTTP requests
  - the Langfuse callback handler -- traces every graph run, node, and LLM call,
    keyed by session_id, into one trace tree

Both are created once at app startup and reused. Langfuse is a clean no-op when
keys are unset; the checkpointer falls back to in-memory (dev only) when
DATABASE_URL is unset, with a loud warning.
"""

import asyncio
import logging
from contextlib import contextmanager

from config import settings

logger = logging.getLogger(__name__)

_checkpointer = None
_pg_pool = None  # AsyncConnectionPool -- keeps connections alive (keepalives enabled)
_langfuse_handler = None
_langfuse_client = None  # langfuse 3.x Langfuse() client — used for flush()


# -- Checkpointer --------------------------------------------------------------

async def init_checkpointer():
    global _checkpointer, _pg_pool
    if not settings.database_url:
        from langgraph.checkpoint.memory import MemorySaver
        _checkpointer = MemorySaver()
        logger.warning(
            "DATABASE_URL unset -- using in-memory checkpointer. "
            "Approvals will NOT survive a restart. Set DATABASE_URL for production."
        )
        return _checkpointer

    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    # Append TCP keepalives so NAT/firewalls don't silently drop idle connections.
    dsn = settings.database_url
    sep = "&" if "?" in dsn else "?"
    dsn += f"{sep}keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=5"

    # Pool: min_size=1 keeps one connection warm; max_idle=60 forces recreation
    # before the 5-min NAT timeout can drop it silently.
    _pg_pool = AsyncConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=5,
        max_idle=30,          # recycle before most NAT/firewall idle timeouts
        max_lifetime=300,     # force full reconnect every 5 min
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await _pg_pool.open(wait=True)
    _checkpointer = AsyncPostgresSaver(_pg_pool)
    await _checkpointer.setup()    # idempotent -- creates checkpoint tables if missing
    logger.info("Postgres checkpointer ready  pool_size=1-5")
    return _checkpointer


def get_checkpointer():
    if _checkpointer is None:
        raise RuntimeError("checkpointer not initialised -- call init_checkpointer() first")
    return _checkpointer


async def reset_checkpoint_thread(session_id: str) -> None:
    """Delete every checkpoint for a thread so a new (edited) graph topology can start on
    a clean thread_id. AsyncPostgresSaver.adelete_thread raises NotImplementedError in this
    langgraph-checkpoint version, so we delete the rows directly (tables checkpoints /
    checkpoint_blobs / checkpoint_writes are all keyed by thread_id). MemorySaver / no-DB:
    best-effort no-op. WITHOUT this reset, seeding a fresh run on a thread that still holds
    the old checkpoint makes LangGraph inherit the old state (edit-resume would then reuse
    already-completed agents even when reverting to an earlier checkpoint)."""
    if _pg_pool is None:
        return  # MemorySaver or uninitialised -- nothing durable to clear
    async with _pg_pool.connection() as conn:
        for tbl in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            await conn.execute(f"DELETE FROM {tbl} WHERE thread_id = %s", (session_id,))


async def close_checkpointer():
    global _pg_pool, _checkpointer
    if _pg_pool is not None:
        try:
            await asyncio.wait_for(_pg_pool.close(), timeout=3.0)
        except Exception:  # noqa: BLE001
            logger.warning("checkpointer pool close timed out or errored -- ignoring")
        _pg_pool = None
    _checkpointer = None


# -- Langfuse ------------------------------------------------------------------

def init_langfuse():
    global _langfuse_handler, _langfuse_client
    if not settings.langfuse_enabled:
        logger.info("Langfuse disabled (keys unset) -- tracing is a no-op")
        return None
    import os
    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler
    # pydantic-settings doesn't populate os.environ; set explicitly so langfuse SDK picks them up
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)
    _langfuse_client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
    _langfuse_handler = CallbackHandler()
    logger.info("[ok] Langfuse tracing enabled  host=%s", settings.langfuse_host)
    return _langfuse_handler


def get_langfuse_handler():
    return _langfuse_handler


def get_langfuse_client():
    return _langfuse_client


def trace_id_for(session_id: str) -> str | None:
    """Deterministic Langfuse trace id derived from the session id.

    Both the API process (planning + execution orchestration) and the Temporal
    worker (agent task activities) compute the SAME id from the session id, so
    every span -- regardless of which process produced it -- lands in one trace
    tree per session. Returns None when Langfuse is disabled.
    """
    if _langfuse_client is None or not session_id:
        return None
    from langfuse import Langfuse
    return Langfuse.create_trace_id(seed=session_id)


@contextmanager
def session_trace(session_id: str, name: str = "session"):
    """Pin everything emitted inside the block to the session's single trace.

    The LangChain CallbackHandler nests its spans under the currently-active
    Langfuse span, so wrapping astream in this span makes the auto-generated
    graph/node spans attach to the deterministic per-session trace id (instead of
    a fresh random trace per run). No-op when Langfuse is disabled.
    """
    tid = trace_id_for(session_id)
    if _langfuse_client is None or tid is None:
        yield
        return
    try:
        with _langfuse_client.start_as_current_span(
            name=name, trace_context={"trace_id": tid}
        ) as span:
            try:
                span.update_trace(session_id=session_id)
            except Exception:  # noqa: BLE001
                pass
            yield
    except Exception:  # noqa: BLE001
        # Tracing must never break the run.
        logger.warning("session_trace span failed -- continuing untraced", exc_info=True)
        yield


def flush_langfuse():
    if _langfuse_client is not None:
        try:
            _langfuse_client.flush()
        except Exception:  # noqa: BLE001
            logger.warning("langfuse flush failed", exc_info=True)


# -- Run config ----------------------------------------------------------------

def run_config(session_id: str, goal: str = "") -> dict:
    """Build the per-run config: thread_id for checkpointing + Langfuse trace tagging."""
    cfg: dict = {"configurable": {"thread_id": session_id}}
    if _langfuse_handler is not None:
        cfg["callbacks"] = [_langfuse_handler]
        cfg["metadata"] = {"langfuse_session_id": session_id}
        if goal:
            cfg["run_name"] = goal[:60]
    return cfg
