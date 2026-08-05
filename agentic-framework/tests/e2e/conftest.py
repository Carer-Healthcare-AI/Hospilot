"""Live E2E environment setup for the task sweep.

These tests hit the REAL backends the tasks use — Fabric, Hasura, the forecast
service, and (for a few) Claude. Two things have to be true before any task runs:

  1. Config is loaded from the repo-root .env, and fabric_base_url is repointed to
     the host-published Fabric port. The compose maps 8002:8001, so the app's
     in-container `localhost:8001` is `localhost:8002` when the suite runs on the host.
     Override with FABRIC_BASE_URL if your Fabric lives elsewhere.
  2. `broadcast` (the websocket push) is stubbed, since there is no live socket in a
     test process. We capture what each task tried to broadcast so tests can inspect it.
"""
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

# --- 1. environment: load shared .env, then repoint Fabric to the host port ---------
_REPO_ROOT = Path(__file__).resolve().parents[3]        # .../hospilot-internal
load_dotenv(_REPO_ROOT / ".env")

# NB: the repo .env sets FABRIC_BASE_URL=:8001 (the in-container port), so we can't
# read that back as the host value. Use a dedicated E2E var, defaulting to the
# compose-published host port :8002, and force it past the .env value.
_FABRIC = os.environ.get("E2E_FABRIC_BASE_URL", "http://localhost:8002")
os.environ["fabric_base_url"] = _FABRIC                  # before config is imported
os.environ["FABRIC_BASE_URL"] = _FABRIC                  # override the .env upper-case one

from config import settings                              # noqa: E402
settings.fabric_base_url = _FABRIC                       # belt-and-suspenders


@pytest.fixture(autouse=True)
async def redis_ready():
    """Tasks that stage/cache go through cache.redis, which the worker inits at
    startup. pytest-asyncio uses a fresh event loop per test, and an aioredis client
    is bound to the loop it was created on — so init a fresh client for THIS test's
    loop and close it in-loop to avoid 'attached to a different loop' errors."""
    import cache.redis as r
    await r.init_redis()
    yield
    await r.close_redis()
    r._client = None


@pytest.fixture(autouse=True)
async def seed_session():
    """Several tasks write an audit row keyed by session_id (FK -> sessions table).
    Insert a minimal session row for SESSION_ID so those writes succeed. Idempotent:
    a duplicate insert on reruns is ignored."""
    from db.hasura import hasura
    from _helpers import SESSION_ID
    try:
        await hasura.create_session(SESSION_ID, goal="e2e-test", constraints="", pipeline={})
    except Exception:
        pass  # row already exists from a previous run — that's all the FK needs
    yield


@pytest.fixture(autouse=True)
def mock_broadcast(monkeypatch):
    """Replace websocket broadcast everywhere and capture the messages.

    Tasks do `from api.routes.ws import broadcast`, so each module holds its own
    reference — patch the source and every already-imported agent module copy.
    Yields the captured list so a test can assert on emitted alerts if it wants.
    """
    import sys
    import api.routes.ws as ws

    captured: list[tuple] = []

    async def _fake(session_id, message, *a, **k):
        captured.append((session_id, message))

    monkeypatch.setattr(ws, "broadcast", _fake)
    for name, mod in list(sys.modules.items()):
        if name.startswith("agents.") and getattr(mod, "broadcast", None) is not None:
            monkeypatch.setattr(mod, "broadcast", _fake, raising=False)
    yield captured
