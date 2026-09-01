"""Everything the API does that is not HTTP.

Kept free of FastAPI on purpose: the translation from a request to an
:class:`~allocation.trigger.runtime.AllocationRun` and back to JSON is where the mistakes
live, and it is testable here without a client, a port, or an event loop.

**The response carries the bid ladder.** ``cli.to_json`` reports the winner and the closing
numbers; that is the right summary for a terminal and the wrong one for an API, because the
interesting content of an auction is *how the price got there* — who raised, who withdrew,
against which ceiling, and what the guard did to the proposal. A caller who has to re-run the
auction to see round 2 has been handed a result they cannot check.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from allocation.audit.serialise import jsonable
from allocation.auction.guards import (
    safety_is_declared,
    safety_is_enforced,
    safety_posture,
    safety_rules,
)
from allocation.config import Config, load_config
from allocation.contracts import AgentKind, AuctionMode, Candidate, DataSource
from allocation.explain import derivation, explain
from allocation.ingest.fixtures import CANDIDATES, NOW, FixtureDataSource
from allocation.ingest.scenarios import ScenarioError, build_scenario, load_scenario
from allocation.profiles.registry import REGISTRY, ResourceProfile
from allocation.trigger.query import UnknownUseCase, describe, resolve_profile
from allocation.trigger.runtime import AllocationRun, run_allocation
from allocation.trigger.session import (
    SessionResult,
    event_schedule,
    run_session,
    with_rounds,
)

#: The default use-case sentence, identical to the CLI's.
DEFAULT_QUERY = (
    "ER, OT, and ICU/Ward demand compete for one limited ICU bed. RL learns who should "
    "receive the bed while balancing survival, throughput, waiting time, cancellations, "
    "and financial impact."
)

#: Runs retained for ``GET /auction/{id}``. Bounded because this is a convenience cache in a
#: long-lived process, not a log — an unbounded dict here is a memory leak with a nice name.
DEFAULT_STORE_SIZE = 64

#: Upper bound on ``events`` for one ``POST /session``. A session is synchronous and each
#: event is a full auction with a fresh snapshot per round, so an unbounded count is a
#: request that ties up a worker for minutes.
MAX_SESSION_EVENTS = 200

#: Candidates accepted in one request body. Generous against the real limit, which is not a
#: number at all — see :func:`_inline_world`, where one candidate *per agent* is enforced.
MAX_CANDIDATES = 12

#: Clinical rows accepted per candidate, per kind. A trajectory needs a handful of readings;
#: ten thousand is a request that spends a worker's minute in ``timeseries``, not a patient.
MAX_ROWS_PER_CANDIDATE = 500

#: ``policy`` on the two POST routes. Which *bidder* decides, not which world is bid over.
#:
#: ``heuristic`` is the default and stays the default, matching ``run_allocation`` and the CLI:
#: RL_FIXES fix 6 made the learned policy loadable, not preferred. ``rl`` selects the weights
#: **this process was started with** — a request cannot name a file, for the same reason
#: :class:`Settings` pins ``config_dir`` (a path in a JSON body is a file-read primitive for
#: anyone who can reach the port). Whether ``rl`` then *acts* or merely *shadows* is the
#: operator's call at startup, never the caller's.
PolicyChoice = ("heuristic", "rl")

#: Whether a text response repeats the provisional-safety wall above the report. Off by
#: choice, not by oversight: the facts that wall carried are still served structurally —
#: ``safety_status``, ``safety_clinically_approved`` and ``safety_rules`` on ``GET /health``
#: and in every JSON response's ``policy`` block — and ``__main__._announce_policy`` still
#: prints the warning once to stderr at startup, where a log keeps it. This silences only the
#: per-response repeat above the report. Flip to True to restore it.
SHOW_SAFETY_BANNER = False


class ApiError(Exception):
    """A request that cannot be served, with the HTTP status it maps to.

    Carries the status so the route layer never has to re-derive *why* something failed from
    the exception type — the module that knows the reason names the code.
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------------------
# Settings — fixed for the process, never taken from a request body
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    """What the *operator* chose at startup, as opposed to what a caller asks for.

    The split matters. ``--config-dir`` and ``--scenario`` are ordinary CLI flags because a
    person typing them already has a shell and can read any file they like. Accepting either
    as a path in a JSON body would turn that into a file-read primitive for anyone who can
    reach the port, so both are pinned here and requests address scenarios by name.
    """

    config_dir: Path | None = None
    scenario_dir: Path = Path("scenarios")
    store_size: int = DEFAULT_STORE_SIZE
    #: Whether this process was started with an API key. Not the key itself — nothing here
    #: needs to compare it, only to know whether inline patient data may be accepted at all.
    api_key_configured: bool = False
    #: Trained weights this process will serve under ``policy: "rl"``. A path, and therefore
    #: an operator's decision — see :data:`PolicyChoice`.
    policy_path: Path | None = None
    #: Whether those weights may *decide* an allocation. False means ``policy: "rl"`` still
    #: runs, but shadowed: the heuristic allocates and the learned choices are recorded. This
    #: is the CLI's ``--policy`` / ``--live-policy`` split, and the default is the safe one for
    #: the same reason — ``rl/pilot.py`` wants a track record before influence, and a flag that
    #: quietly promoted the policy would make the safe path the one you have to remember.
    policy_live: bool = False


_SCENARIO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def scenario_names(settings: Settings) -> tuple[str, ...]:
    """Scenario files available to this process, by stem."""
    if not settings.scenario_dir.is_dir():
        return ()
    return tuple(sorted(p.stem for p in settings.scenario_dir.glob("*.yaml")))


def resolve_scenario(settings: Settings, name: str) -> Path:
    """Map a scenario *name* to a file inside the configured directory.

    The regex is the whole security control: it admits no ``/``, no ``\\`` and no ``.``, so
    ``../../etc/passwd`` and ``C:\\secrets`` are rejected before a path is ever built. The
    ``resolve``/``is_relative_to`` check afterwards is belt and braces for symlinks.
    """
    if not _SCENARIO_NAME.match(name):
        raise ApiError(
            f"invalid scenario name {name[:64]!r}. Names are file stems from GET /scenarios "
            "— letters, digits, dash and underscore only. Paths are not accepted here: the "
            "server reads scenarios only from its own configured directory."
        )

    root = settings.scenario_dir.resolve()
    path = (root / f"{name}.yaml").resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ApiError(
            f"no scenario named {name!r}. Available: {list(scenario_names(settings))}",
            status=404,
        )
    return path


# ---------------------------------------------------------------------------------------
# Run store
# ---------------------------------------------------------------------------------------


class RunStore:
    """The last N runs, newest evicted last. In memory, and honest about it.

    Exists so ``POST /auction`` followed by ``GET /auction/{id}/explain`` does not have to run
    the auction twice — re-running would also re-read the world, so the second answer would
    not necessarily match the first.
    """

    def __init__(self, size: int = DEFAULT_STORE_SIZE) -> None:
        if size < 1:
            raise ValueError("store size must be at least 1")
        self._size = size
        self._runs: OrderedDict[str, tuple[AllocationRun, Config]] = OrderedDict()

    def put(self, run: AllocationRun, config: Config) -> None:
        key = run.outcome.result.auction_id
        self._runs[key] = (run, config)
        self._runs.move_to_end(key)
        while len(self._runs) > self._size:
            self._runs.popitem(last=False)

    def get(self, auction_id: str) -> tuple[AllocationRun, Config]:
        try:
            entry = self._runs[auction_id]
        except KeyError:
            raise ApiError(
                f"no run {auction_id!r} in memory. The store keeps the last {self._size} runs "
                "and does not survive a restart — there is no auction table yet (F-02).",
                status=404,
            ) from None
        self._runs.move_to_end(auction_id)
        return entry

    def ids(self) -> tuple[str, ...]:
        return tuple(reversed(self._runs))

    def __len__(self) -> int:
        return len(self._runs)


# ---------------------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuctionRequest:
    """One auction, as asked for over HTTP.

    ``hospital`` and ``candidates`` are the difference between asking the engine to
    demonstrate itself and asking it to decide something: without them the same three
    Appendix C patients bid every time, and ``query`` only picks which *resource* is at stake.
    """

    query: str = DEFAULT_QUERY
    mode: str = AuctionMode.SIMULATION.value
    at: str | None = None
    scenario: str | None = None
    hospital: Mapping[str, Any] | None = None
    candidates: Sequence[Mapping[str, Any]] | None = None
    rounds: int | None = None
    uplift: bool = True
    #: Which bidder decides. See :data:`PolicyChoice` — the caller picks *whether* to use the
    #: learned weights, the operator picked *which* weights and whether they may act. ``None``
    #: means neither was asked for, so :func:`default_policy` resolves it from what the process
    #: actually loaded.
    policy: str | None = None
    #: Presentation only — neither flag changes the auction, both change what is returned.
    explain: bool = False
    derivation: bool = False


@dataclass(frozen=True, slots=True)
class SessionRequest:
    """Many auctions across shifts, carrying one ledger."""

    query: str = DEFAULT_QUERY
    at: str | None = None
    scenario: str | None = None
    hospital: Mapping[str, Any] | None = None
    candidates: Sequence[Mapping[str, Any]] | None = None
    rounds: int | None = None
    uplift: bool = True
    policy: str | None = None
    events: int = 8
    every: str = "45m"


_DURATION = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([mhd])\s*", re.IGNORECASE)
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(text: str) -> timedelta:
    """``45m``, ``2h``, ``1d`` — the CLI's ``--every`` grammar, unchanged."""
    match = _DURATION.fullmatch(text)
    if not match:
        raise ApiError(f"cannot read duration {text!r}; use forms like 45m, 2h, 1d")
    amount, unit = match.groups()
    return timedelta(**{_UNITS[unit.lower()]: float(amount)})


def parse_moment(value: str | None) -> datetime:
    """The clock to run against.

    Defaults to the Appendix C fixture clock rather than the wall clock, and that is not
    laziness: the shipped data source serves vitals timestamped around 13:00 on a fixed day.
    Running it at ``datetime.now()`` would read every one of them as hours or years stale,
    collapse coverage, and return a technically valid allocation computed from almost no
    signal. A real ``DataSource`` makes the wall clock the right default; a fixture does not.
    """
    if value is None:
        return NOW
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(f"cannot read timestamp {value!r}: {exc}") from exc


def check_mode(mode: str) -> AuctionMode:
    """Validate the mode and refuse ``live`` outright."""
    try:
        parsed = AuctionMode(mode)
    except ValueError:
        raise ApiError(
            f"unknown mode {mode!r}; one of {[m.value for m in AuctionMode]}"
        ) from None

    if parsed.is_binding:
        # Identical refusal to cli.py, for an identical reason. See the package docstring.
        raise ApiError(
            "mode 'live' is refused over HTTP. The shipped data source serves three invented "
            "Appendix C patients, so a live auction would hold a real bed for people who do "
            "not exist. Wire a real DataSource and go through the CLI one auction at a time.",
            status=403,
        )
    return parsed


def load(settings: Settings) -> Config:
    """Load the process's configuration, mapping a bad ``--config-dir`` to a 500."""
    try:
        return load_config(settings.config_dir)
    except (OSError, KeyError, ValueError) as exc:
        raise ApiError(
            f"cannot load config from {settings.config_dir}: {exc}", status=500
        ) from exc


def _with_uplift(config: Config, enabled: bool) -> Config:
    """``uplift: false`` reverts to D.9's ``Ceiling = U``, as ``--no-uplift`` does."""
    if enabled:
        return config
    rules = {**config.rules, "uplift": {**config.rules["uplift"], "enabled": False}}
    return replace(config, rules=rules)


# ---------------------------------------------------------------------------------------
# Learned policy
#
# The CLI got this in RL_FIXES fix 6 and the HTTP layer did not, so ``POST /auction`` served
# the heuristic no matter what the process had been started with. The split below mirrors
# ``cli.build_policy`` deliberately rather than reimplementing the decision: same shadow
# default, same safety gate, same refusal to load weights fitted under another encoder.
#
# Two differences, both forced by the fact that this is a server and not a terminal:
#
# * **The safety checks run once, at startup, not per request.** A process that may not serve
#   an acting policy should fail to start, not accept the flag and then 403 every caller who
#   asks for it. :func:`load_policy` raises and ``__main__`` reports it.
# * **The policy object is built per request, never shared.** It has to be: ``uplift: false``
#   produces a different ``Config`` per request and the policy closes over one. That it also
#   sidesteps a mutable ``DivergenceMonitor`` shared across FastAPI's threadpool — the bug
#   PRODUCTION_CHECKLIST flags against ``RunStore`` — is a second reason, not the first.
# ---------------------------------------------------------------------------------------


def load_policy(settings: Settings, config: Config):
    """Read the weights this process serves, or ``None`` if it was started without any.

    Every refusal that can be decided from configuration alone is decided here, so a request
    never has to be told "this server could have done that if it had been started differently".
    """
    if settings.policy_path is None:
        if settings.policy_live:
            raise ApiError("--live-policy requires --policy", status=500)
        return None

    from allocation.rl.policy import QWeights

    if settings.policy_live and not safety_is_enforced(config):
        # BUILD_SPEC F-13, and the same wording the CLI refuses with. Nothing structurally
        # prevents a learned policy abandoning a critical patient while no hard constraint is
        # in force; shadowing under that is fine, acting under it is not a pilot.
        raise ApiError(
            "refusing --live-policy: auction.yaml declares safety_status "
            f"{safety_posture(config)!r} with "
            f"{len(config.auction.get('safety_constraints') or ())} constraints. A learned "
            "policy may not decide allocations while no hard constraint is enforced. Start "
            "without --live-policy to shadow it instead.",
            status=500,
        )

    try:
        return QWeights.load(settings.policy_path)
    except (OSError, KeyError, ValueError) as exc:
        raise ApiError(
            f"cannot load policy from {settings.policy_path}: {exc}", status=500
        ) from exc


def default_policy(weights) -> str:
    """What ``policy`` means when a request does not say.

    A process started with ``--policy`` was started *in order to* serve those weights, so
    ``rl`` is the honest default there and ``heuristic`` is the honest default without them.
    The alternative — defaulting to ``heuristic`` always — is what made an operator who had
    loaded weights read a heuristic ladder and believe it was the learned one.

    Resolving here rather than defaulting the field to ``"rl"`` keeps a weightless process
    useful: that server answers with the heuristic instead of 409-ing every request. Nothing
    is hidden by it — ``policy.names`` on every response and the banner above every text
    rendering name the bidder that actually decided, whichever way this resolved.
    """
    return "rl" if weights is not None else "heuristic"


def _policy_for(settings: Settings, config: Config, weights, choice: str | None):
    """The bidder for one request. ``None`` means the heuristic, as ``run_allocation`` defaults.

    Returns the policy object so the caller can read its divergence log afterwards — a
    shadowed run's whole output is what the learned policy *would* have done, and on a server
    that has to reach the response body rather than stderr.
    """
    if choice is None:
        choice = default_policy(weights)
    if choice not in PolicyChoice:
        raise ApiError(f"unknown policy {choice!r}; expected one of {list(PolicyChoice)}")
    if choice == "heuristic":
        return None
    if weights is None:
        raise ApiError(
            "this process was started without --policy, so 'rl' has no weights to run. "
            "GET /health reports what is loaded.",
            status=409,
        )

    from allocation.policy.heuristic import HeuristicPolicy
    from allocation.rl.pilot import DivergenceMonitor, GatedPolicy, SafetyGate, ShadowPolicy
    from allocation.rl.policy import LinearQPolicy

    learned = LinearQPolicy(config, weights)
    gate = SafetyGate(config)
    if settings.policy_live:
        return GatedPolicy(learned, gate=gate, fallback=HeuristicPolicy(config))
    return ShadowPolicy(
        HeuristicPolicy(config), learned, gate=gate, monitor=DivergenceMonitor()
    )


def policy_json(settings: Settings, config: Config, weights) -> dict[str, Any]:
    """What this process will do if asked for ``policy: "rl"``.

    On ``/health`` and ``/config`` because it is the one thing about a served auction a caller
    cannot infer from the result: two processes with identical versions and identical fixtures
    return different bid ladders depending on how they were started.
    """
    if weights is None:
        return {
            "loaded": False,
            "acting": False,
            "default_for_requests": default_policy(weights),
            "note": "started without --policy",
        }
    return {
        "loaded": True,
        "path": str(settings.policy_path),
        # What a request that omits ``policy`` will get. Reported because it is now a property
        # of how the process was started, not a constant a caller can read off the schema.
        "default_for_requests": default_policy(weights),
        "policy_version": weights.policy_version,
        "encoder_version": weights.encoder_version,
        "fabrication_version": weights.fabrication_version,
        "acting": settings.policy_live,
        "mode": "gated" if settings.policy_live else "shadow",
        "safety_status": safety_posture(config),
        "safety_clinically_approved": safety_is_declared(config),
        "safety_rules": [c["id"] for c in safety_rules(config)],
        # C-5 rides along with the artefact, because a caller reading policy_version has no
        # other way to learn that the objective behind it was never fitted.
        "note": (
            "the reward these weights optimise is unfitted and no_mortality has no source "
            "(F-01, RL_FIXES C-5); this is not a deployment recommendation"
        ),
    }


def _policy_report(policy) -> dict[str, Any] | None:
    """The divergence log, for a shadowed run. ``None`` when nothing was shadowed.

    ``cli._report_shadow`` prints this to stderr to keep ``--json`` parseable. Over HTTP there
    is no second stream, so it goes in the body — and a shadowed run whose divergence the
    caller cannot see is a run that recorded nothing they can act on.
    """
    monitor = getattr(policy, "monitor", None)
    blocked = list(getattr(policy, "blocked", ()) or ())
    if monitor is None:
        return {"gate_refusals": blocked} if blocked else None
    return {
        "decisions_observed": monitor.observed,
        "divergence_rate": monitor.rate,
        "recent_rate": monitor.recent_rate,
        "breaker_threshold": monitor.threshold,
        "breaker_tripped": monitor.tripped,
        "disagreements": dict(monitor.disagreements),
        "gate_refusals": blocked,
    }


def _inline_world(
    settings: Settings,
    hospital: Mapping[str, Any] | None,
    candidates: Sequence[Mapping[str, Any]],
    now: datetime,
) -> tuple[DataSource, Sequence[Candidate], str]:
    """Build the world from patients supplied in the request body.

    **This is the endpoint that turns the API into a service rather than a demo**, and it is
    also the one that changes what the deployment is holding. A body carrying vitals, labs and
    drug orders is patient data; the process cannot tell a real trajectory from an invented
    one, so it does not try — it requires that an operator has at least decided who may call
    before it will accept any.
    """
    if not settings.api_key_configured:
        raise ApiError(
            "inline candidates carry patient data, so this route is closed unless the "
            "process was started with ALLOCATION_API_KEY set. Use 'scenario' for fixtures, "
            "or configure a key. (Set TLS termination in front of it too — a key sent over "
            "plaintext HTTP is a key that has been published.)",
            status=403,
        )

    if len(candidates) > MAX_CANDIDATES:
        raise ApiError(f"{len(candidates)} candidates exceeds the limit of {MAX_CANDIDATES}")

    for index, raw in enumerate(candidates):
        for key in ("vitals", "labs", "orders"):
            rows = raw.get(key) or ()
            if len(rows) > MAX_ROWS_PER_CANDIDATE:
                raise ApiError(
                    f"candidates[{index}].{key} has {len(rows)} rows; the limit is "
                    f"{MAX_ROWS_PER_CANDIDATE}"
                )

    # One candidate per agent, and this has to be said out loud. ``run_auction`` builds
    # ``{c.agent: c}`` and ``initial_positions`` keys positions by agent, so a second ER
    # patient does not lose the auction — it is *silently overwritten* and never bids. The
    # mechanism is department against department, one patient each (RL-Steps sections 9-18);
    # anything else needs a different auction, not a longer list.
    seen: dict[str, int] = {}
    for index, raw in enumerate(candidates):
        agent = str(raw.get("agent", "")).strip().lower()
        if agent in seen:
            raise ApiError(
                f"candidates[{index}] and candidates[{seen[agent]}] are both '{agent}'. "
                "The auction runs one candidate per agent — a second would be dropped rather "
                "than outbid. Run one auction per patient, or send each department's single "
                "strongest claim."
            )
        seen[agent] = index

    body = {"hospital": hospital or {}, "candidates": list(candidates)}
    try:
        source, parsed, description = build_scenario(
            body, now, where="request body", default_description="inline candidates"
        )
    except ScenarioError as exc:
        raise ApiError(str(exc)) from exc
    return source, parsed, description


def _world(
    settings: Settings,
    scenario: str | None,
    hospital: Mapping[str, Any] | None,
    candidates: Sequence[Mapping[str, Any]] | None,
    now: datetime,
) -> tuple[DataSource, Sequence[Candidate], str]:
    """The data source and bidders for a request.

    Three sources, in order of specificity: patients in the body, a named scenario on the
    server, or the Appendix C fixture. The first two are mutually exclusive — a request that
    supplied both would get one of them silently, and which one is not something a caller
    should have to learn from the output.
    """
    if candidates and scenario is not None:
        raise ApiError(
            "'candidates' and 'scenario' both describe the world; send one. Candidates come "
            "from the request, a scenario from the server's own directory."
        )
    if candidates:
        return _inline_world(settings, hospital, candidates, now)
    if hospital is not None:
        raise ApiError(
            "'hospital' was sent without 'candidates'. A hospital state with no bidders "
            "describes no auction; send the patients too, or omit both for the fixture."
        )
    if scenario is None:
        return FixtureDataSource(), CANDIDATES, "Appendix C fixture"

    path = resolve_scenario(settings, scenario)
    try:
        source, parsed, description = load_scenario(path, now)
    except ScenarioError as exc:
        raise ApiError(f"cannot load scenario {scenario!r}: {exc}") from exc
    return source, parsed, description


def _profile(query: str, rounds: int | None) -> ResourceProfile:
    try:
        profile = resolve_profile(query)
    except UnknownUseCase as exc:
        raise ApiError(str(exc)) from exc
    if rounds is None:
        return profile
    try:
        return with_rounds(profile, rounds)
    except ValueError as exc:
        raise ApiError(str(exc)) from exc


def run_one(
    settings: Settings, config: Config, request: AuctionRequest, weights=None
) -> tuple[AllocationRun, Config, str, Any]:
    """Serve ``POST /auction``.

    Returns the run, the config it ran under, the world, and the policy object — the last so
    the route can read a shadowed run's divergence, which exists only on that instance.
    """
    mode = check_mode(request.mode)
    now = parse_moment(request.at)
    config = _with_uplift(config, request.uplift)
    source, candidates, world = _world(
        settings, request.scenario, request.hospital, request.candidates, now
    )
    profile = _profile(request.query, request.rounds)
    # After ``_with_uplift``: the policy scores against the ceilings it will actually bid into,
    # so a run with uplift disabled must not hand it a policy built on the enabled config.
    policy = _policy_for(settings, config, weights, request.policy)

    try:
        run = run_allocation(
            config=config,
            source=source,
            candidates=candidates,
            now=now,
            query=request.query,
            profile=profile,
            mode=mode,
            policy=policy,
        )
    except ValueError as exc:
        # No eligible bidders is the common one: a scenario whose candidates are all from an
        # agent this profile does not admit.
        raise ApiError(str(exc)) from exc
    return run, config, world, policy


def run_many(
    settings: Settings, config: Config, request: SessionRequest, weights=None
) -> tuple[SessionResult, Config, str, Any]:
    """Serve ``POST /session``. Returns as :func:`run_one` does — result, config, world, policy.

    The config is returned rather than assumed because ``uplift: false`` runs under a copy, and
    the route retains what it is handed: a session stored under the process config would render
    ``/auction/{id}/explain`` with the uplift setting the run did not use.
    """
    if request.events < 1:
        raise ApiError("a session needs at least one event")
    if request.events > MAX_SESSION_EVENTS:
        raise ApiError(
            f"{request.events} events exceeds the limit of {MAX_SESSION_EVENTS}; a session "
            "runs synchronously and every event is a full auction"
        )

    now = parse_moment(request.at)
    config = _with_uplift(config, request.uplift)
    source, candidates, world = _world(
        settings, request.scenario, request.hospital, request.candidates, now
    )
    profile = _profile(request.query, request.rounds)
    events = event_schedule(now, request.events, parse_duration(request.every))
    policy = _policy_for(settings, config, weights, request.policy)

    try:
        result = run_session(
            config=config,
            source=source,
            candidates=candidates,
            start=now,
            events=events,
            profile=profile,
            query=request.query,
            mode=AuctionMode.SIMULATION,
            policy=policy,
        )
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    return result, config, world, policy


# ---------------------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------------------


def _policy_names(run: AllocationRun) -> list[str]:
    """Which bidder produced this run, read back off the audit rows.

    Off the rows rather than off the request, because that is the record that survives into
    :class:`RunStore` and out through ``/auction/{id}`` — a retained run has no request left
    to consult. ``audit/validate.py`` refuses a bid with an empty ``policy_name``, so this is
    never guesswork: every row names the policy that decided it.
    """
    return sorted({bid.policy_name for bid in run.bundle.bids if bid.policy_name})


def auction_json(
    run: AllocationRun, world: str, config: Config, policy: Any = None
) -> dict[str, Any]:
    """One run, in full. The bid ladder is the point — see the module docstring.

    ``config`` is the one the run executed under, which is not always the process's: ``uplift:
    false`` builds a copy. :class:`RunStore` retains it alongside the run for exactly this.
    """
    result = run.outcome.result
    return {
        "auction_id": result.auction_id,
        "auction_key": result.auction_key,
        "query": run.query,
        "world": world,
        # Which bidder decided, stated on every response. Without it two processes serving
        # identical versions over identical fixtures return different ladders and nothing in
        # the body says why — the gap that made ``--json`` unable to prove an RL run happened.
        "policy": {
            "names": _policy_names(run),
            "shadow": _policy_report(policy) if policy is not None else None,
        },
        "resource": {
            "type": result.resource_type.value,
            "id": result.resource_id,
            "bidders": [a.value for a in run.profile.eligible_agents],
        },
        "mode": result.mode.value,
        "binding": run.binding,
        "opened_at": result.opened_at.isoformat(),
        "closed_at": result.closed_at.isoformat(),
        "outcome": result.outcome,
        "winner": result.winner.value if result.winner else None,
        "winning_candidate_id": result.winning_candidate_id,
        "winning_bid": result.winning_bid,
        "reserve_price": result.reserve_price,
        "contention": result.contention,
        "rounds_run": result.rounds_run,
        "max_rounds": result.max_rounds,
        "utilities": {cid: b.total for cid, b in run.utilities.items()},
        "ceilings": dict(run.ceilings),
        "rounds": [
            {
                "round_index": state.round_index,
                "opened_at": state.opened_at.isoformat(),
                "active_at_close": sorted(a.value for a in state.active_agents),
                "bids": [
                    {
                        "agent": bid.agent.value,
                        "candidate_id": bid.candidate_id,
                        "action": bid.action.value,
                        "amount": bid.amount,
                        "utility": bid.utility,
                        "ceiling": bid.ceiling,
                        "alpha": bid.alpha,
                    }
                    for bid in state.bids
                ],
            }
            for state in result.rounds
        ],
        "positions": {
            agent.value: {
                "candidate_id": position.candidate_id,
                "final_bid": position.current_bid,
                "utility": position.utility,
                "ceiling": position.ceiling,
                "active": position.active,
                "exit_round": position.exit_round,
                "exit_reason": position.exit_reason or None,
            }
            for agent, position in result.positions.items()
        },
        "budgets": {
            agent.value: {
                "opened": run.opening_budgets[agent].budget_total,
                "total": state.budget_total,
                "remaining": state.budget_remaining,
                "spent": state.spent,
                "burn_rate": state.burn_rate,
            }
            for agent, state in run.outcome.budgets.items()
        },
        "spends": {
            agent.value: {
                "bid": spend.bid,
                "contention": spend.contention,
                "outcome_factor": spend.outcome_factor,
                "commitment_rate": spend.commitment_rate,
                "cost": spend.cost,
                "won": spend.won,
            }
            for agent, spend in run.outcome.spends.items()
        },
        "shift": {
            "id": run.shift.shift_id,
            "label": run.shift.label,
            "start": run.shift.start.isoformat(),
            "end": run.shift.end.isoformat(),
        },
        "reward": {
            "due_at": run.pending.due_at.isoformat(),
            "horizon_hours": run.pending.horizon_hours,
            # Stated on every response rather than left to the reader: an auction whose reward
            # cannot be observed is not training data, and F-01 means that is every auction.
            "trainable": False,
            "note": "no mortality column exists (F-01), so no episode can complete",
        },
        "governance": {
            "caps_version": result.caps_version,
            "config_version": result.config_version,
            "unsigned_rules": dict(result.unsigned_rules),
            # Was hardcoded False, which happened to be true while nothing read it. It is a
            # governance claim on a response that can now report a learned policy deciding the
            # allocation, so it reads the config the way ``/config`` already does.
            "safety_constraints_declared": safety_is_declared(config),
        },
        "audit": {
            "row_count": run.bundle.row_count,
            "bids": len(run.bundle.bids),
            "budgets": len(run.bundle.budgets),
            "snapshots": len(run.bundle.snapshots),
            "persisted": False,
            "note": "allocation.* tables do not exist yet (F-02); rows are built and dropped",
        },
        "trace": [
            {
                "key": step.key,
                "section": step.section,
                "title": step.title,
                "rows": [list(row) for row in step.rows],
                "notes": list(step.notes),
            }
            for step in run.trace
        ],
    }


def session_json(result: SessionResult, world: str, policy: Any = None) -> dict[str, Any]:
    """A session: per-shift burn, win share, and one line per auction.

    The divergence log matters more here than on a single auction: one shadowed auction
    observes a handful of decisions, and ``DivergenceMonitor.tripped`` needs thirty before it
    will report at all. A session is the shortest request that can actually trip the breaker.
    """
    return {
        "world": world,
        "auctions": len(result.runs),
        "policy": {
            "names": sorted({n for run in result.runs for n in _policy_names(run)}),
            "shadow": _policy_report(policy) if policy is not None else None,
        },
        "shifts": [
            {
                "shift_id": report.shift.shift_id,
                "label": report.shift.label,
                "start": report.shift.start.isoformat(),
                "end": report.shift.end.isoformat(),
                "auctions": report.auctions,
                "healthy": report.healthy,
                "agents": {
                    agent.value: {
                        "opened": report.opened[agent],
                        "spent": report.spent[agent],
                        "recovered": report.recovered[agent],
                        "closed": report.closed[agent],
                        "wins": report.wins[agent],
                        "burn_rate": report.burn_rate[agent],
                        "band": report.band[agent],
                    }
                    for agent in report.burn_rate
                },
                "exhausted": [a.value for a in report.exhausted],
            }
            for report in result.shifts
        ],
        "wins": {a.value: n for a, n in result.wins.items()},
        "win_share": {a.value: s for a, s in result.win_share.items()},
        "unallocated": result.unallocated,
        "bases": {
            agent.value: {
                "base": base.base,
                "n_win": base.n_win,
                "n_req": base.n_req,
                "expected_bid": base.expected_bid,
                "cost_per_win": base.cost_per_win,
                "source": base.source,
            }
            for agent, base in result.bases.items()
        },
        "runs": [
            {
                "auction_id": run.outcome.result.auction_id,
                "opened_at": run.outcome.result.opened_at.isoformat(),
                "winner": run.winner.value if run.winner else None,
                "winning_bid": run.outcome.result.winning_bid,
                "reserve_price": run.outcome.result.reserve_price,
                "outcome": run.outcome.result.outcome,
                "rounds_run": run.outcome.result.rounds_run,
            }
            for run in result.runs
        ],
    }


def audit_json(run: AllocationRun) -> dict[str, Any]:
    """The full audit bundle — every row that migration 091 would persist."""
    return {
        "auction_id": run.bundle.auction_id,
        "row_count": run.bundle.row_count,
        "persisted": False,
        "rows": jsonable(run.bundle),
    }


def summary_text(run: AllocationRun) -> str:
    """Who won and why, in a screenful. ``?format=summary``.

    ``format=text`` answers "show me the auction"; this answers the only question most
    callers actually have. Every line is read off the closed result — nothing is recomputed —
    so the summary cannot disagree with the JSON it abbreviates. The *why* is the exit
    reasons: an auction is won by being the last bidder standing above the reserve, and each
    rival's position records exactly why it stopped.
    """
    result = run.outcome.result
    positions = result.positions

    def drivers(candidate_id: str) -> str:
        breakdown = run.utilities.get(candidate_id)
        if breakdown is None:
            return ""
        strongest = sorted(
            (r for r in breakdown.components if r.points > 0), key=lambda r: -r.points
        )[:3]
        return ", ".join(f"{r.component.value} {r.points:.1f}" for r in strongest)

    def fate(position) -> str:
        if position.exit_reason:
            return f"out in round {position.exit_round + 1}: {position.exit_reason}"
        return "active at close, outbid"

    lines: list[str] = []

    if result.winner is not None:
        winning = positions[result.winner]
        held = "" if result.mode.is_binding else "  (simulation — nothing is held)"
        lines.append(
            f"winner  {result.winner.value} ({winning.candidate_id}) at "
            f"{result.winning_bid:.1f} after {result.rounds_run} rounds{held}"
        )
        lines.append("why:")
        lines.append(
            f"  {result.winner.value:<5} utility {winning.utility:.1f}, ceiling "
            f"{winning.ceiling:.1f} — driven by {drivers(winning.candidate_id)}"
        )
        losers = sorted(
            (p for agent, p in positions.items() if agent is not result.winner),
            key=lambda p: -p.ceiling,
        )
        for position in losers:
            lines.append(
                f"  {position.agent.value:<5} utility {position.utility:.1f}, ceiling "
                f"{position.ceiling:.1f} — {fate(position)}"
            )
        lines.append(
            f"  the winning bid cleared the reserve price of {result.reserve_price:.1f}"
        )
        return "\n".join(lines) + "\n"

    standing = [p for p in positions.values() if p.active and p.current_bid > 0]
    if standing:
        best = max(standing, key=lambda p: p.current_bid)
        lines.append(
            f"winner  none — best standing bid {best.current_bid:.1f} "
            f"({best.agent.value}) did not clear the reserve price "
            f"of {result.reserve_price:.1f}"
        )
    else:
        lines.append(f"winner  none — every bidder withdrew ({result.outcome})")
    lines.append("why:")
    for position in sorted(positions.values(), key=lambda p: -p.ceiling):
        lines.append(
            f"  {position.agent.value:<5} utility {position.utility:.1f}, ceiling "
            f"{position.ceiling:.1f} — {fate(position)}"
        )
    lines.append("  the resource falls to the fallback pathway (manual allocation)")
    return "\n".join(lines) + "\n"


def brief_text(run: AllocationRun) -> str:
    """Five lines: what was asked, what it resolved to, who got it, and the ranking.

    ``?format=brief``. Shorter than ``summary`` and for a different reader: ``summary``
    explains *why* each rival stopped bidding, which is what a person auditing an allocation
    wants. This is for a caller with only ``curl`` — no ``jq``, no scripting language — who
    needs the decision on stdout and nothing else. Everything is read off the closed result;
    nothing is recomputed, so it cannot disagree with the JSON it abbreviates.

    Utilities are ranked descending because the ranking is the claim this engine can defend.
    The magnitudes rest on unfitted caps (B.13), and five of the six caps tables are still
    copies of ICU's — so "er above ward" is a finding and "er by 59 points" is not. That
    caveat is not printed here; it travels in ``governance.unsigned_rules`` on the JSON
    response and on ``GET /config``, which is where a machine reader can act on it.
    """
    result = run.outcome.result
    winner = result.winner

    if winner is None:
        who = f"none — {result.outcome}"
    else:
        bid = result.winning_bid or 0.0
        cleared = "cleared" if bid >= result.reserve_price else "BELOW RESERVE"
        who = (
            f"{winner.value} ({result.winning_candidate_id})  bid {bid:.1f}   "
            f"reserve {result.reserve_price:.1f}   {cleared}"
        )

    ranked = " | ".join(
        f"{_agent_for(run, cid).value} {breakdown.total:.1f}"
        for cid, breakdown in sorted(
            run.utilities.items(), key=lambda kv: -kv[1].total
        )
    )

    return (
        f"query     : {run.query}\n"
        f"resource  : {run.profile.resource_type.value}\n"
        f"outcome   : {result.outcome}\n"
        f"winner    : {who}\n"
        f"utilities : {ranked}\n"
    )


def _agent_for(run: AllocationRun, candidate_id: str) -> AgentKind:
    """The department that bid this candidate.

    Read from the snapshot rather than parsed out of the candidate id: ``ER-Patient-A``
    happens to start with its agent, but nothing guarantees a caller's own ids do.
    """
    return run.snapshot.patients[candidate_id].candidate.agent


def policy_banner(settings: Settings, config: Config, weights, policy) -> str:
    """The header ``cli.main`` prints above a report when a learned policy is involved.

    In the CLI this goes to stderr, to keep ``--json`` parseable. A text response has no second
    stream, and dropping it would make the API's report the *only* rendering of an auction that
    does not say a learned policy decided it — which is the one line a reader most needs. So it
    is prepended to the body instead, and the two surfaces print the same thing again.
    """
    if policy is None or weights is None:
        return ""

    from allocation.cli import WIDTH

    lines: list[str] = []
    if SHOW_SAFETY_BANNER and settings.policy_live and not safety_is_declared(config):
        rules = safety_rules(config)
        lines += [
            "!" * WIDTH,
            f"PROVISIONAL SAFETY RULES — {len(rules)} in force, NONE clinically approved.",
            "  A learned policy is deciding allocations behind constraints written by",
            f"  engineering. auction.yaml safety_status is {safety_posture(config)!r}, not "
            "'signed_off'.",
        ]
        for constraint in rules:
            threshold = constraint.get("threshold")
            suffix = f"  (threshold {threshold})" if threshold is not None else ""
            lines.append(f"    - {constraint['id']}{suffix}")
        lines.append("!" * WIDTH)

    lines += [
        f"policy   {getattr(policy, 'name', 'unknown')}",
        f"         encoder {weights.encoder_version}, "
        f"fabrication {weights.fabrication_version}",
        "         ACTING — the learned policy decides this allocation"
        if settings.policy_live
        else "         shadowing — the heuristic decides, learned choices are recorded only",
    ]
    return "\n".join(lines) + "\n"


def report_text(run: AllocationRun) -> str:
    """The step-by-step report, identical to what ``python -m allocation`` prints.

    Imported from the CLI rather than reimplemented. A terminal is the most common client
    curl has, and the alternative — a caller piping 40 kB of one-line JSON through a JSON
    formatter to find out who won — is a worse answer than the one the CLI already gives.
    """
    from allocation.cli import render

    return render(run)


def shadow_text(policy) -> str:
    """``cli._report_shadow``'s block, for a text response. Empty unless something shadowed."""
    monitor = getattr(policy, "monitor", None)
    blocked = list(getattr(policy, "blocked", ()) or ())
    if monitor is None:
        return f"\ngate refusals  {len(blocked)}\n" if blocked else ""

    from allocation.cli import WIDTH

    lines = [
        "",
        "=" * WIDTH,
        "SHADOW — what the learned policy would have done",
        "=" * WIDTH,
        monitor.report(),
    ]
    if blocked:
        lines.append(f"\ngate refusals  {len(blocked)}")
        lines += [f"  {rule}" for rule in dict.fromkeys(blocked)]
    return "\n".join(lines) + "\n"


def session_report_text(result: SessionResult) -> str:
    from allocation.cli import render_session

    return render_session(result)


def explain_text(run: AllocationRun, config: Config) -> str:
    return explain(run, config)


def derivation_json(run: AllocationRun, config: Config) -> dict[str, Any]:
    """The ``--explain`` report as data rather than as a wall of text.

    The text version is for a person reading a terminal. This is the same content for a
    front-end that wants to render "Urgency scored 23.7 because NEWS2 was 0.75 at weight
    0.35" as a table, or for a notebook fitting caps against contested cases (B.13) — which
    needs per-factor values, not a formatted page it would have to parse back.
    """
    return derivation(run, config)


def config_json(config: Config, settings: Settings) -> dict[str, Any]:
    """Versions and, more importantly, everything that is not signed off."""
    return {
        "caps_version": config.caps_version,
        "config_version": config.config_version,
        "config_dir": str(settings.config_dir) if settings.config_dir else "built-in",
        "components": len(config.caps["components"]),
        "rule_tables": sorted(config.rules),
        "unsigned_rules": dict(config.unsigned),
        "safety_constraints_declared": safety_is_declared(config),
        "uplift_enabled_by_default": bool(config.rules["uplift"].get("enabled", False)),
        "commitment_rate": float(config.budget["spend"]["commitment_rate"]),
        "scenarios": list(scenario_names(settings)),
    }


def use_cases_json() -> dict[str, Any]:
    """Every registered profile and a query that resolves to it.

    Served because ``resolve_profile`` has no default by design — an unrecognised sentence is
    a 400, and a caller needs to be able to discover what phrasing works without reading the
    matcher.
    """
    profiles = []
    for profile in REGISTRY.all():
        matcher = profile.matcher
        example = (
            f"{matcher.qualifiers[0]} {matcher.nouns[0]}"
            if matcher.qualifiers and matcher.nouns
            else profile.resource_type.value
        )
        profiles.append(
            {
                "resource_type": profile.resource_type.value,
                "description": profile.description,
                "example_query": example,
                "detail": dict(describe(profile)),
            }
        )
    return {"profiles": profiles, "default_query": DEFAULT_QUERY}


def agents_of(run: AllocationRun) -> tuple[AgentKind, ...]:
    return tuple(run.outcome.result.positions)
