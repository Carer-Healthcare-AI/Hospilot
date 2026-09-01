"""Live end-to-end flow tests: environment, service gate, and the flow driver.

These run WHOLE PIPELINES through the real graph runner against the real
backends — Fabric, Hasura, Redis, the checkpointer and (per agent) Claude. They
are the counterpart to tests/e2e, which exercises individual task activities;
here the unit under test is the orchestration itself: level ordering, fan-in and
fan-out, conditional skips, and synthesis.

Because they need a running stack they are OPT-IN. Every test in this directory
carries the `live` marker (applied automatically below) and is deselected unless
you ask for it:

    pytest tests/flows -m live          # run them
    pytest tests/flows                  # collected, then skipped

If the marker is requested but the stack is not reachable, the whole directory
skips with a message naming what was missing — a missing service is an
un-runnable test, not a failure.
"""
import os
import uuid
from pathlib import Path

import pytest
from dotenv import load_dotenv

# ── 1. environment: same contract as tests/e2e/conftest.py ───────────────────
# The compose maps 8002:8001, so the app's in-container localhost:8001 is
# localhost:8002 from the host. Set this BEFORE importing config.
_REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(_REPO_ROOT / ".env")

_FABRIC = os.environ.get("E2E_FABRIC_BASE_URL", "http://localhost:8002")
os.environ["fabric_base_url"] = _FABRIC
os.environ["FABRIC_BASE_URL"] = _FABRIC

# config.py validates these at import time. The live tests need the real values
# (from .env above); the static catalog tests in test_flow_coverage.py need only
# to import the registry, so supply placeholders rather than making plain CI
# unable to collect this directory at all.
os.environ.setdefault("HASURA_URL", "http://localhost/v1/graphql")
os.environ.setdefault("HASURA_ADMIN_SECRET", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from config import settings  # noqa: E402

settings.fabric_base_url = _FABRIC

# A flow can legitimately take a while: every agent in it runs its own tasks and
# several call an LLM. Overridable for a slow or cold stack.
FLOW_TIMEOUT_SECONDS = float(os.environ.get("FLOW_TIMEOUT_SECONDS", "300"))


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: end-to-end flow test requiring a running stack")


def pytest_collection_modifyitems(config, items):
    """Mark the LIVE flow tests and skip them unless `-m live` was requested.

    Only the tests that actually drive a pipeline are gated. The static catalog
    checks in test_flow_coverage.py need nothing but the registry import, so they
    run in ordinary CI — that is what makes "an agent was added but no flow
    covers it" fail on a normal PR rather than only in a live run.

    Keeping the skip here (rather than a module-level skipif) also means the live
    tests still COLLECT in plain runs, so a typo in a flow definition surfaces as
    a collection error instead of hiding until someone runs the stack.
    """
    requested = config.getoption("-m", default="")
    skip = pytest.mark.skip(reason="live flow test — run with `-m live` and a running stack")
    for item in items:
        if "test_flows_live" not in item.nodeid:
            continue
        item.add_marker(pytest.mark.live)
        if "live" not in requested:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
async def live_stack():
    """Fail fast, and legibly, when the stack this suite needs is not up.

    Checked once per session: Hasura (the registry + session rows), Redis (the
    pipeline cache the runner reads on every drive) and the checkpointer. A
    missing piece skips the directory rather than producing N identical
    connection errors.
    """
    missing = []

    try:
        from db.hasura import hasura
        await hasura.fetch_agent_registry()
    except Exception as e:  # noqa: BLE001
        missing.append(f"Hasura ({type(e).__name__}: {e})")

    try:
        import cache.redis as r
        await r.init_redis()
    except Exception as e:  # noqa: BLE001
        missing.append(f"Redis ({type(e).__name__}: {e})")

    try:
        from workflows.graph.observability import init_checkpointer
        await init_checkpointer()
    except Exception as e:  # noqa: BLE001
        missing.append(f"checkpointer ({type(e).__name__}: {e})")

    if missing:
        pytest.skip("live stack unavailable — " + "; ".join(missing))

    yield

    try:
        import cache.redis as r
        await r.close_redis()
        r._client = None
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
async def redis_ready():
    """pytest-asyncio gives each test a fresh event loop, and an aioredis client is
    bound to the loop that created it. Re-init per test and close in-loop, exactly
    as tests/e2e does, to avoid 'attached to a different loop'."""
    import cache.redis as r
    await r.init_redis()
    yield
    await r.close_redis()
    r._client = None


@pytest.fixture
def captured_broadcasts(monkeypatch):
    """Replace the websocket push and capture what the flow emitted.

    There is no live socket in a test process. Agents do `from api.routes.ws
    import broadcast`, so each module holds its own reference — patch the source
    AND every already-imported agent/workflow module copy.
    """
    import sys
    import api.routes.ws as ws

    captured: list[tuple] = []

    async def _fake(session_id, message, *a, **k):
        captured.append((session_id, message))

    monkeypatch.setattr(ws, "broadcast", _fake)
    for name, mod in list(sys.modules.items()):
        if (name.startswith(("agents.", "workflows."))
                and getattr(mod, "broadcast", None) is not None):
            monkeypatch.setattr(mod, "broadcast", _fake, raising=False)
    return captured


@pytest.fixture
async def flow_session():
    """Create a real session row and hand back its id.

    A fresh uuid per test: approval paths start Temporal workflows whose id
    embeds the session_id, so a reused value collides with "already started" on
    a rerun. Several agent tasks also write audit rows with an FK to this row, so
    it has to exist before the flow runs.

    Rows are intentionally NOT deleted afterwards. There is no delete_session on
    the Hasura client, and the rows are the record of what the run did — the same
    thing tests/e2e relies on when a flow needs investigating. Sessions are named
    goal="flow-e2e", so they are easy to reap in bulk against a test database.
    """
    from db.hasura import hasura

    session_id = str(uuid.uuid4())
    await hasura.create_session(session_id, goal="flow-e2e", constraints="", pipeline={})
    yield session_id
