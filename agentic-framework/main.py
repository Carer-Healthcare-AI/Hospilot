import asyncio
import logging
import sys
from contextlib import asynccontextmanager

# psycopg3 / psycopg_pool require the SelectorEventLoop on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from logging_config import setup_logging
setup_logging()                          # must run before any logger is created

from flow_log import setup_flow_logging
setup_flow_logging()

from config import settings
from cache.redis import init_redis, close_redis
from api.routes.sessions import router as sessions_router
from api.routes.approvals import router as approvals_router
from api.routes.queues import router as queues_router
from api.routes.ws import router as ws_router
from api.routes.agents import router as agents_router
from api.routes.auth import router as auth_router
from api.routes.orgs import router as orgs_router
from api.routes.users import router as users_router

logger = logging.getLogger("__main__")


async def _bootstrap_super_admin() -> None:
    """Create the platform super_admin on first boot (multi-tenancy).

    Runs only when BOOTSTRAP_ADMIN_USERNAME/PASSWORD are set AND no super_admin
    exists yet -- idempotent across restarts. super_admin is org-less
    (platform-level) and can never be created through the signup API."""
    if not (settings.bootstrap_admin_username and settings.bootstrap_admin_password):
        return
    from db.hasura import hasura
    from api.routes.auth import _hash_password
    try:
        if await hasura.count_users_by_role("super_admin") > 0:
            return
        user = await hasura.create_user(
            username=settings.bootstrap_admin_username.strip().lower(),
            password_hash=_hash_password(settings.bootstrap_admin_password),
            display_name=settings.bootstrap_admin_display_name,
            role="super_admin",
            org_id=None,
            status="active",
        )
        logger.warning(
            "BOOTSTRAP: super_admin '%s' created (id=%s). "
            "Change BOOTSTRAP_ADMIN_PASSWORD before production use.",
            user["username"], user["id"],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("super_admin bootstrap skipped (run migrations 050/051?): %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Hospilot backend starting  env=%s", settings.app_env)

    # Startup
    await init_redis()
    logger.info("Redis connected  url=%s", settings.redis_url)

    if settings.fabric_base_url:
        logger.info("Fabric data layer  url=%s  auth=%s",
                    settings.fabric_base_url, "on" if settings.fabric_api_key else "OFF (dev)")
    else:
        logger.warning("FABRIC_BASE_URL not set -- agent data reads will fail")

    # Cold-start Redis seed — continuous updates come from messaging.data_consumer
    from messaging.initial_sync import run_initial_sync
    await run_initial_sync()

    # Multi-tenancy: warm the org routing cache (org_id -> Hasura source/prefix)
    # and bootstrap the first super_admin if none exists. Both tolerate a DB
    # that predates migration 050 (they just log and move on).
    from db.hasura import hasura
    try:
        await hasura.load_org_registry()
    except Exception as exc:  # noqa: BLE001
        logger.warning("org registry not available (run migration 050?): %s", exc)
    await _bootstrap_super_admin()

    def _task_crash(name: str):
        def _cb(t: asyncio.Task):
            if not t.cancelled() and t.exception():
                logger.error("TASK CRASHED: %s -- %s", name, t.exception(), exc_info=t.exception())
        return _cb

    # LangGraph runtime: durable Postgres checkpointer + Langfuse tracing
    from workflows.graph.observability import init_checkpointer, init_langfuse, close_checkpointer, flush_langfuse
    await init_checkpointer()
    init_langfuse()

    # Load DB-stored generated task functions (was done in the Temporal worker startup)
    from agents._shared.generated_activities import load_from_db
    loaded = await load_from_db()
    logger.info("loaded %d generated task(s) from DB", loaded)

    # Kafka event/replay bus: the API process relays bus events to its WebSockets.
    # (broadcast() publishes to Kafka; the worker also publishes; this consumer is
    # what delivers worker-produced events to the browser.) No-op when disabled.
    if settings.kafka_enabled:
        from messaging.flow_event_relay import start_ws_relay
        from messaging.data_consumer import start_data_consumer
        from messaging.ack_consumer import start_ack_consumer
        await start_ws_relay()
        await start_data_consumer()
        await start_ack_consumer()
        logger.info("Kafka enabled  events=%s  data_prefix=%s  ack=%s",
                    settings.kafka_events_topic, settings.kafka_data_topic_prefix,
                    settings.kafka_ack_topic)

    # Temporal execution plane: warm the client (activities run on the separate worker).
    if settings.temporal_enabled:
        from workflows.temporal.client import get_temporal_client
        await get_temporal_client()
        logger.info("Temporal execution enabled  host=%s  queue=%s",
                    settings.temporal_host, settings.temporal_task_queue)

    # Approval timeout reaper -- resumes parked sessions with "timeout" after 30 min
    from workflows.graph.reaper import start_reaper
    reaper_task = asyncio.create_task(start_reaper(), name="approval-reaper")
    reaper_task.add_done_callback(_task_crash("approval-reaper"))

    # Yield to event loop so tasks can start and fail loudly before first request
    await asyncio.sleep(0)

    logger.info("Ready  http://localhost:8000")

    yield

    # Shutdown
    logger.info("Hospilot shutting down...")
    reaper_task.cancel()
    if settings.kafka_enabled:
        from messaging.flow_event_relay import stop_ws_relay
        from messaging.data_consumer import stop_data_consumer
        from messaging.ack_consumer import stop_ack_consumer
        from messaging.producer import close_producer
        await stop_ws_relay()
        await stop_data_consumer()
        await stop_ack_consumer()
        await close_producer()
    if settings.temporal_enabled:
        from workflows.temporal.client import close_temporal_client
        await close_temporal_client()
    flush_langfuse()
    await close_checkpointer()
    await close_redis()
    logger.info("Shutdown complete")


app = FastAPI(
    title="Hospilot Backend",
    version="0.1.0",
    lifespan=lifespan,
)

@app.middleware("http")
async def sanitize_json_body(request: Request, call_next):
    if "application/json" in request.headers.get("content-type", "") and not request.url.path.startswith("/fhir"):
        raw = await request.body()
        cleaned = raw.replace(b"\r\n", b" ").replace(b"\r", b" ").replace(b"\n", b" ")
        async def receive():
            return {"type": "http.request", "body": cleaned, "more_body": False}
        request._receive = receive
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    #allow_credentials=True,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api")
app.include_router(orgs_router, prefix="/api")
app.include_router(users_router, prefix="/api")
app.include_router(sessions_router, prefix="/api")
app.include_router(approvals_router, prefix="/api")
app.include_router(queues_router, prefix="/api")
app.include_router(agents_router, prefix="/api")
app.include_router(ws_router)


@app.get("/health")
async def health():
    return {"status": "ok", "env": settings.app_env}
