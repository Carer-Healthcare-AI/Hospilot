"""
Hospilot-Fabric — transformation layer in front of the DB's APIs.

Fabric is a CLIENT of the DB:
  • it GETs canonical FHIR R5 from the DB's FHIR API (clinical), and calls the
    DB's plain-REST financial API (billing), then
  • transforms both into the normalized dict shapes the main backend wants, and
  • serves them over a plain REST API (this app).

If the DB's APIs change (or a different EHR/DB is integrated), only Fabric
changes — the main backend keeps calling these normalized endpoints unchanged.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware

from logging_config import setup_logging
setup_logging()

from config import settings

logger = logging.getLogger("__main__")


async def require_fabric_auth(request: Request) -> None:
    """Guard Fabric's endpoints with a shared key (main app → Fabric)."""
    key = settings.fabric_api_key
    if not key:
        return
    auth = request.headers.get("authorization", "")
    provided = auth[7:].strip() if auth[:7].lower() == "bearer " else request.headers.get("x-api-key")
    if provided != key:
        raise HTTPException(status_code=401, detail="Missing or invalid Fabric API key")


from writeback.http import router as changes_router
from runtime import router as runtime_router
from initial_sync import router as sync_router
from ingest import change_poller
from ingest import diff_poller
from ingest import kafka_consumer
from messaging import producer as kafka
from writeback.kafka import write_publisher


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("▶ Hospilot-Fabric (transformation layer) starting  env=%s", settings.app_env)
    logger.info("✓ upstream FHIR API      %s", settings.ehr_fhir_base_url)
    logger.info("✓ upstream financial API %s", settings.financial_api_base_url)
    logger.info("✓ upstream REST API      %s", settings.db_rest_base_url)
    logger.info("✓ upstream sync API      %s", settings.sync_api_base_url)
    logger.info("✓ Fabric auth=%s", "on" if settings.fabric_api_key else "OFF (dev)")

    await kafka.start()
    poll_task = None
    write_task = None
    if kafka.enabled():
        if settings.kafka_mode:
            logger.info("✓ ingest mode = KAFKA (event-triggered from hospilot.changes.*)")
            poll_task = asyncio.create_task(kafka_consumer.run())
        elif settings.polling_mode:
            logger.info("✓ ingest mode = POLLING (per-entity field-level diff)")
            poll_task = asyncio.create_task(diff_poller.run())
        else:
            logger.info("✓ ingest mode = CHANGE_API ($changed-resources feed)")
            poll_task = asyncio.create_task(change_poller.run())
        # Write direction: kafka mode PUSHES proposals; change_api/polling keep the
        # HTTP $pending-changes pull (writeback/http/ stays active in those modes).
        if settings.kafka_mode:
            logger.info("✓ write mode = KAFKA → %s", settings.kafka_write_topic)
            write_task = asyncio.create_task(write_publisher.run())
        else:
            logger.info("✓ write mode = HTTP PULL ($pending-changes)")
    elif settings.polling_mode or settings.kafka_mode:
        logger.warning("⚠ INTEGRATION_MODE=%s but Kafka disabled — no ingest started; "
                       "writes fall back to the HTTP pull", settings.integration_mode)
    else:
        logger.info("✓ change poller OFF (Kafka disabled)")

    logger.info("✓ Ready")
    yield

    for task in (poll_task, write_task):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    await kafka.stop()
    logger.info("Hospilot-Fabric shutting down")


app = FastAPI(
    title="Hospilot-Fabric",
    version="0.2.0",
    description="Transformation layer: calls the DB's FHIR + financial APIs and serves normalized JSON.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Three HTTP surfaces, distinguished by who calls them (see each package's docstring):
app.include_router(runtime_router, dependencies=[Depends(require_fabric_auth)])   # hospilot's agents, continuously
app.include_router(changes_router, dependencies=[Depends(require_fabric_auth)])   # the HIS, collecting queued writes
app.include_router(sync_router,    dependencies=[Depends(require_fabric_auth)])   # the backend, seeding its cache once


@app.get("/health")
async def health():
    return {"status": "ok", "service": "hospilot-fabric", "env": settings.app_env}
