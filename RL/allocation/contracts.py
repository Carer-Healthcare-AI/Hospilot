"""Frozen types that cross layer boundaries. Imports nothing else from the package.

Three invariants are enforced here rather than by convention, because every one of them has
already been got wrong somewhere in the source documents:

1. **Absent is not zero.** ``Signal.value is None`` means the input was missing.
   ``RL_STEPS_END_TO_END.md`` D.0: *"A missing factor is dropped, never treated as zero. Zero
   means 'this patient is fine', which would rank an untested patient above a tested one."*
   A ``float`` field could not express the difference; ``Signal`` can.

2. **Caps carry their own sign.** Alternative Availability is ``-20`` and Resource Stress is
   ``-10``. Points are ``cap * score`` with ``score`` in ``[0, 1]``, so they come out negative
   and the utility is a plain sum. ``D.0`` writes the utility with minus signs in front of
   both, but Appendix C sums the already-negative values (45.4 + 23.7 + 15.4 + 16.7 + 11.2 +
   5.3 - 2.4 - 8.2 = 107.1). Following the prose would double-negate them.

3. **Components are scored per agent kind.** ``Operational`` (D.5) and ``Waiting/Delay`` (D.3)
   are defined differently for a surgical bidder than a medical one — that is the framework's
   design, not an accommodation. ``AgentKind`` is therefore in the ``Component`` signature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping, Protocol, Sequence

# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class AgentKind(str, Enum):
    """A bidding department.

    ``ICU`` is present because RL-Steps section 3 gives ICU internal demand a TTL, but never
    models it as a bidder. Whether it bids and holds a budget is AGENT_BUDGET open decision 3
    (BUILD_SPEC F-12) — unresolved, so it is declared but not yet eligible in any profile.
    """

    ER = "er"
    OT = "ot"
    WARD = "ward"
    ICU = "icu"


class ResourceType(str, Enum):
    """What is being auctioned. The profile registry is keyed by this.

    **A bed family, not one bed.** Values are ``<unit>_bed``, and ``<unit>`` is drawn from the
    unit vocabulary in ``config/rules/units.yaml`` — the same six names its ``ward_patterns``
    table maps ``hospilot.beds.ward`` onto, and the same six the care ladder orders. Keeping
    the two lists identical means a resource can find its rung on the ladder by string, with
    no mapping table to fall out of sync.

    Every one of the six is auctionable. Which beds a hospital actually frees is not known in
    advance, and a query may name a unit this engine would otherwise have refused to auction.

    ``units.yaml`` also carries a ``{pattern: "", unit: ward}`` catch-all, so an unrecognised
    *ward string* classifies as ``ward``. That is a different question from an unrecognised
    *query*, which still resolves to no profile at all rather than defaulting.
    """

    ICU_BED = "icu_bed"
    HDU_BED = "hdu_bed"
    PACU_BED = "pacu_bed"
    RESUS_BED = "resus_bed"
    ED_BED = "ed_bed"
    WARD_BED = "ward_bed"

    @property
    def unit(self) -> str:
        """The ``units.yaml`` unit this resource sits in — ``icu_bed`` -> ``icu``.

        Used to look the resource up in the care ladder and to match ``HospitalState.unit``.
        """
        return self.value.removesuffix("_bed")


class AuctionMode(str, Enum):
    """Why this auction is running.

    Only ``LIVE`` holds a bed, decrements a real budget, and is valid RL training data.
    BUILD_SPEC section 1 of the trigger decision: without this column, hand-fired test runs
    are indistinguishable from real allocations afterwards, and the blocked models in
    section 6.2 would train on auctions where no bed was ever held.
    """

    LIVE = "live"
    SIMULATION = "simulation"
    ADVISORY = "advisory"
    REPLAY = "replay"

    @property
    def is_binding(self) -> bool:
        """True only when the outcome changes hospital state."""
        return self is AuctionMode.LIVE


class TriggerSource(str, Enum):
    """What opened the auction. All paths converge on one ``open_auction`` call."""

    DISCHARGE_PREDICTION = "discharge_prediction"
    CHANGE_QUEUE = "change_queue"
    MANUAL_QUERY = "manual_query"
    FIXTURE = "fixture"


class ComponentName(str, Enum):
    """The eight utility components of RL_STEPS_END_TO_END.md section 2."""

    CLINICAL_BENEFIT = "clinical_benefit"
    URGENCY = "urgency"
    WAITING = "waiting"
    THROUGHPUT = "throughput"
    OPERATIONAL = "operational"
    FINANCIAL = "financial"
    ALTERNATIVE = "alternative"
    RESOURCE_STRESS = "resource_stress"


class Action(str, Enum):
    """What happens to the BID this round. RL-Steps section 5.

    Three mechanics, and only three: a bid goes up, stays, or stops. This is deliberately
    *not* the decision space — see :class:`QAction`. An agent that withdraws because HDU is
    free and one that withdraws because it simply cannot win both emit ``WITHDRAW`` here, and
    the difference between them is the whole of RL-Steps' closing table.
    """

    WITHDRAW = "withdraw"
    HOLD = "hold"
    INCREASE_BID = "increase_bid"


class QAction(str, Enum):
    """What happens to the PATIENT. RL-Steps' closing table — the five valued decisions.

    ``Action`` and ``QAction`` are different questions, and collapsing them was a real defect:

    * ``WIN_NOW`` and ``CONTINUE`` are both ``INCREASE_BID``, separated by aggression.
    * The other three are all ``WITHDRAW``, separated by **what the agent does next** — and
      that difference is invisible in the bid mechanics.

    Why it had to be fixed before any training. ``config/reward.yaml`` already pays
    ``safely_held`` (+10, *"a losing surgical case was safely held in PACU"*) and
    ``second_bed_opened`` (+15, *"a further ICU bed became available inside the window"*).
    Those are the outcomes of ``WITHDRAW_ALTERNATIVE`` and ``AWAIT_NEXT_RESOURCE``. With one
    undifferentiated withdrawal the points landed on whichever agent happened to have bid, for
    a hand-off no policy ever chose — the objective rewarded behaviour the action space could
    not express, and a policy trained on it learns to attribute a pathway decision to a bid.

    :class:`Decision` refuses to construct an exit that does not name its onward plan, so the
    ``+`` in *Withdraw + Alternative* is enforced by the type rather than by discipline.
    """

    #: Aggressive — secure the resource now. Deterioration risk high, delay dangerous.
    WIN_NOW = "win_now"
    #: Stay in, do not overbid; a later round may be cheaper. The safe waiting window is real.
    CONTINUE = "continue"
    #: Leave and activate the best alternative pathway. Requires a named target unit.
    WITHDRAW_ALTERNATIVE = "withdraw_alternative"
    #: Leave because another bed is predicted soon. Requires an ETA and a probability.
    AWAIT_NEXT_RESOURCE = "await_next_resource"
    #: Leave temporarily, under a monitored condition that re-opens bidding. Requires a
    #: :class:`ReentryTrigger` — a "temporary" exit nothing watches is a permanent one.
    RE_ENTER_LATER = "re_enter_later"
    #: Leave with **nothing arranged**. Not in RL-Steps' table, and added because that table
    #: has no entry for the case that forces most real withdrawals: the agent cannot win, no
    #: alternative unit is open, and no release is predicted inside the patient's safe window.
    #: Section 12's Ward has an HDU bed *and* a 50 % second-bed estimate; a patient with
    #: neither still has to leave the auction, and the framework does not say what that is.
    #:
    #: **A sixth action rather than a null plan, because it has to be scoreable.** The other
    #: three exits arrange something; this one abandons. Folding it in with them would let
    #: ``safely_held`` (+10) and ``second_bed_opened`` (+15) attach to an abandonment, which
    #: is the precise failure the Q-space exists to prevent. A rising count here is the
    #: mechanism reporting that it is rationing past what the alternatives can absorb — and
    #: nothing else in the system reports that.
    WITHDRAW_UNPLANNED = "withdraw_unplanned"

    @property
    def exits(self) -> bool:
        """True when this action leaves the auction. Four of the six do."""
        return self in _EXITING

    @property
    def arranges_care(self) -> bool:
        """True when the exit committed to something. ``WITHDRAW_UNPLANNED`` did not.

        The predicate the reward layer needs: only an exit that arranged something may be
        credited with having arranged something.
        """
        return self.exits and self is not QAction.WITHDRAW_UNPLANNED

    @property
    def action(self) -> "Action":
        """The bid mechanic this decision reduces to."""
        return Action.WITHDRAW if self.exits else Action.INCREASE_BID


_EXITING = frozenset(
    {
        QAction.WITHDRAW_ALTERNATIVE,
        QAction.AWAIT_NEXT_RESOURCE,
        QAction.RE_ENTER_LATER,
        QAction.WITHDRAW_UNPLANNED,
    }
)


class CareNeed(str, Enum):
    """Needs matched against a unit's capability vector. D.7; rule table BUILD_SPEC 5.2."""

    VENTILATION = "ventilation"
    VASOPRESSORS = "vasopressors"
    ONE_TO_ONE_NURSING = "one_to_one_nursing"
    CONTINUOUS_MONITORING = "continuous_monitoring"


class BudgetSource(str, Enum):
    """Whether a budget row was seeded or produced by the shift formula.

    Seeding with ``Base`` rather than RL-Steps' declared 1000/800/700 keeps shift 0 and
    shift 1 continuous; mixing the two produces a ~12x cliff at the first recompute.
    """

    SEED = "seed"
    COMPUTED = "computed"


# --------------------------------------------------------------------------------------
# Signals and scoring
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signal:
    """A normalised value in ``[0, 1]``, or an explicit absence.

    ``source`` records where the number came from so a stored utility can be re-derived, and
    so BUILD_SPEC's provenance distinction (live data / config / governance / model / our
    choice) survives into the log.
    """

    value: float | None
    source: str
    note: str = ""

    def __post_init__(self) -> None:
        if self.value is not None and not (0.0 <= self.value <= 1.0):
            raise ValueError(
                f"Signal from {self.source!r} must be normalised to [0, 1], got {self.value!r}"
            )

    @property
    def present(self) -> bool:
        return self.value is not None

    @classmethod
    def absent(cls, source: str, note: str = "") -> Signal:
        """Construct a missing input. Never substitute 0.0 for this."""
        return cls(value=None, source=source, note=note)


@dataclass(frozen=True, slots=True)
class FactorScore:
    """One weighted factor inside a component, e.g. Clinical Benefit's ICU benefit at .25."""

    name: str
    weight: float
    signal: Signal

    @property
    def present(self) -> bool:
        return self.signal.present


@dataclass(frozen=True, slots=True)
class ComponentScore:
    """What a component returns: a normalised score plus the factors behind it.

    Components do not know their own cap and never multiply by one — the engine applies the
    cap from the resource's ``caps_<resource>.yaml``, so a cap change is a config change
    and never a code change.
    """

    normalised: Signal
    coverage: float
    factors: tuple[FactorScore, ...] = ()

    def __post_init__(self) -> None:
        if not (0.0 <= self.coverage <= 1.0):
            raise ValueError(f"coverage must be in [0, 1], got {self.coverage!r}")


@dataclass(frozen=True, slots=True)
class ComponentResult:
    """A component's contribution in points, after the cap is applied by the engine."""

    component: ComponentName
    cap: float
    points: float
    coverage: float
    factors: tuple[FactorScore, ...] = ()


@dataclass(frozen=True, slots=True)
class UtilityBreakdown:
    """A complete utility with every component retained.

    The breakdown, not just the total, is what BUILD_SPEC section 8.4 requires be logged:
    B.13 cap fitting needs per-component values on contested cases, and Fairness v3 needs the
    utility forgone by losers. Storing the total alone blocks both permanently.
    """

    candidate_id: str
    agent: AgentKind
    components: tuple[ComponentResult, ...]
    caps_version: str
    config_version: str
    computed_at: datetime

    @property
    def total(self) -> float:
        """``U = sum(points)``. Caps carry their sign, so this is a plain sum."""
        return sum(c.points for c in self.components)

    @property
    def coverage(self) -> Mapping[ComponentName, float]:
        return {c.component: c.coverage for c in self.components}


# --------------------------------------------------------------------------------------
# Raw data transfer objects — one per source table, ingest layer output
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VitalsReading:
    """One row of ``hospilot.vitals``. Live column names, not the documents' shorthand.

    ``BUILD_SPEC`` F-08: the documents write ``bp_sys``; the column is ``bp_systolic``.
    ``on_oxygen`` is B.1 — it does not exist in the table yet, so it is ``None`` until
    migration 092 is signed off, and NEWS2 reports reduced coverage rather than assuming
    room air.
    """

    recorded_at: datetime
    temperature: float | None = None
    pulse: float | None = None
    bp_systolic: float | None = None
    bp_diastolic: float | None = None
    spo2: float | None = None
    respiratory_rate: float | None = None
    gcs: float | None = None
    is_critical: bool | None = None
    on_oxygen: bool | None = None


@dataclass(frozen=True, slots=True)
class LabResult:
    """One row of ``hospilot.lab_results``. The timestamp column is ``reported_at`` (F-08)."""

    test_name: str
    result_value: float
    reported_at: datetime
    unit: str | None = None
    flag: str | None = None


@dataclass(frozen=True, slots=True)
class MedicationOrder:
    """One row of ``hospilot.pharmacy_orders``.

    Vasopressor status is derived by matching ``generic_name`` against the drug-class rule
    table (BUILD_SPEC 5.5 / F-14); there is no drug classification in the schema.
    """

    medication_name: str
    generic_name: str | None
    route: str | None
    status: str | None
    prescribed_at: datetime | None
    dispensed_at: datetime | None = None


# --------------------------------------------------------------------------------------
# Snapshot — the immutable per-round view
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HospitalState:
    """State shared by every bidder in a round. Feeds Resource Stress, Scarcity, Contention.

    Resource Stress reads none of the patient, so it is identical for all bidders and cannot
    change who wins (D.8) — it moves the absolute level, which matters for budget spend and
    the reserve price.

    **Unit-scoped, not ICU-scoped.** ``unit`` names which unit's beds the counts below
    describe; every other field is read relative to it. The fields were once ``icu_*``, which
    meant a ward-bed auction had to put ward beds in a field named ``icu_total_beds``. Values
    match the unit vocabulary in ``config/rules/units.yaml`` (``icu``, ``hdu``, ``pacu``,
    ``resus``, ``ed``, ``ward``) — a plain ``str`` rather than an enum because that taxonomy
    is a signed-off config table, not a code constant, exactly like ``Candidate.current_unit``.

    Consumers that take ``occupancy`` as a float — reserve price, contention, budget spend —
    read it through the property and are insulated from the field names entirely.
    """

    unit: str
    unit_total_beds: int
    unit_occupied_beds: int
    predicted_demand_4h: float | None
    expected_discharges_4h: float | None
    boarding_count: int | None
    lwbs_risk: float | None
    active_isolation_cases: int | None

    @property
    def occupancy(self) -> float:
        if self.unit_total_beds <= 0:
            raise ValueError("unit_total_beds must be positive")
        return self.unit_occupied_beds / self.unit_total_beds

    @property
    def isolation_pressure(self) -> float | None:
        if self.active_isolation_cases is None or self.unit_total_beds <= 0:
            return None
        return self.active_isolation_cases / self.unit_total_beds


@dataclass(frozen=True, slots=True)
class Candidate:
    """A patient a department is bidding for."""

    candidate_id: str
    patient_token: str
    agent: AgentKind
    admission_id: str | None = None
    visit_id: str | None = None
    arrived_at: datetime | None = None
    current_unit: str | None = None
    condition_category: str | None = None
    severity_band: str | None = None
    needs: frozenset[CareNeed] = field(default_factory=frozenset)
    department_id: str | None = None


@dataclass(frozen=True, slots=True)
class PatientData:
    """Everything the ingest layer gathered for one candidate, unscored."""

    candidate: Candidate
    vitals: tuple[VitalsReading, ...] = ()
    labs: tuple[LabResult, ...] = ()
    orders: tuple[MedicationOrder, ...] = ()
    pending_nursing_tasks: int | None = None
    ward_nurses: int | None = None
    ot_cases_at_risk: int | None = None
    expected_los_days: float | None = None
    icu_day_rate: float | None = None
    best_alternative_unit: str | None = None
    #: Every unit that could take this patient instead, as free-text ``beds.ward`` strings.
    #: Alternative Availability now scores *relative to the unit being auctioned* — it drops
    #: the target unit and any escalation above it — so it needs candidates to choose from,
    #: not a single pre-picked one. ``best_alternative_unit`` remains the single-value
    #: shorthand and is read when this is empty.
    alternative_units: tuple[str, ...] = ()
    pacu_capacity_probability: float | None = None


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """One immutable read of the world, taken once per auction round.

    Every component in a round sees identical data. Without this, two components can read
    ``/icu/occupancy`` a second apart and disagree, and the resulting utility is not
    reproducible from the log — which makes B.13 cap fitting impossible.
    """

    snapshot_id: str
    taken_at: datetime
    hospital: HospitalState
    patients: Mapping[str, PatientData]
    caps_version: str
    config_version: str

    def for_candidate(self, candidate_id: str) -> PatientData:
        try:
            return self.patients[candidate_id]
        except KeyError as exc:
            raise KeyError(f"candidate {candidate_id!r} not in snapshot {self.snapshot_id!r}") from exc


# --------------------------------------------------------------------------------------
# Auction
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleaseEvent:
    """The thing that opens an auction.

    A real predicted discharge, a CDC row, or a hand-fired test query all become one of
    these. ``auction_key`` is derived from resource plus release-time bucket so a re-firing
    prediction cannot open two auctions on one bed (RL_STEPS_END_TO_END.md section 7).
    """

    event_id: str
    resource_type: ResourceType
    resource_id: str
    predicted_free_at: datetime
    detected_at: datetime
    source: TriggerSource
    mode: AuctionMode = AuctionMode.LIVE

    def auction_key(self, bucket_minutes: int) -> str:
        bucket = int(self.predicted_free_at.timestamp() // (bucket_minutes * 60))
        return f"{self.resource_type.value}:{self.resource_id}:{bucket}"


@dataclass(frozen=True, slots=True)
class ReentryTrigger:
    """The condition under which a withdrawn candidate automatically re-enters bidding.

    RL-Steps: *"Withdraw temporarily but continuously monitor conditions and automatically
    enter a future auction when circumstances change."*

    **A temporary exit that nothing watches is a permanent one.** The trigger is therefore a
    stored object with an owner and an expiry, not a flag on a bid — something outside the
    auction has to hold it between auctions and test it when the next bed is released. That
    is :class:`~allocation.pathway.reentry.ReentryRegistry`.

    ``expires_at`` is required for the same reason the utilities carry a TTL: a monitor armed
    against a patient's 13:00 physiology is not meaningful at 19:00, and an unexpiring trigger
    quietly re-enters a patient who has since been discharged.
    """

    candidate_id: str
    agent: AgentKind
    resource_type: ResourceType
    armed_at: datetime
    expires_at: datetime
    #: Re-enter if NEWS2 rises this far above ``baseline_news2``. The Ward example: the
    #: patient goes to HDU, deteriorates, and bidding re-opens.
    news2_rise: float | None = None
    #: Re-enter if the alternative unit this patient was held in stops being available.
    on_alternative_lost: bool = False
    baseline_news2: float | None = None
    #: Where the patient is held while the trigger is armed, if anywhere.
    holding_unit: str | None = None

    def __post_init__(self) -> None:
        if self.expires_at <= self.armed_at:
            raise ValueError("a re-entry trigger must expire after it is armed")
        if self.news2_rise is None and not self.on_alternative_lost:
            raise ValueError(
                "a re-entry trigger with no condition never fires, which makes it a plain "
                "withdrawal wearing a monitor's name. Set news2_rise or on_alternative_lost."
            )


@dataclass(frozen=True, slots=True)
class PathwayPlan:
    """What a withdrawal commits to — the ``+`` in *Withdraw + Alternative*.

    One type covers all three exits because RL-Steps' own re-entry example uses two of them at
    once: *"Ward moves patient temporarily to HDU. NEWS2 later deteriorates, causing the agent
    to automatically re-enter ICU bidding."* That is an alternative placement **and** a
    monitor. Splitting them into separate types would make the commonest real exit
    unrepresentable.
    """

    #: The unit the patient goes to instead. Required by ``WITHDRAW_ALTERNATIVE``.
    target_unit: str | None = None
    #: How long that unit is clinically acceptable for this patient — ``rules/units.yaml``.
    safe_hold_minutes: float | None = None
    #: When the next bed of this type is expected. Required by ``AWAIT_NEXT_RESOURCE``.
    expected_release_at: datetime | None = None
    #: Confidence in that release, ``[0, 1]``. RL-Steps' OT example: 35 minutes at 88 %.
    release_probability: float | None = None
    #: Required by ``RE_ENTER_LATER``.
    reentry: ReentryTrigger | None = None
    note: str = ""

    def __post_init__(self) -> None:
        p = self.release_probability
        if p is not None and not (0.0 <= p <= 1.0):
            raise ValueError(f"release_probability must be in [0, 1], got {p!r}")


@dataclass(frozen=True, slots=True)
class Decision:
    """What a policy returned: one of five valued actions, plus the plan it commits to.

    The invariants below are enforced here rather than by convention because each one, if
    broken, produces a log row that reads like a safe hand-off and describes an abandonment:

    1. **An exit must name its onward plan.** ``WITHDRAW_ALTERNATIVE`` without a target unit
       is not *Withdraw + Alternative*, it is a withdrawal — and it would collect
       ``safely_held``'s +10 for a pathway nobody activated.
    2. **A non-exit must not carry one.** A plan on a ``CONTINUE`` would be recorded as a
       commitment the agent never made.
    3. **The bid mechanic follows the Q-action**, never the other way round.

    ``q_values`` carries the estimate for every action the policy considered, not just the
    winner. Section 12 publishes both sides — ``Q(Continue) = 41`` against
    ``Q(Withdraw) = 58`` — and the losing estimate is what makes a decision auditable and a
    value function trainable. Storing only the argmax discards the label.
    """

    q_action: QAction
    action: Action
    alpha: float | None = None
    plan: PathwayPlan | None = None
    #: Estimated value per action considered. Empty for a rule-based policy that ranks
    #: nothing — an honest empty, not a fabricated set of scores.
    q_values: Mapping[QAction, float] = field(default_factory=dict)
    #: Actions that were available at all. ``WITHDRAW_ALTERNATIVE`` is infeasible when no
    #: alternative unit exists, and a policy must not be scored for failing to pick it.
    feasible: frozenset[QAction] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.q_action.exits:
            if self.action is not Action.WITHDRAW:
                raise ValueError(
                    f"{self.q_action.value} leaves the auction but carries action "
                    f"{self.action.value!r}"
                )
            plan = self.plan
            if self.q_action is QAction.WITHDRAW_UNPLANNED:
                if plan is not None:
                    raise ValueError(
                        "WITHDRAW_UNPLANNED is the exit that arranged nothing. Carrying a "
                        "PathwayPlan means something was arranged — use the action that "
                        "names it, or the log will under-report what the agent achieved."
                    )
                return
            if plan is None:
                raise ValueError(
                    f"{self.q_action.value} must carry a PathwayPlan. An exit with no onward "
                    "plan is an abandonment; record it as WITHDRAW_UNPLANNED rather than as "
                    "a strategic exit, which would credit it with an outcome nobody arranged."
                )
            if self.q_action is QAction.WITHDRAW_ALTERNATIVE and not plan.target_unit:
                raise ValueError(
                    "WITHDRAW_ALTERNATIVE must name the alternative unit it activates"
                )
            if self.q_action is QAction.AWAIT_NEXT_RESOURCE and (
                plan.expected_release_at is None or plan.release_probability is None
            ):
                raise ValueError(
                    "AWAIT_NEXT_RESOURCE must carry the expected release and its probability "
                    "— waiting for a bed nobody predicted is not a strategy"
                )
            if self.q_action is QAction.RE_ENTER_LATER and plan.reentry is None:
                raise ValueError("RE_ENTER_LATER must carry the ReentryTrigger that re-arms it")
        else:
            if self.action is Action.WITHDRAW:
                raise ValueError(
                    f"{self.q_action.value} stays in the auction but emits WITHDRAW"
                )
            if self.plan is not None:
                raise ValueError(
                    f"{self.q_action.value} stays in the auction and must not carry a "
                    "PathwayPlan — it would log a commitment that was never made"
                )

    @property
    def exits(self) -> bool:
        return self.q_action.exits

    @classmethod
    def compete(
        cls, q_action: QAction, action: Action, alpha: float | None, **kwargs: object
    ) -> "Decision":
        """A non-exiting decision. Rejects the three exits by construction."""
        if q_action.exits:
            raise ValueError(f"{q_action.value} is an exit; use Decision(...) with a plan")
        return cls(q_action=q_action, action=action, alpha=alpha, **kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class Bid:
    """One agent's position in one round. Losers and withdrawals are recorded too.

    RL_STEPS_END_TO_END.md sections 23-24: both a winning and a losing episode are needed,
    *"which is why the log must record the losers' bids and utilities, not only the
    winner's."*
    """

    auction_id: str
    round_index: int
    agent: AgentKind
    candidate_id: str
    action: Action
    amount: float
    utility: float
    ceiling: float
    alpha: float | None = None
    contention: float | None = None
    outcome_factor: float | None = None
    cost: float | None = None
    #: Which of the five decisions produced this row. ``None`` only for the synthetic rows
    #: :func:`~allocation.auction.state.standing_bids` builds to show a policy the leader
    #: board — those describe a position, not a decision, and inventing a Q-action for them
    #: would put rows in the training log that no policy ever emitted.
    q_action: QAction | None = None
    #: The onward plan, when this row is one of the three exits. This is what makes
    #: ``safely_held`` and ``second_bed_opened`` attributable to a decision.
    plan: PathwayPlan | None = None
    q_values: Mapping[QAction, float] = field(default_factory=dict)
    #: Which actions were available when this decision was taken. Carried onto the row so an
    #: evaluation can separate an exit the policy declined from one it never had.
    feasible: frozenset[QAction] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class RoundState:
    """What the policy is allowed to see. Agents observe the leading bid (section 5)."""

    auction_id: str
    round_index: int
    opened_at: datetime
    bids: tuple[Bid, ...]
    active_agents: frozenset[AgentKind]

    @property
    def highest_bid(self) -> float:
        live = [b.amount for b in self.bids if b.action is not Action.WITHDRAW]
        return max(live, default=0.0)

    @property
    def n_bidders(self) -> int:
        """Live bidder count — the ``n_bidders`` term of the contention formula."""
        return len(self.active_agents)


@dataclass(frozen=True, slots=True)
class BudgetState:
    """One row of ``allocation.agent_budget`` — a department's capacity for one shift.

    All four factors are stored, not just the product: a budget with no record of which
    factor moved it is unauditable and cannot be re-derived after a cap change
    (AGENT_BUDGET.md section 10).
    """

    agent: AgentKind
    shift_id: str
    shift_start: datetime
    shift_end: datetime
    base: float
    demand: float
    # RL-Steps section 4's four factors. Criticality is department-level and shift-invariant;
    # it is the one that differentiates departments now that Base is common to all of them.
    criticality: float
    fairness: float
    scarcity: float
    budget_total: float
    budget_remaining: float
    source: BudgetSource
    caps_version: str
    config_version: str
    # Tracked separately from ``budget_remaining`` because hourly recovery moves that number
    # too. Burn rate is ``spent / budget_total``, and burn rate is the health metric for the
    # whole mechanism (AGENT_BUDGET section 8) — deriving it from the remainder would hide
    # recovery entirely.
    spent: float = 0.0
    recovered: float = 0.0
    #: Per-factor provenance, e.g. ``{"scarcity": "/icu/occupancy 100%"}``.
    #:
    #: Stored for the same reason the factors themselves are: a budget of 1046.5 with no
    #: record of *which* factor moved it is unauditable — and a factor of 1.00 with no record
    #: of whether it was **computed** or **fell back** is unauditable one level down. Demand
    #: and Fairness both read 1.00 today, and neither is a measurement.
    factor_sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def burn_rate(self) -> float:
        """Fraction of the shift allowance consumed. Below 0.4 the budget is inert."""
        if self.budget_total <= 0:
            return 0.0
        return self.spent / self.budget_total


# --------------------------------------------------------------------------------------
# Seams — the protocols that let layers be swapped
# --------------------------------------------------------------------------------------


class Component(Protocol):
    """One utility component. The engine applies the cap; the component never does.

    ``agent`` is in the signature because Operational (D.5) and Waiting/Delay (D.3) are
    defined per agent kind.
    """

    name: ComponentName

    def score(
        self, candidate: Candidate, snapshot: FeatureSnapshot, agent: AgentKind
    ) -> ComponentScore: ...


class BiddingPolicy(Protocol):
    """Emits an action and, when increasing, an aggression factor ``alpha`` in ``[0, 1]``.

    ``Increment = alpha * (Ceiling - CurrentBid)`` (section 6). The policy never emits a
    point value directly. ``heuristic`` ships first and stays as the regression baseline;
    ``rl`` swaps in behind this same interface and nothing outside ``policy/`` may know which
    is running.

    **This is the narrow seam.** It can only express the bid mechanics, so a policy reached
    through it can never choose one of the three strategic exits — see :class:`StrategicPolicy`,
    which the auction prefers when a policy implements it. Kept because it is what the whole
    test suite and the section 18 regression are written against, and because a policy that
    genuinely has no onward plan should say so rather than fabricate one.
    """

    def decide(
        self,
        candidate: Candidate,
        utility: UtilityBreakdown,
        ceiling: float,
        round_state: RoundState,
        budget: BudgetState,
        snapshot: FeatureSnapshot,
    ) -> tuple[Action, float | None]: ...


class StrategicPolicy(Protocol):
    """The full five-action seam. RL-Steps' closing table.

    Everything :class:`BiddingPolicy` can say plus the three exits, each carrying the plan it
    commits to. :func:`~allocation.auction.rounds.run_round` prefers this method when a policy
    defines it and falls back to ``decide`` otherwise, so both kinds of policy run through one
    auction implementation.

    ``pathways`` supplies what the exits need and the auction layer must not compute: which
    alternative units are open to this patient, and when the next bed of this type is expected.
    Passing it in keeps the availability question in ``pathway/`` — a policy that decided for
    itself whether HDU was free would be reading the world twice, and could disagree with the
    Alternative Availability component that already scored it.
    """

    def decide_q(
        self,
        candidate: Candidate,
        utility: UtilityBreakdown,
        ceiling: float,
        round_state: RoundState,
        budget: BudgetState,
        snapshot: FeatureSnapshot,
        pathways: "PathwayOptions",
    ) -> Decision: ...


class PathwayOptions(Protocol):
    """What the three exits need to know, read once per round by the auction layer."""

    #: Alternative units this patient could go to instead, best first. Empty means
    #: ``WITHDRAW_ALTERNATIVE`` is infeasible, not that it is unattractive.
    alternatives: Sequence["AlternativeOption"]
    #: When the next bed of the auctioned type is expected, and how confident that is.
    next_release_at: datetime | None
    next_release_probability: float | None
    #: How long this patient can safely wait at all, from the clinical ladder.
    safe_wait_minutes: float | None


@dataclass(frozen=True, slots=True)
class AlternativeOption:
    """One clinically acceptable unit other than the one being auctioned.

    ``capability_gap`` is what the unit cannot do that the auctioned one can — an HDU with no
    ventilator is an alternative for a patient who does not need one and not for a patient who
    does. A gap that matters makes the option unavailable rather than merely worse: this is a
    clinical eligibility question, and ranking an infeasible unit second-best would let a
    policy pick it.
    """

    unit: str
    safe_hold_minutes: float
    #: **Tri-state, like every other signal here.** ``None`` means nobody read this unit's
    #: occupancy — which is the state in production today, because ``FeatureSnapshot`` holds
    #: only the unit being auctioned. Absent must not become ``True``: an alternative assumed
    #: free is how a patient gets withdrawn into a full HDU. :attr:`usable` therefore requires
    #: an explicit ``True``, so ``WITHDRAW_ALTERNATIVE`` stays infeasible until someone
    #: actually looks (see ``pathway.alternatives.unit_reader``).
    available: bool | None
    capability_gap: frozenset[CareNeed] = field(default_factory=frozenset)
    source: str = ""

    @property
    def usable(self) -> bool:
        """Clinically adequate **and** confirmed open. Unknown is not open."""
        return self.available is True and not self.capability_gap


class DataSource(Protocol):
    """The only seam onto the hospital database and forecast endpoints.

    A fixture implementation serves BUILD_SPEC's Appendix C rows so the whole engine runs
    with no dependency on the internal framework; the Hasura implementation replaces it
    later without touching any layer above ``ingest/``.
    """

    async def hospital_state(self, unit: str, at: datetime) -> HospitalState:
        """The state of ``unit``'s beds at ``at``.

        ``unit`` is not a hint. An implementation that cannot describe this unit must
        **raise**, never substitute another unit's beds: occupancy sets the reserve price,
        contention, scarcity and every budget factor, so a ward-bed auction answered with
        ICU's 20/20 produces a full ladder of plausible numbers, all of them about a
        different unit. Returning a state whose ``unit`` is not the one asked for is a bug
        the ingest layer checks for (:func:`allocation.ingest.snapshot.build_snapshot`).
        """
        ...

    async def patient_data(self, candidate: Candidate, at: datetime) -> PatientData: ...
