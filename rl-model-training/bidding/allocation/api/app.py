"""The routes. Thin by design — every one of them delegates to ``service``.

Nine endpoints::

    GET  /health                     liveness, plus the versions this process is running
    GET  /config                     versions, unsigned rules, available scenarios
    GET  /use-cases                  registered profiles and a query that resolves to each
    GET  /scenarios                  scenario names this process will accept
    POST /auction                    run one auction, full bid ladder returned
    POST /session                    run N auctions across shifts, burn rate returned
    GET  /auction/{id}               a run from this process's memory
    GET  /auction/{id}/audit         every audit row that run produced
    GET  /auction/{id}/explain       the full derivation, as text/plain

The request models validate shape; ``service`` validates meaning and raises
:class:`~allocation.api.service.ApiError` with the status it wants. One exception handler maps
that to a response, so no route needs its own try/except and no failure reaches the client as
an unlabelled 500.
"""

from __future__ import annotations

from hmac import compare_digest
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

#: Reachable without a key even when one is configured. ``/health`` is open so a load
#: balancer or orchestrator can probe liveness without holding a credential — a health check
#: that needs a secret is a health check that gets disabled the first time it fails.
OPEN_PATHS = frozenset({"/health"})

#: ``?format=`` on the two POST routes. A presentation concern, so a query parameter rather
#: than a body field: the request describes the same auction either way, and a caller
#: switching between them should not have to edit the document they are sending.
#:
#: ``text`` exists because the most common client curl has is a person reading a terminal,
#: and 40 kB of single-line JSON is not an answer to "who got the bed". ``summary`` is the
#: shortest true answer to exactly that question: the winner and why, a screenful, no ladder.
#: ``brief`` is five lines, for a caller holding curl and nothing else — no ``jq``, no
#: scripting language to reshape a response with.
#:
#: ``steps`` is an alias for ``text``, not a sixth renderer. The deployed instances answer to
#: that name and the report calls itself "one auction, step by step", so a caller who reads the
#: output and guesses the format from it guesses ``steps``. Both names are kept because
#: breaking the deployed one to tidy the list would be a rename dressed as a cleanup.
OutputFormat = Literal["json", "text", "steps", "summary", "brief", "explain"]

from allocation.api import service
from allocation.api.service import (
    ApiError,
    AuctionRequest,
    RunStore,
    SessionRequest,
    Settings,
)

DESCRIPTION = """
HTTP surface over the HOSPILOT allocation auction.

Every number here comes from the same functions the CLI calls. Two things to know before
reading a response:

* **`mode: live` is refused** (403). The shipped data source serves three invented Appendix C
  patients; a live auction would hold a real bed for people who do not exist.
* **Nothing is persisted.** The `allocation.*` tables do not exist yet (F-02), and no episode
  can complete because no mortality column exists (F-01). Every response says so.
* **`policy: rl` needs a process started with `--policy`**, and shadows unless that process
  also had `--live-policy`. `GET /health` reports both, and every response names the policy
  that actually decided it.
"""


class AuctionBody(BaseModel):
    """``POST /auction``. Every field optional — an empty body runs the reference auction."""

    query: str = Field(
        default=service.DEFAULT_QUERY,
        description="the use-case sentence; must name a registered resource (GET /use-cases)",
    )
    mode: str = Field(
        default="simulation", description="simulation | advisory | replay. live is refused"
    )
    at: str | None = Field(
        default=None,
        description="ISO timestamp to run at; defaults to the Appendix C fixture clock, "
                    "because the fixture's vitals are anchored there",
    )
    scenario: str | None = Field(
        default=None, description="scenario name from GET /scenarios; omit for the fixture"
    )
    hospital: dict[str, Any] | None = Field(
        default=None,
        description="hospital state for an inline auction: unit, unit_total_beds, "
                    "unit_occupied_beds, expected_discharges_4h, boarding_count, ... The "
                    "icu_* spellings still parse and imply unit='icu'. To describe more than "
                    "one unit, send a 'units' list of these blocks and keep boarding_count / "
                    "lwbs_risk at the top level. The auction reads the unit its resource sits "
                    "in, so a body describing only the ICU cannot be asked for a ward bed. "
                    "Required with 'candidates'",
    )
    candidates: list[dict[str, Any]] | None = Field(
        default=None,
        description="the patients to auction between, one per agent, each with its vitals, "
                    "labs and orders. Requires ALLOCATION_API_KEY — this is patient data. "
                    "Omit to use the server's fixture or a named scenario",
    )
    rounds: int | None = Field(
        default=None, ge=1, le=20, description="override the profile's round count (RL-Steps: 3)"
    )
    uplift: bool = Field(
        default=True, description="false reverts to D.9's Ceiling = U, disabling the B.9 interim"
    )
    policy: Literal["heuristic", "rl"] | None = Field(
        default=None,
        description="which bidder decides. Omit and the process decides: 'rl' where it was "
                    "started with --policy, 'heuristic' where it was not — a server started to "
                    "serve weights serves them without every caller having to ask. 'rl' runs "
                    "SHADOWED — the heuristic still allocates — unless that process also had "
                    "--live-policy. GET /health reports the resolved default and both flags. "
                    "A request cannot name a weights file; the operator chose it at startup",
    )
    explain: bool = Field(
        default=False, description="include the full derivation as a text field"
    )
    derivation: bool = Field(
        default=False,
        description="include the full derivation as structured JSON — every component, every "
                    "factor with its weight, value, source and whether it was absent. Same "
                    "content as `python -m allocation --explain`, for a machine",
    )


class SessionBody(BaseModel):
    """``POST /session``. Many auctions against one ledger — this is what shows burn rate."""

    query: str = Field(default=service.DEFAULT_QUERY)
    at: str | None = Field(default=None)
    scenario: str | None = Field(default=None)
    hospital: dict[str, Any] | None = Field(default=None)
    candidates: list[dict[str, Any]] | None = Field(
        default=None, description="as POST /auction; the same patients bid at every event"
    )
    rounds: int | None = Field(default=None, ge=1, le=20)
    uplift: bool = Field(default=True)
    policy: Literal["heuristic", "rl"] | None = Field(
        default=None, description="as POST /auction; the same bidder runs every event"
    )
    events: int = Field(
        default=8, ge=1, le=service.MAX_SESSION_EVENTS, description="how many auctions to run"
    )
    every: str = Field(default="45m", description="spacing between events, e.g. 45m, 2h, 1d")


def create_app(
    config_dir: Path | None = None,
    scenario_dir: Path | None = None,
    store_size: int = service.DEFAULT_STORE_SIZE,
    api_key: str | None = None,
    policy_path: Path | None = None,
    policy_live: bool = False,
) -> FastAPI:
    """Build the app.

    The config is loaded **once, here**, not per request. Loading it per request would let the
    versions stamped on two auctions differ mid-process because someone edited a YAML between
    them, and ``caps_version`` exists precisely so that cannot happen silently.

    ``api_key`` is **off by default and that is deliberate**, because the shipped data source
    serves three invented Appendix C patients — there is no patient data here to protect, and
    a demo that demands a credential to return a fixture is friction with nothing behind it.
    The moment a real :class:`~allocation.contracts.DataSource` is wired, that reasoning
    inverts completely and the key stops being optional. Set ``ALLOCATION_API_KEY`` before
    that day, not after it.
    """
    settings = Settings(
        config_dir=config_dir,
        scenario_dir=scenario_dir or Path("scenarios"),
        store_size=store_size,
        api_key_configured=bool(api_key),
        policy_path=policy_path,
        policy_live=policy_live,
    )
    config = service.load(settings)
    # Loaded once, at startup, and for the same reason the config is: so a mismatched encoder
    # or a refused --live-policy stops the process rather than failing every request that asks
    # for it. Weights are immutable; the policy wrapper around them is built per request.
    weights = service.load_policy(settings, config)
    store = RunStore(settings.store_size)

    app = FastAPI(
        title="HOSPILOT allocation",
        description=DESCRIPTION,
        version=config.config_version,
    )
    app.state.settings = settings
    app.state.config = config
    app.state.store = store
    app.state.weights = weights

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"error": exc.message})

    if api_key:

        @app.middleware("http")
        async def _require_key(request: Request, call_next):
            """Constant-time key check on everything but ``/health``.

            ``compare_digest`` rather than ``==``: string equality short-circuits on the first
            differing byte, and the timing difference is enough to recover a key one character
            at a time from a few thousand requests. It costs nothing to not have that bug.
            """
            if request.url.path not in OPEN_PATHS:
                offered = request.headers.get("x-api-key", "")
                if not compare_digest(offered, api_key):
                    return JSONResponse(
                        status_code=401,
                        content={"error": "missing or invalid X-API-Key header"},
                    )
            return await call_next(request)

    # -- discovery ---------------------------------------------------------------------

    @app.get("/health", summary="Liveness and the versions this process is running")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "caps_version": config.caps_version,
            "config_version": config.config_version,
            "runs_in_memory": len(store),
            "live_mode": "refused",
            "auth": "api_key" if api_key else "none",
            "accepts_inline_candidates": bool(api_key),
            "policy": service.policy_json(settings, config, weights),
            # Stated on the one endpoint a deployment always probes: whatever this instance is
            # serving, it is not a hospital. If this ever reads "hasura", the auth line above
            # had better not read "none".
            "data_source": "fixtures",
        }

    @app.get("/config", summary="Versions, unsigned rules, and available scenarios")
    def get_config() -> dict[str, Any]:
        return service.config_json(config, settings)

    @app.get("/use-cases", summary="Registered profiles and a query that resolves to each")
    def use_cases() -> dict[str, Any]:
        return service.use_cases_json()

    @app.get("/scenarios", summary="Scenario names this process will accept")
    def scenarios() -> dict[str, Any]:
        return {
            "directory": str(settings.scenario_dir),
            "scenarios": list(service.scenario_names(settings)),
        }

    # -- running -----------------------------------------------------------------------

    @app.post("/auction", summary="Run one auction and return the full bid ladder")
    def post_auction(body: AuctionBody, format: OutputFormat = "json"):
        run, used, world, policy = service.run_one(
            settings, config, AuctionRequest(**body.model_dump()), weights
        )
        store.put(run, used)

        # Provenance first, then the report, then what the shadow saw — the CLI's own order,
        # and the only one where a reader meets "a learned policy decided this" before the
        # numbers it decided.
        banner = service.policy_banner(settings, used, weights, policy)
        if format in ("text", "steps"):
            return PlainTextResponse(
                banner + service.report_text(run) + service.shadow_text(policy)
            )
        if format == "summary":
            return PlainTextResponse(banner + service.summary_text(run))
        if format == "brief":
            return PlainTextResponse(banner + service.brief_text(run))
        if format == "explain":
            return PlainTextResponse(banner + service.explain_text(run, used))

        payload = service.auction_json(run, world, used, policy)
        if body.explain:
            payload["explain"] = service.explain_text(run, used)
        if body.derivation:
            payload["derivation"] = service.derivation_json(run, used)
        return payload

    @app.post("/session", summary="Run many auctions across shifts and return burn rate")
    def post_session(body: SessionBody, format: OutputFormat = "json"):
        result, used, world, policy = service.run_many(
            settings, config, SessionRequest(**body.model_dump()), weights
        )
        for run in result.runs:
            store.put(run, used)

        # A session has no single winner, so ``summary`` and ``brief`` get the session report
        # too rather than silently falling through to JSON.
        if format in ("text", "steps", "summary", "brief", "explain"):
            return PlainTextResponse(
                service.policy_banner(settings, used, weights, policy)
                + service.session_report_text(result)
                + service.shadow_text(policy)
            )
        return service.session_json(result, world, policy)

    # -- retrieval ---------------------------------------------------------------------

    @app.get("/auctions", summary="Auction ids held in memory, newest first")
    def auctions() -> dict[str, Any]:
        return {"auction_ids": list(store.ids()), "retained": len(store)}

    @app.get("/auction/{auction_id}", summary="A run from this process's memory")
    def get_auction(auction_id: str) -> dict[str, Any]:
        run, used = store.get(auction_id)
        # No policy object: the divergence log lived on the instance that ran, and this run may
        # have been retained for hours. ``policy.names`` still comes off the audit rows, so a
        # retained run says *which* bidder decided it even though it cannot say by how much the
        # shadow disagreed.
        return service.auction_json(run, "retained", used)

    @app.get("/auction/{auction_id}/audit", summary="Every audit row that run produced")
    def get_audit(auction_id: str) -> dict[str, Any]:
        run, _ = store.get(auction_id)
        return service.audit_json(run)

    @app.get(
        "/auction/{auction_id}/explain",
        summary="The full derivation as text, every factor and weight",
        response_class=PlainTextResponse,
    )
    def get_explain(auction_id: str) -> str:
        run, used = store.get(auction_id)
        return service.explain_text(run, used)

    @app.get(
        "/auction/{auction_id}/derivation",
        summary="The same derivation as structured JSON, for a UI or a notebook",
    )
    def get_derivation(auction_id: str) -> dict[str, Any]:
        run, used = store.get(auction_id)
        return service.derivation_json(run, used)

    return app
