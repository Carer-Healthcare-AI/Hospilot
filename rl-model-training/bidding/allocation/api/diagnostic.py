"""Service functions behind the ``/diagnostic/*`` routes.

The same relationship ``api.service`` has to the bed CLI: every number here comes from the
functions ``python -m allocation.use_cases.diagnostic_machine`` calls, and this module adds
serialisation and nothing else. No scoring, no policy selection, no defaulting of a clinical
value lives here.

**Why diagnostic gets its own routes rather than a resource type on ``POST /auction``.** The
bed route resolves a query to a :class:`~allocation.profiles.registry.ResourceProfile` and
auctions one bed. A diagnostic auction allocates a *capacity interval* on a machine, against
a deadline, with hard eligibility on modality and capability — there is no bed-shaped request
body that describes it, and pretending otherwise would mean a caller sending `unit_total_beds`
to book a CT scanner. The two families keep separate surfaces for the same reason they keep
separate caps tables.

**Three ways to name the world**, in the order the route tries them:

``scenario``   a named deterministic fixture (``GET /diagnostic/scenarios``). Needs no patient
               data and is the path a demo or a smoke check should use.
``query``      natural language, resolved by :mod:`.query`. Refuses rather than inventing a
               clinical value when the sentence does not carry one — a refusal is a 422 with
               the evidence it did find and what was missing.
``machines`` + ``requests``
               real state, supplied inline. This carries patient data and therefore requires
               the API key, exactly as ``candidates`` does on the bed route.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from allocation.config import Config
from allocation.contracts import AgentKind, AuctionMode
from allocation.use_cases.diagnostic_machine.auction import run_diagnostic_auction
from allocation.use_cases.diagnostic_machine.budgets import (
    REGIMES,
    diagnostic_shift,
    open_diagnostic_budgets,
)
from allocation.use_cases.diagnostic_machine.config import (
    for_modality,
    rules_version,
    unsigned_diagnostic_rules,
)
from allocation.use_cases.diagnostic_machine.contracts import (
    AllocationInterval,
    DiagnosticMachineState,
    DiagnosticModality,
    DiagnosticRequest,
    MachineStatus,
)
from allocation.use_cases.diagnostic_machine.evaluate import run_policy
from allocation.use_cases.diagnostic_machine.explain import as_data
from allocation.use_cases.diagnostic_machine.profiles import MODALITIES
from allocation.use_cases.diagnostic_machine.query import resolve
from allocation.use_cases.diagnostic_machine.scenarios import SCENARIOS, all_scenarios

#: The epoch the deterministic scenarios are anchored to. Their arrival times and deadlines are
#: offsets from it, so a run that used the wall clock would report deadlines already missed.
from allocation.use_cases.diagnostic_machine.evaluate import EPOCH


class DiagnosticRefused(ValueError):
    """The request named a world the engine will not invent the missing half of."""

    def __init__(self, message: str, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = dict(detail or {})


# ---------------------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------------------


def modalities_json() -> dict[str, Any]:
    """``GET /diagnostic/modalities``. What can be auctioned, and under which tables."""
    return {
        "modalities": [
            {
                "modality": profile.modality.value,
                "caps_config": profile.caps_config,
                "budget_config": profile.budget_config,
                "eligible_agents": [a.value for a in profile.eligible_agents],
                "capabilities": sorted(profile.capabilities or ()),
            }
            for profile in MODALITIES.all()
        ],
        "regimes": list(REGIMES),
        "rules_version": rules_version(),
        "unsigned": unsigned_diagnostic_rules(),
    }


def scenarios_json() -> dict[str, Any]:
    """``GET /diagnostic/scenarios``. The deterministic fixtures, with what each demonstrates."""
    return {
        "scenarios": [
            {
                "name": s.name,
                "description": s.description,
                "modality": s.modality.value,
                "machines": len(s.machines),
                "arrivals": len(s.arrivals),
                "hours": s.hours,
                "expects": s.expects,
            }
            for s in all_scenarios()
        ]
    }


# ---------------------------------------------------------------------------------------
# running a scenario or a resolved query
# ---------------------------------------------------------------------------------------


def _metrics_json(result: Any) -> dict[str, Any]:
    m = result.metrics
    return {
        "requests": m.requests,
        "answered": m.answered,
        "answered_late": m.answered_late,
        "diverted": m.diverted,
        "expired": m.expired,
        "abandoned": m.abandoned,
        "pending": m.pending,
        "auctions": m.auctions,
        "awarded": m.awarded,
        "timeliness": m.timeliness,
        "management_impact_rate": m.management_impact_rate,
        "average_delay_minutes": m.average_delay_minutes,
        "utilisation": m.utilisation,
        "mean_queue_depth": m.mean_queue_depth,
        "constraint_violations": m.constraint_violations,
        "burn_rate": {k: v for k, v in (m.burn_rate or {}).items()},
        "wins": {k: v for k, v in (m.wins or {}).items()},
        "alternative_usage": m.alternative_usage,
    }


def _schedule_json(result: Any) -> list[dict[str, Any]]:
    return [
        {
            "request_id": p.request_id,
            "agent": p.agent.value if hasattr(p.agent, "value") else str(p.agent),
            "machine_id": p.machine_id,
            "modality": p.modality.value,
            "requested_at": p.requested_at.isoformat(),
            "starts_at": p.starts_at.isoformat(),
            "ends_at": p.ends_at.isoformat(),
            "latest_useful_at": p.latest_useful_at.isoformat(),
            "diagnostic_value": p.diagnostic_value,
            "winning_bid": p.winning_bid,
        }
        for p in result.procedures
    ]


def run_scenario(
    config: Config, name: str, regime: str = "normal", derivation: bool = False
) -> dict[str, Any]:
    """Run one named fixture and return its outcome."""
    if name not in SCENARIOS:
        raise DiagnosticRefused(
            f"unknown scenario {name!r}", {"available": sorted(SCENARIOS)}
        )
    if regime not in REGIMES:
        raise DiagnosticRefused(f"unknown regime {regime!r}", {"available": list(REGIMES)})

    scenario = SCENARIOS[name]()
    result = run_policy(config, scenario, None, regime=regime)
    payload: dict[str, Any] = {
        "family": "diagnostic_machine",
        "scenario": scenario.name,
        "description": scenario.description,
        "modality": scenario.modality.value,
        "regime": regime,
        "expects": scenario.expects,
        "policy": "heuristic",
        "binding": False,
        "metrics": _metrics_json(result),
        "schedule": _schedule_json(result),
    }
    if derivation:
        payload["derivation"] = as_data(result, scenario, config)
    return payload


def run_query(
    config: Config, text: str, regime: str = "normal", derivation: bool = False
) -> dict[str, Any]:
    """Resolve a sentence to a scenario and run it, or refuse with what was missing."""
    resolution = resolve(text)
    if not resolution.runnable:
        raise DiagnosticRefused(
            "the query does not name a runnable diagnostic allocation",
            {
                "query": text,
                "evidence": list(resolution.evidence),
                "missing": list(resolution.missing),
                "note": "No clinical value was invented and no auction was opened.",
            },
        )
    payload = run_scenario(config, resolution.scenario, regime=regime, derivation=derivation)
    payload["resolved_from"] = {
        "query": text,
        "scenario": resolution.scenario,
        "confidence": resolution.confidence,
        "evidence": list(resolution.evidence),
    }
    return payload


# ---------------------------------------------------------------------------------------
# running an inline world
# ---------------------------------------------------------------------------------------


def _need(block: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in block or block[key] is None:
        raise DiagnosticRefused(f"{where} is missing required field {key!r}")
    return block[key]


def _when(value: Any, where: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise DiagnosticRefused(f"{where} is not an ISO timestamp: {value!r}") from exc


def _modality(value: Any, where: str) -> DiagnosticModality:
    try:
        return DiagnosticModality(str(value).lower())
    except ValueError as exc:
        raise DiagnosticRefused(
            f"{where} names no known modality: {value!r}",
            {"available": [m.value for m in DiagnosticModality]},
        ) from exc


def _machine(block: Mapping[str, Any]) -> DiagnosticMachineState:
    where = f"machine {block.get('machine_id', '?')!r}"
    try:
        status = MachineStatus(str(block.get("status", "available")).lower())
    except ValueError as exc:
        raise DiagnosticRefused(
            f"{where} has an unknown status {block.get('status')!r}",
            {"available": [s.value for s in MachineStatus]},
        ) from exc
    return DiagnosticMachineState(
        machine_id=str(_need(block, "machine_id", where)),
        modality=_modality(_need(block, "modality", where), where),
        status=status,
        window_starts_at=_when(_need(block, "window_starts_at", where), where),
        window_ends_at=_when(_need(block, "window_ends_at", where), where),
        capabilities=frozenset(block.get("capabilities") or ()),
        allocations=tuple(
            AllocationInterval(
                machine_id=str(a.get("machine_id", block.get("machine_id"))),
                request_id=str(a.get("request_id", "")),
                starts_at=_when(a.get("starts_at"), f"{where} allocation"),
                ends_at=_when(a.get("ends_at"), f"{where} allocation"),
            )
            for a in (block.get("allocations") or ())
        ),
        setup_minutes=int(block.get("setup_minutes", 0)),
        cleanup_minutes=int(block.get("cleanup_minutes", 0)),
    )


def _request(block: Mapping[str, Any]) -> DiagnosticRequest:
    where = f"request {block.get('request_id', '?')!r}"
    try:
        agent = AgentKind(str(_need(block, "agent", where)).lower())
    except ValueError as exc:
        raise DiagnosticRefused(
            f"{where} names no known agent {block.get('agent')!r}",
            {"available": [a.value for a in AgentKind]},
        ) from exc

    eligible = block.get("eligible_modalities") or ()
    if not eligible:
        raise DiagnosticRefused(f"{where} lists no eligible_modalities")

    # Every clinical number is required rather than defaulted. A default here is a fabricated
    # value that the auction would then rank a real patient against.
    return DiagnosticRequest(
        request_id=str(_need(block, "request_id", where)),
        patient_token=str(_need(block, "patient_token", where)),
        agent=agent,
        clinical_question=str(_need(block, "clinical_question", where)),
        requested_procedure=str(_need(block, "requested_procedure", where)),
        eligible_modalities=frozenset(_modality(m, where) for m in eligible),
        requested_at=_when(_need(block, "requested_at", where), where),
        latest_useful_at=_when(_need(block, "latest_useful_at", where), where),
        estimated_duration=timedelta(
            minutes=float(_need(block, "estimated_duration_minutes", where))
        ),
        diagnostic_yield=float(_need(block, "diagnostic_yield", where)),
        management_impact_probability=float(
            _need(block, "management_impact_probability", where)
        ),
        management_impact_importance=float(
            _need(block, "management_impact_importance", where)
        ),
        required_capabilities=frozenset(block.get("required_capabilities") or ()),
        alternative_procedures=tuple(block.get("alternative_procedures") or ()),
        transport_minutes=int(block.get("transport_minutes", 0)),
        modality_yields={
            _modality(k, where): float(v)
            for k, v in (block.get("modality_yields") or {}).items()
        },
        reschedulable=bool(block.get("reschedulable", False)),
    )


def run_inline(
    config: Config,
    machines: Sequence[Mapping[str, Any]],
    requests: Sequence[Mapping[str, Any]],
    regime: str = "normal",
    mode: str = "simulation",
    opened_at: str | None = None,
) -> dict[str, Any]:
    """Run one auction over state supplied by the caller."""
    if not machines:
        raise DiagnosticRefused("a diagnostic auction needs at least one machine")
    if not requests:
        raise DiagnosticRefused("a diagnostic auction needs at least one request")
    if regime not in REGIMES:
        raise DiagnosticRefused(f"unknown regime {regime!r}", {"available": list(REGIMES)})

    # The contracts enforce invariants this parser does not restate (interval ordering,
    # non-empty identifiers, a deadline after the request). Those are all caller errors, so
    # they are reported as a refusal with the reason rather than escaping as a 500.
    try:
        parsed_machines = [_machine(m) for m in machines]
        parsed_requests = [_request(r) for r in requests]
    except DiagnosticRefused:
        raise
    except ValueError as exc:
        raise DiagnosticRefused(str(exc)) from exc

    modalities = {m.modality for m in parsed_machines}
    if len(modalities) != 1:
        raise DiagnosticRefused(
            "one auction covers one modality; machines name "
            f"{sorted(m.value for m in modalities)}"
        )
    modality = next(iter(modalities))
    profile = MODALITIES.get(modality)
    scoped = for_modality(config, modality)

    at = _when(opened_at, "opened_at") if opened_at else min(
        m.window_starts_at for m in parsed_machines
    )
    shift = diagnostic_shift(at, hours=8.0)
    budgets = open_diagnostic_budgets(
        scoped, profile, shift, machines=parsed_machines, regime=regime
    )

    outcome = run_diagnostic_auction(
        scoped,
        profile,
        machines=parsed_machines,
        requests=parsed_requests,
        budgets=budgets,
        opened_at=at,
        mode=AuctionMode(mode),
    )
    return _outcome_json(outcome, modality, regime, mode)


def _outcome_json(
    outcome: Any, modality: DiagnosticModality, regime: str, mode: str
) -> dict[str, Any]:
    """One auction's result: the full ladder, losers and withdrawals included.

    A response naming only the winner would make the fairness and cap-fitting questions
    unanswerable later — the same reason ``allocation.auction`` records one row per agent per
    round rather than one row per auction.
    """
    result = outcome.result
    return {
        "family": "diagnostic_machine",
        "auction_id": result.auction_id,
        "auction_key": result.auction_key,
        "modality": result.modality.value,
        "machine_id": result.machine_id,
        "regime": regime,
        "mode": mode,
        "binding": False,
        "policy": "heuristic",
        "opened_at": result.opened_at.isoformat(),
        "closed_at": result.closed_at.isoformat() if result.closed_at else None,
        "contention": result.contention,
        "reserve_price": result.reserve_price,
        "winner": result.winner.value if result.winner is not None else None,
        "winning_request_id": result.winning_request_id,
        "winning_bid": result.winning_bid,
        "outcome": result.outcome.value if hasattr(result.outcome, "value") else result.outcome,
        "awarded_interval": None
        if result.awarded_interval is None
        else {
            "machine_id": result.awarded_interval.machine_id,
            "request_id": result.awarded_interval.request_id,
            "starts_at": result.awarded_interval.starts_at.isoformat(),
            "ends_at": result.awarded_interval.ends_at.isoformat(),
        },
        "rounds": [
            {
                "round_index": rnd.round_index,
                "opened_at": rnd.opened_at.isoformat(),
                "active_agents": [a.value for a in rnd.active_agents],
                "bids": [
                    {
                        "agent": bid.agent.value,
                        "request_id": bid.request_id,
                        "action": bid.action.value,
                        "pathway": bid.pathway.value if bid.pathway is not None else None,
                        "amount": bid.amount,
                        "utility": bid.utility,
                        "ceiling": bid.ceiling,
                        "alpha": bid.alpha,
                        "remaining_budget": bid.remaining_budget,
                        "clamped_by": bid.clamped_by,
                    }
                    for bid in rnd.bids
                ],
            }
            for rnd in result.rounds
        ],
        "caps_version": result.caps_version,
        "config_version": result.config_version,
        "unsigned_rules": result.unsigned_rules,
        "note": "Nothing is persisted and no machine time is held; mode live is refused.",
    }
