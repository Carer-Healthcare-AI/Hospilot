"""The end-to-end sequence. Query in, logged auction out.

This is the only module that knows the order of the pipeline::

    1  use case      resolve the query to a profile          RL-Steps 1
    2  ingest        one read of the world                   RL-Steps 1
    3  utility       8 components -> points                  RL-Steps 2, 8
    4  ceiling       willingness to compete                  RL-Steps 8.2 / D.9
    5  budget        Base x Demand x Fairness x Scarcity     RL-Steps 4
    6  auction       3 rounds, sealed first                  RL-Steps 9-18
    7  settlement    Bid x Contention x Outcome x Rate       RL-Steps 19
    8  audit         rows, validated                         BUILD_SPEC 7
    9  reward        scheduled, not scored                   RL-Steps 23-24

Nothing here computes anything. Every number comes from a layer that is separately tested;
this module sequences them and records what happened. If a formula ever appears in this file,
it is in the wrong place — the layer that owns it has been bypassed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from allocation.audit.records import AuditBundle
from allocation.audit.writer import build_bundle
from allocation.auction.runner import AuctionOutcome, run_auction
from allocation.budget.base import BaseBudget, derive_all
from allocation.budget.factors import compute_factors
from allocation.budget.ledger import burn_band, open_shift
from allocation.budget.shifts import Shift, resolve_shift
from allocation.config import Config
from allocation.contracts import (
    AgentKind,
    AuctionMode,
    BiddingPolicy,
    BudgetState,
    Candidate,
    DataSource,
    FeatureSnapshot,
    ReleaseEvent,
    UtilityBreakdown,
)
from allocation.ingest.snapshot import build_snapshot_sync
from allocation.pathway.alternatives import unit_reader
from allocation.pathway.options import RoundOptions, build_options
from allocation.policy.heuristic import HeuristicPolicy
from allocation.profiles.registry import ResourceProfile
from allocation.reward.observer import PendingObservation, pending_for
from allocation.trigger import query as query_mod
from allocation.trigger.steps import StepTrace
from allocation.utility.ceiling import ceiling_for
from allocation.utility.components import build_components
from allocation.utility.engine import UtilityEngine
from allocation.utility.uplift import UpliftEstimate, estimate as estimate_uplift
from allocation.utility.uplift import enabled as uplift_enabled


class EngineUtilitySource:
    """Scores every candidate from a fresh snapshot, once per round.

    **One read of the world per round**, cached, so the eight components in a round all see
    the same hospital. Re-reading per component would let two of them disagree about
    occupancy and produce a utility that cannot be reproduced from the log.

    Utilities are recomputed each round rather than scored once and reused: RL-Steps has ER
    rising 135 -> 148 -> 171 across three rounds as its patient deteriorates. Whether they
    actually move depends on the data source — against a static fixture they will not, and
    that is the fixture being static, not the engine refusing to look.

    ``unit`` names whose beds the snapshot describes and is fixed for the auction's whole
    life, because the resource is. Every round reads the same unit; what may change between
    rounds is that unit's occupancy.
    """

    def __init__(
        self,
        config: Config,
        engine: UtilityEngine,
        source: DataSource,
        candidates: Sequence[Candidate],
        auction_id: str,
        opened_at: datetime,
        round_seconds: int,
        unit: str,
        horizon_hours: float = 4.0,
    ) -> None:
        self._config = config
        self._engine = engine
        self._source = source
        self._candidates = tuple(candidates)
        self._auction_id = auction_id
        self._opened_at = opened_at
        self._round_seconds = round_seconds
        self._unit = unit
        self._horizon_hours = horizon_hours
        self._cache: dict[int, dict[str, UtilityBreakdown]] = {}
        self._snapshots: dict[int, FeatureSnapshot] = {}

    def snapshot(self, round_index: int) -> FeatureSnapshot:
        if round_index not in self._snapshots:
            self._snapshots[round_index] = build_snapshot_sync(
                self._source,
                self._candidates,
                self._opened_at + timedelta(seconds=self._round_seconds * round_index),
                self._config,
                self._unit,
                auction_id=self._auction_id,
                round_index=round_index,
            )
        return self._snapshots[round_index]

    def utilities(self, round_index: int) -> Mapping[str, UtilityBreakdown]:
        if round_index not in self._cache:
            self._cache[round_index] = self._engine.score_all(
                self._candidates, self.snapshot(round_index)
            )
        return self._cache[round_index]

    def uplifts(self, round_index: int) -> Mapping[str, UpliftEstimate]:
        """Per-candidate uplift, recomputed each round alongside the utility.

        Pinning it at round 0 would price a deterioration that the later rounds can already
        see — the ceiling has to move with the patient, not with the auction's opening.
        """
        snapshot = self.snapshot(round_index)
        return {
            candidate.candidate_id: estimate_uplift(
                self._config,
                snapshot.patients[candidate.candidate_id],
                snapshot.taken_at,
                self._horizon_hours,
            )
            for candidate in self._candidates
        }

    def ceilings(self, round_index: int) -> Mapping[str, float]:
        uplifts = self.uplifts(round_index)
        return {
            cid: ceiling_for(breakdown, uplifts[cid].value or None).value
            for cid, breakdown in self.utilities(round_index).items()
        }


class EnginePathwaySource:
    """Pathway options per round, over the same snapshots the utilities were scored from.

    Sharing ``EngineUtilitySource``'s snapshot cache is the point, not an optimisation: the
    alternative unit that discounts a patient's utility and the alternative unit an exit
    withdraws them into must be the same fact. Two reads a minute apart could disagree, and
    the disagreement would be between the bid and the decision to abandon it.

    ``reader`` is what turns ``WITHDRAW_ALTERNATIVE`` from infeasible into feasible: it reads
    the *other* unit's bed state, which a snapshot deliberately cannot describe. Passing
    ``None`` leaves every alternative's availability unknown, and unknown is not open — so the
    action stays unreachable rather than being taken on an assumption.
    """

    def __init__(
        self,
        config: Config,
        source: EngineUtilitySource,
        candidates: Sequence[Candidate],
        profile: ResourceProfile,
        data_source: DataSource | None = None,
        read_alternatives: bool = False,
    ) -> None:
        self._config = config
        self._source = source
        self._candidates = tuple(candidates)
        self._profile = profile
        self._data_source = data_source
        self._read_alternatives = read_alternatives
        self._cache: dict[int, Mapping[AgentKind, RoundOptions]] = {}

    def options(self, round_index: int) -> Mapping[AgentKind, RoundOptions]:
        if round_index not in self._cache:
            snapshot = self._source.snapshot(round_index)
            reader = (
                unit_reader(self._data_source, snapshot.taken_at)
                if self._read_alternatives and self._data_source is not None
                else None
            )
            self._cache[round_index] = {
                candidate.agent: build_options(
                    self._config,
                    candidate,
                    snapshot,
                    target_unit=self._profile.resource_type.unit,
                    horizon_hours=self._profile.allocation_horizon_hours,
                    resource_type=self._profile.resource_type,
                    reader=reader,
                )
                for candidate in self._candidates
            }
        return self._cache[round_index]


@dataclass(frozen=True, slots=True)
class AllocationRun:
    """Everything one triggered allocation produced."""

    query: str
    profile: ResourceProfile
    event: ReleaseEvent
    outcome: AuctionOutcome
    bundle: AuditBundle
    snapshot: FeatureSnapshot
    utilities: Mapping[str, UtilityBreakdown]
    ceilings: Mapping[str, float]
    opening_budgets: Mapping[AgentKind, BudgetState]
    bases: Mapping[AgentKind, BaseBudget]
    shift: Shift
    pending: PendingObservation
    trace: StepTrace
    #: The pathway options each agent decided against, at the opening round. Retained because
    #: they are part of the state a policy saw — a training example that records the action but
    #: not whether an alternative was available is an example nobody can learn from.
    pathways: Mapping[AgentKind, RoundOptions] = field(default_factory=dict)

    @property
    def winner(self) -> AgentKind | None:
        return self.outcome.result.winner

    @property
    def binding(self) -> bool:
        """True only when this run actually held a bed."""
        return self.event.mode.is_binding


def run_allocation(
    config: Config,
    source: DataSource,
    candidates: Sequence[Candidate],
    now: datetime,
    query: str = "",
    profile: ResourceProfile | None = None,
    event: ReleaseEvent | None = None,
    policy: BiddingPolicy | None = None,
    mode: AuctionMode = AuctionMode.SIMULATION,
    resource_id: str | None = None,
    budgets: Mapping[AgentKind, BudgetState] | None = None,
    charge_budgets: bool | None = None,
    read_alternatives: bool = False,
) -> AllocationRun:
    """Run one allocation from trigger to audit bundle.

    ``query`` and ``profile`` are alternatives: a typed sentence resolves to a profile, or a
    caller that already knows the resource passes one. ``event`` likewise — production passes
    a real :class:`ReleaseEvent`; a hand-fired run gets one built at ``SIMULATION``.

    ``budgets`` carries a ledger forward. Omitted, each run opens a fresh shift budget, which
    is right for a single auction and wrong for a sequence: without carrying them, every
    auction in a shift would start at full budget and burn rate would be meaningless.
    """
    trace = StepTrace()

    # 1 · Use case ---------------------------------------------------------------------
    if profile is None:
        profile = query_mod.resolve_profile(query)
    if event is None:
        event = query_mod.manual_event(profile, now, resource_id=resource_id, mode=mode)

    # Caps are per resource type, so the resource has to be known before anything is scored.
    # This is the only place the selection happens: everything below — policy, engine,
    # components, budgets, audit — reads caps through this config and inherits the right
    # caps_version with it.
    config = config.for_resource(profile)

    trace.add(
        "use_case",
        "RL-Steps 1",
        "Use case resolved",
        rows=(
            ("matched on", query_mod.matched_tokens(query, profile)),
            *query_mod.describe(profile),
            ("mode", event.mode.value),
            ("binding", "yes — holds a bed" if event.mode.is_binding else "no"),
            ("trigger", event.source.value),
            ("bed ETA", event.predicted_free_at.strftime("%H:%M")),
        ),
        notes=(
            ()
            if event.mode.is_binding
            else ("Not binding: no bed is held and no real budget moves.",)
        ),
    )

    eligible = [c for c in candidates if profile.is_eligible(c.agent)]
    policy = policy or HeuristicPolicy(config)
    policy_name = getattr(policy, "name", "unknown")

    engine = UtilityEngine(config, profile, build_components(config, profile))
    auction_id_hint = event.event_id

    # The unit whose beds every snapshot describes. It comes from the resource being
    # auctioned, not from the bidders: ER, OT and ICU can all bid for a ward bed, and the
    # occupancy that prices the auction is the ward's.
    utility_source = EngineUtilitySource(
        config, engine, source, eligible, auction_id_hint, event.detected_at,
        profile.round_seconds, profile.resource_type.unit,
        horizon_hours=profile.allocation_horizon_hours,
    )

    # 2 · Ingest -----------------------------------------------------------------------
    snapshot = utility_source.snapshot(0)
    hospital = snapshot.hospital
    trace.add(
        "ingest",
        "RL-Steps 1",
        "One read of the world",
        rows=(
            ("taken at", snapshot.taken_at.strftime("%Y-%m-%d %H:%M:%S")),
            (f"{hospital.unit.upper()} occupancy",
             f"{hospital.unit_occupied_beds}/{hospital.unit_total_beds}"
             f" = {hospital.occupancy:.0%}"),
            ("expected discharges 4h", _show(hospital.expected_discharges_4h)),
            ("predicted demand 4h", _show(hospital.predicted_demand_4h)),
            ("ED boarding", _show(hospital.boarding_count)),
            ("candidates", ", ".join(f"{c.agent.value}:{c.candidate_id}" for c in eligible)),
            ("snapshot id", snapshot.snapshot_id),
        ),
    )

    # 3 · Utility ----------------------------------------------------------------------
    utilities = dict(utility_source.utilities(0))
    trace.add(
        "utility",
        "RL-Steps 2, 8",
        "Utility points — 8 capped components",
        rows=tuple(
            (
                f"{_agent_of(eligible, cid).value} {cid}",
                f"{breakdown.total:7.1f}  (coverage {_coverage(breakdown):.0%})",
            )
            for cid, breakdown in utilities.items()
        ),
        notes=(
            "Caps are unfitted (B.13). These points are internally consistent, not calibrated.",
        ),
    )

    # 4 · Ceiling ----------------------------------------------------------------------
    ceilings = dict(utility_source.ceilings(0))
    uplifts = utility_source.uplifts(0)
    on = uplift_enabled(config)
    trace.add(
        "ceiling",
        "RL-Steps 8.2 / D.9",
        "Utility ceiling — maximum willingness to compete",
        rows=tuple(
            (
                f"{_agent_of(eligible, cid).value} {cid}",
                f"{utilities[cid].total:7.1f} x (1 + {uplifts[cid].value:.2f}) = {value:7.1f}"
                + (f"   [{uplifts[cid].band}]" if uplifts[cid].band else "")
                + (f"   {uplifts[cid].note}" if uplifts[cid].note and on else ""),
            )
            for cid, value in ceilings.items()
        ),
        notes=(
            (
                "⚠ UPLIFT IS ON. The bands in rules/uplift.yaml are OURS — RL-Steps says the "
                "ceiling should exceed utility but gives no rule for by how much. This is the "
                "one unsigned table that can reallocate a bed rather than distort a score.",
            )
            if on
            else (
                "Ceiling = U. The B.9 uplift is disabled, so no agent is willing to bid above "
                "its current utility — D.9's own fallback, and conservative: it can only "
                "under-bid. Enable in rules/uplift.yaml or with --uplift.",
            )
        ),
    )

    # 5 · Budget -----------------------------------------------------------------------
    shift = resolve_shift(config, now)
    agents = tuple(dict.fromkeys(c.agent for c in eligible))
    bases = derive_all(config, agents)

    if budgets is None:
        opened: dict[AgentKind, BudgetState] = {}
        for agent in agents:
            factors = compute_factors(
                config,
                agent,
                occupancy_4h=hospital.occupancy,
                demand_forecast=hospital.predicted_demand_4h,
                demand_median_30d=None,  # F-18: needs 30 days of forecast_history
            )
            opened[agent] = open_shift(config, bases[agent], factors, shift)
        budgets = opened
    else:
        missing = [a.value for a in agents if a not in budgets]
        if missing:
            raise ValueError(
                f"carried budgets are missing bidders {missing}. An agent with no budget "
                "row cannot be scored, and defaulting it to a fresh shift allowance would "
                "silently hand it a full budget mid-shift."
            )
        # One pool per resource type (D-3). A budget row stamps the caps_version its points
        # were denominated in, and caps are per resource, so a row from another resource type
        # is detectable here rather than by wondering why ward-bed auctions stopped clearing.
        foreign = sorted(
            {
                s.caps_version
                for a, s in budgets.items()
                if a in agents and s.caps_version and s.caps_version != config.caps_version
            }
        )
        if foreign:
            raise ValueError(
                f"carried budgets were derived under caps_version(s) {foreign}, but this "
                f"{profile.resource_type.value} auction runs under {config.caps_version}. "
                "Budgets are denominated in utility points and each resource type has its "
                "own pool — spending one resource's budget on another lets ICU auctions, at "
                "~107 points each, drain what ward-bed auctions bid ~50 into."
            )

    trace.add(
        "budget",
        "RL-Steps 4",
        "Priority budget — B = Base x Demand x Criticality x Scarcity x Fairness",
        rows=tuple(
            (
                agent.value,
                f"{state.base:6.1f} x {state.demand:.2f} x {state.criticality:.2f} "
                f"x {state.scarcity:.2f} x {state.fairness:.2f} = {state.budget_total:7.1f}",
            )
            for agent, state in budgets.items()
        ),
        notes=(
            f"Shift {shift.label} {shift.start:%H:%M}-{shift.end:%H:%M}.",
            f"Base is COMMON to all departments ({next(iter(bases.values())).source}); the "
            "factors carry every difference between them — RL-Steps section 4."
            if next(iter(bases.values())).source == "common"
            else "Base is derived per department from target win counts (AGENT_BUDGET v0.3).",
            "Demand 1.00 and Fairness 1.00 until 30 days of forecast and ~10 shifts of log "
            f"exist; Scarcity is one value for the whole {hospital.unit} auction. So "
            "Criticality is the only factor differentiating departments today (F-19).",
        ),
    )

    # 6-7 · Auction and settlement -----------------------------------------------------
    pathway_source = EnginePathwaySource(
        config, utility_source, eligible, profile,
        data_source=source, read_alternatives=read_alternatives,
    )

    outcome = run_auction(
        config,
        profile=profile,
        event=event,
        candidates=eligible,
        utility_source=utility_source,
        policy=policy,
        budgets=budgets,
        snapshot=snapshot,
        policy_name=policy_name,
        charge_budgets=charge_budgets,
        pathways=pathway_source,
    )
    result = outcome.result

    # How long the auction was allowed to be, and what bound it. Shown before the rounds
    # because it is the thing that explains their count: an auction that ran two rounds
    # because the bed was six minutes out is a different event from one that ran two because
    # bidding stopped, and neither is visible from the round rows alone.
    lead_minutes = (event.predicted_free_at - event.detected_at).total_seconds() / 60.0
    tightest_ttl = min(profile.ttl_for(c.agent) for c in eligible)
    trace.add(
        "round_budget",
        "RL-Steps 7-8",
        "Rounds available",
        rows=(
            ("profile cadence", f"up to {profile.max_rounds} x {profile.round_seconds}s"),
            ("bed lands in", f"{lead_minutes:.0f} min -> "
                             f"{int(lead_minutes * 60 // profile.round_seconds)} rounds"),
            ("tightest utility TTL", f"{tightest_ttl} min -> "
                                     f"{int(tightest_ttl * 60 // profile.round_seconds)} rounds"),
            ("budgeted", f"{result.max_rounds} rounds"),
            ("ran", f"{result.rounds_run} rounds"),
        ),
        notes=(
            ()
            if result.rounds_run == result.max_rounds
            else (
                "Closed early: either every agent withdrew, or a whole round moved no bid "
                "while the leader was already above the reserve.",
            )
        ),
    )

    # Sections 9, 12 and 15 are rounds 1-3. RL-Steps has exactly three, so a fourth round has
    # no section to cite — saying "RL-Steps 18" for it would attribute the budget summary to
    # a round that the framework does not describe.
    round_sections = ("RL-Steps 9", "RL-Steps 12", "RL-Steps 15")

    for round_state in result.rounds:
        index = round_state.round_index
        trace.add(
            f"round_{index}",
            round_sections[index] if index < len(round_sections) else "beyond RL-Steps",
            f"Round {index + 1}"
            + (" — sealed, no one sees a rival" if index == 0 else "")
            + ("" if index < len(round_sections) else " — RL-Steps specifies three"),
            rows=tuple(
                (
                    bid.agent.value,
                    f"{bid.action.value:<13} bid {bid.amount:6.1f}  "
                    f"utility {bid.utility:6.1f}  ceiling {bid.ceiling:6.1f}"
                    + (f"  alpha {bid.alpha:.2f}" if bid.alpha is not None else ""),
                )
                for bid in round_state.bids
            ),
        )

    trace.add(
        "close",
        "RL-Steps 17-19",
        "Close and settle",
        rows=(
            ("winner", result.winner.value if result.winner else "none"),
            ("winning bid", _show(result.winning_bid)),
            ("reserve price", f"{result.reserve_price:.1f}"),
            ("outcome", result.outcome),
            ("contention", f"{result.contention:.2f}"),
            *(
                (
                    f"cost {agent.value}",
                    f"{spend.bid:6.1f} x {spend.contention:.2f} x {spend.outcome_factor:.2f} "
                    f"x {spend.commitment_rate:.2f} = {spend.cost:6.2f}",
                )
                for agent, spend in outcome.spends.items()
            ),
            *(
                (f"burn {agent.value}", _burn_row(config, outcome, agent))
                for agent in outcome.budgets
            ),
        ),
        notes=(
            (
                "Burn is one auction into an eight-hour shift — 'inert' here is a partial "
                "number, not a finding. Judge it at shift end.",
            )
            if event.mode.is_binding
            else (
                "Costs are computed and logged but NOT charged: a non-binding auction is "
                "scored identically to a live one and moves no budget (settle.py). The burn "
                "shown is what a live run would have produced.",
                "Burn is one auction into an eight-hour shift — judge it at shift end.",
            )
        ),
        expected={
            # RL-Steps section 18 awards the bed to ER. That is the claim worth checking:
            # the bid ladder itself cannot match, because these utilities are computed from
            # Appendix C (107.4 / 35.1 / 48.6) rather than RL-Steps' published 135 / 117 / 86
            # (F-20). The ranking is what survives the discrepancy, and the ranking is what
            # decides the allocation.
            "winner (section 18)": ("er", result.winner.value if result.winner else "none"),
        },
    )

    # 8 · Audit ------------------------------------------------------------------------
    bundle = build_bundle(
        outcome, event=event, snapshot=snapshot, candidates=eligible,
        policy_name=policy_name, bases=bases,
    )
    trace.add(
        "audit",
        "BUILD_SPEC 7",
        "Audit bundle — validated",
        rows=(
            ("auction rows", "1"),
            ("bid rows", str(len(bundle.bids))),
            ("budget rows", str(len(bundle.budgets))),
            ("snapshot rows", str(len(bundle.snapshots))),
            ("caps version", result.caps_version[:12]),
            ("config version", result.config_version[:12]),
            ("unsigned rules", str(len(result.unsigned_rules))),
        ),
        notes=(
            "Every round of every agent is recorded, losers included — a losing episode is "
            "training data too (sections 23-24).",
        ),
    )

    # 9 · Reward -----------------------------------------------------------------------
    pending = pending_for(config, result.auction_id, result.closed_at)
    trace.add(
        "reward",
        "RL-Steps 23-24",
        "Reward — scheduled, not scored",
        rows=(
            ("closed at", result.closed_at.strftime("%H:%M")),
            ("observation due", pending.due_at.strftime("%H:%M")),
            ("horizon", f"{pending.horizon_hours:g} h"),
        ),
        notes=(
            "The reward is not known now and cannot be. no_mortality has no structured "
            "source (F-01), so this episode will be incomplete and must stay out of any "
            "training set.",
        ),
    )

    return AllocationRun(
        query=query,
        profile=profile,
        event=event,
        outcome=outcome,
        bundle=bundle,
        snapshot=snapshot,
        utilities=utilities,
        ceilings=ceilings,
        opening_budgets=budgets,
        bases=bases,
        shift=shift,
        pending=pending,
        trace=trace,
        pathways=dict(pathway_source.options(0)),
    )


# -- small display helpers ---------------------------------------------------------------


def _show(value: object) -> str:
    """Absent is shown as absent. Never as 0 — that is the whole three-state rule."""
    return "— (absent)" if value is None else f"{value}"


def _burn_row(config: Config, outcome: AuctionOutcome, agent: AgentKind) -> str:
    """Burn rate, distinguishing charged from merely computed.

    In a non-binding run ``spent`` stays 0 by design, so reporting it beside a non-zero cost
    reads as a bug in the ledger. The would-be burn is shown instead, explicitly marked, so
    the number is neither hidden nor mistaken for a real decrement.
    """
    state = outcome.budgets[agent]
    spend = outcome.spends.get(agent)
    charged = outcome.result.mode.is_binding

    spent = state.spent if charged else (spend.cost if spend else 0.0)
    rate = spent / state.budget_total if state.budget_total > 0 else 0.0
    suffix = "" if charged else "  (not charged — simulation)"
    return (
        f"{spent:6.2f} / {state.budget_total:7.1f} = {rate:.1%}  "
        f"[{burn_band(config, rate)}]{suffix}"
    )


def _agent_of(candidates: Sequence[Candidate], candidate_id: str) -> AgentKind:
    for candidate in candidates:
        if candidate.candidate_id == candidate_id:
            return candidate.agent
    raise KeyError(candidate_id)


def _coverage(breakdown: UtilityBreakdown) -> float:
    """Weighted mean coverage across components — how much of the model had inputs."""
    components = breakdown.components
    if not components:
        return 0.0
    return sum(c.coverage for c in components) / len(components)
