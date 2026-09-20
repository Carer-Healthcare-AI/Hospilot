"""The deterministic diagnostic bidding policy, and the decision it returns.

**The bid arithmetic lives in this package's own** :mod:`~allocation.use_cases.diagnostic_machine.ladder`,
which re-implements the four rules the bed policy runs. The bed execution path is frozen, so
the rules are copied rather than extracted — see that module for what the duplication costs
and which test holds the two in step. What *this* module adds is the diagnostic vocabulary the
ladder does not own: which exit a withdrawal is, and what that exit commits the request to.

**``DiagnosticPathwayAction`` and ``Action`` stay separate all the way through.** The ladder
returns *leave / hold / raise*; :class:`DiagnosticDecision` records both what the request
does (``pathway``) and what the auction does (``action``), and the second is derived from the
first rather than stored independently. Collapsing them would make an abandonment and a
handover to ultrasound the same row in the log, which is the precise thing the six-action
space exists to prevent.

This policy runs first, generates the log that makes training possible, and then stays as the
regression baseline — the same role ``HeuristicPolicy`` plays for beds. It ranks nothing:
:attr:`DiagnosticDecision.action_values` stays empty rather than carrying invented scores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from allocation.config import Config
from allocation.contracts import Action, AgentKind, BudgetState, RoundState
from allocation.use_cases.diagnostic_machine.config import diagnostic_rules
from allocation.use_cases.diagnostic_machine.ladder import Verdict, climb
from allocation.use_cases.diagnostic_machine.contracts import (
    DiagnosticMachineState,
    DiagnosticModality,
    DiagnosticPathwayAction,
    DiagnosticRequest,
)
from allocation.use_cases.diagnostic_machine.profiles import ModalityProfile


@dataclass(frozen=True, slots=True)
class DiagnosticPlan:
    """What an exiting request committed to.

    The diagnostic counterpart of ``PathwayPlan``, and pointedly not the same shape: a bed
    exit names a *unit* the patient moves to, a diagnostic exit names a *modality* that
    answers the question, a *time* capacity appears, or a *condition* that re-opens bidding.
    """

    alternative_modality: DiagnosticModality | None = None
    #: Yield of that alternative for this clinical question. Recorded so the log says how much
    #: diagnostic value the handover actually preserved, not merely that one happened.
    alternative_yield: float | None = None
    expected_capacity_at: datetime | None = None
    capacity_probability: float | None = None
    #: What would put this request back in an auction. A "temporary" exit nothing watches is a
    #: permanent one.
    reentry_condition: str | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class DiagnosticDecision:
    """One policy decision: what the request does, and what the auction does.

    The invariants are enforced here rather than by convention because each one, if broken,
    produces a row that reads like an arranged pathway and describes an abandonment.
    """

    pathway: DiagnosticPathwayAction
    alpha: float | None = None
    plan: DiagnosticPlan | None = None
    #: Estimated value per pathway action considered. Empty for a rule-based policy that ranks
    #: nothing — an honest empty, not a fabricated set of scores.
    action_values: Mapping[DiagnosticPathwayAction, float] = field(default_factory=dict)
    #: Which actions were available at all, so an evaluation can separate an exit the policy
    #: declined from one it never had.
    feasible: frozenset[DiagnosticPathwayAction] = field(default_factory=frozenset)
    #: Set only when the ladder said hold: at the ceiling with nothing left to expose. Still a
    #: ``CONTINUE`` — holding is a form of continuing, not of winning now.
    holds: bool = False

    def __post_init__(self) -> None:
        if self.pathway.exits:
            plan = self.plan
            if self.pathway is DiagnosticPathwayAction.WITHDRAW_UNPLANNED:
                if plan is not None:
                    raise ValueError(
                        "WITHDRAW_UNPLANNED is the exit that arranged nothing; carrying a "
                        "plan would credit it with a pathway nobody activated"
                    )
                return
            if plan is None:
                raise ValueError(
                    f"{self.pathway.value} must carry a DiagnosticPlan. An exit with no "
                    "onward plan is an abandonment; record it as WITHDRAW_UNPLANNED"
                )
            if (
                self.pathway is DiagnosticPathwayAction.USE_ALTERNATIVE
                and plan.alternative_modality is None
            ):
                raise ValueError("USE_ALTERNATIVE must name the modality it hands over to")
            if self.pathway is DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY and (
                plan.expected_capacity_at is None or plan.capacity_probability is None
            ):
                raise ValueError(
                    "AWAIT_NEXT_CAPACITY must carry the expected capacity and its "
                    "probability — waiting for a slot nobody predicted is not a strategy"
                )
            if (
                self.pathway is DiagnosticPathwayAction.RE_ENTER_LATER
                and not plan.reentry_condition
            ):
                raise ValueError("RE_ENTER_LATER must carry the condition that re-arms it")
        elif self.plan is not None:
            raise ValueError(
                f"{self.pathway.value} stays in the auction and must not carry a plan — it "
                "would log a commitment that was never made"
            )

    @property
    def action(self) -> Action:
        """The bid mechanic this decision reduces to. Derived, never stored twice."""
        if self.holds:
            return Action.HOLD
        return self.pathway.auction_action

    @property
    def exits(self) -> bool:
        return self.pathway.exits


@dataclass(frozen=True, slots=True)
class DiagnosticOptions:
    """What the strategic exits need, rebuilt every round.

    Re-read each round for the same reason utilities are: a machine can fill between round 1
    and round 3, and an exit taken against round 1's availability would hand the request to a
    slot that is no longer there.
    """

    request: DiagnosticRequest
    profile: ModalityProfile
    now: datetime
    #: When this modality could next take the request, and how confident that is. ``None``
    #: means no start exists inside both the operating window and the clinically useful one.
    next_capacity_at: datetime | None = None
    next_capacity_probability: float | None = None
    #: Machines of OTHER modalities, so "use an alternative" can be checked against real
    #: capacity rather than against the alternative merely existing on paper.
    alternative_machines: Sequence[DiagnosticMachineState] = ()
    #: Whether the patient can be monitored while the request stands down.
    monitorable: bool = True
    #: How much of the question an alternative must answer to count as a handover.
    #: Supplied by the caller from ``config/pathway.yaml`` so the threshold is versioned
    #: with the run rather than compiled into this module.
    min_yield_ratio: float = 0.8

    @property
    def safe_wait_minutes(self) -> float:
        """How long the ANSWER can still wait. Drives the WIN_NOW / CONTINUE split."""
        return self.request.safe_delay_remaining(self.now).total_seconds() / 60.0

    def best_alternative(self) -> tuple[DiagnosticModality, float] | None:
        """The best alternative modality that actually answers the question, in time.

        Three filters, and the last two are what stop this becoming a ladder:

        1. **It must have capacity inside the useful window.** An alternative with no free
           slot is not an alternative, and recording a handover to it logs a pathway that
           cannot happen.
        2. **It must answer enough of the question** — ``min_yield_ratio``, from
           ``config/pathway.yaml``. A modality that answers a fraction of the question is not
           a substitute for one that answers it.
        3. Ties break on yield, then on name, so the choice is deterministic.

        Note the deliberate difference from
        :func:`~allocation.use_cases.diagnostic_machine.scoring.alternative_quality`, which
        scores the *raw* ratio with no threshold. That is correct there: a partial alternative
        genuinely does reduce how hard this request should fight for the scanner. It is not
        correct here, where the question is whether the request can *leave*. Willingness to
        bid and sufficiency to hand over are different questions and take different rules.
        """
        ranked = sorted(
            (
                (modality, self.request.yield_for(modality))
                for modality in self.request.eligible_modalities
                if modality is not self.profile.modality
            ),
            key=lambda item: (-item[1], item[0].value),
        )
        requested = self.request.yield_for(self.profile.modality)
        for modality, value in ranked:
            if value <= 0.0:
                continue
            if requested > 0.0 and value / requested < self.min_yield_ratio:
                continue
            machines = [m for m in self.alternative_machines if m.modality is modality]
            if any(m.earliest_start(self.request, self.now) is not None for m in machines):
                return modality, value
        return None


class DiagnosticHeuristicPolicy:
    """Rule-based aggression for diagnostic capacity.

    Reads its alpha parameters from the same ``auction.yaml`` block the bed policy does. That
    is deliberate: the ladder is resource-neutral, so its tuning is too, and giving diagnostics
    its own copy of ``opening_alpha`` would let the two families' bids drift apart for no
    stated reason.
    """

    name = "diagnostic_heuristic"

    def __init__(self, config: Config) -> None:
        # Alpha parameters come from the shared auction table: the ladder is resource-neutral
        # arithmetic, so its tuning is too, and a second copy of `opening_alpha` would let the
        # two families' bids drift for no stated reason.
        cfg = config.auction["policy"]["heuristic"]
        self._opening = cfg["opening_alpha"]
        self._lead_alpha = float(cfg["lead_alpha"])
        self._margin = float(cfg["overtake_margin"])
        self._config = config

        # Everything CLINICAL comes from the diagnostic table. Never `config.rule("pathway")`
        # — that is the bed file, and a diagnostic run must not load bed configuration.
        rules = diagnostic_rules()
        self.rules_version = str(rules.get("version", "unversioned"))
        self._min_capacity_p = float(rules["next_capacity"]["min_probability"])
        self._min_yield_ratio = float(rules["alternative"]["min_yield_ratio"])
        self._reentry_below = float(rules["reentry"]["safe_delay_below_minutes"])
        compete = rules.get("compete", {})
        self._win_now_below = compete.get("win_now_below_minutes")
        floor = compete.get("win_now_alpha_floor")
        self._win_now_floor = None if floor is None else float(floor)

    def decide(
        self,
        agent: AgentKind,
        ceiling: float,
        round_state: RoundState,
        budget: BudgetState,
        options: DiagnosticOptions,
        **context: object,
    ) -> DiagnosticDecision:
        """One of six pathway actions, with the plan the exits commit to.

        ``context`` carries the utility, contention and round budget a learned policy needs to
        encode its state. This policy ignores them — its rules read the bid board and the
        clinical window, nothing else — and accepts them so both kinds of policy sit behind
        one seam.
        """
        del context
        feasible = self._feasible(options)
        outcome = climb(
            self._config,
            agent=agent,
            ceiling=ceiling,
            round_state=round_state,
            budget=budget,
            opening_alpha=self._opening_alpha(agent),
            lead_alpha=self._lead_alpha,
            overtake_margin=self._margin,
        )

        if outcome.verdict is Verdict.EXIT:
            return self._exit(options, feasible, outcome.reason)
        if outcome.verdict is Verdict.HOLD:
            return DiagnosticDecision(
                pathway=DiagnosticPathwayAction.CONTINUE, feasible=feasible, holds=True
            )
        return self._compete(outcome.alpha, options, feasible)

    # -- compete: WIN_NOW or CONTINUE ------------------------------------------------------

    def _compete(
        self,
        alpha: float | None,
        options: DiagnosticOptions,
        feasible: frozenset[DiagnosticPathwayAction],
    ) -> DiagnosticDecision:
        """Label the aggression regime by how long the answer can still wait.

        Same discriminator the bed policy uses, asked of the diagnostic question rather than
        of the patient: ``WIN_NOW`` when delay is dangerous, ``CONTINUE`` when immediate
        acquisition is not essential.
        """
        wait = options.safe_wait_minutes
        threshold = self._win_now_below
        urgent = True if threshold is None else wait < float(threshold)

        if not urgent:
            return DiagnosticDecision(
                pathway=DiagnosticPathwayAction.CONTINUE, alpha=alpha, feasible=feasible
            )
        if self._win_now_floor is not None and alpha is not None:
            alpha = max(alpha, self._win_now_floor)
        return DiagnosticDecision(
            pathway=DiagnosticPathwayAction.WIN_NOW, alpha=alpha, feasible=feasible
        )

    # -- exit: which of the four -----------------------------------------------------------

    def _exit(
        self,
        options: DiagnosticOptions,
        feasible: frozenset[DiagnosticPathwayAction],
        reason: str,
    ) -> DiagnosticDecision:
        """Choose the exit, ordered by how much of the clinical question it still answers.

        1. **An alternative modality with real capacity.** The question gets answered, by
           something else, in time. That is a resolution rather than a deferral — and unlike
           the bed ladder, it is not a step *down*: ultrasound may be the better answer.
        2. **Predicted capacity on this modality inside the safe delay.** Nothing else answers
           the question, but this machine frees up while the answer still matters.
        3. **Monitored stand-down.** Neither, but the patient can be watched and the request
           re-armed if the question becomes more urgent.
        4. **Nothing.** ``WITHDRAW_UNPLANNED``, so it can never collect what an arranged exit
           earns.
        """
        best = options.best_alternative()
        if best is not None:
            modality, value = best
            return DiagnosticDecision(
                pathway=DiagnosticPathwayAction.USE_ALTERNATIVE,
                plan=DiagnosticPlan(
                    alternative_modality=modality,
                    alternative_yield=value,
                    note=(
                        f"{reason}; {modality.value} answers "
                        f"'{options.request.clinical_question}' at yield {value:.2f} "
                        "and has capacity in time"
                    ),
                ),
                feasible=feasible,
            )

        if DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY in feasible:
            return DiagnosticDecision(
                pathway=DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY,
                plan=DiagnosticPlan(
                    expected_capacity_at=options.next_capacity_at,
                    capacity_probability=options.next_capacity_probability,
                    note=(
                        f"{reason}; next {options.profile.modality.value} capacity at "
                        f"{options.next_capacity_at:%H:%M} "
                        f"p={options.next_capacity_probability:.2f}, inside the "
                        f"{options.safe_wait_minutes:.0f} min the answer can still wait"
                    ),
                ),
                feasible=feasible,
            )

        if DiagnosticPathwayAction.RE_ENTER_LATER in feasible:
            return DiagnosticDecision(
                pathway=DiagnosticPathwayAction.RE_ENTER_LATER,
                plan=DiagnosticPlan(
                    reentry_condition=(
                        "clinical deterioration, or safe diagnostic delay below "
                        f"{self._reentry_below:.0f} min"
                    ),
                    note=f"{reason}; nothing available — monitored, request stands by",
                ),
                feasible=feasible,
            )

        return DiagnosticDecision(
            pathway=DiagnosticPathwayAction.WITHDRAW_UNPLANNED, feasible=feasible
        )

    # -- feasibility -----------------------------------------------------------------------

    def _feasible(self, options: DiagnosticOptions) -> frozenset[DiagnosticPathwayAction]:
        """Which actions were available at all.

        Recorded on every decision because an evaluation that cannot separate *declined* from
        *unavailable* will read a policy that never had an alternative as one that never
        wanted one.
        """
        out = {
            DiagnosticPathwayAction.WIN_NOW,
            DiagnosticPathwayAction.CONTINUE,
            DiagnosticPathwayAction.WITHDRAW_UNPLANNED,
        }
        if options.best_alternative() is not None:
            out.add(DiagnosticPathwayAction.USE_ALTERNATIVE)

        probability = options.next_capacity_probability
        if (
            options.next_capacity_at is not None
            and probability is not None
            and probability >= self._min_capacity_p
            and options.next_capacity_at <= options.request.latest_useful_at
        ):
            out.add(DiagnosticPathwayAction.AWAIT_NEXT_CAPACITY)

        if options.monitorable and options.safe_wait_minutes > 0:
            out.add(DiagnosticPathwayAction.RE_ENTER_LATER)
        return frozenset(out)

    def _opening_alpha(self, agent: AgentKind) -> float:
        return float(self._opening.get(agent.value, self._opening["default"]))


# Both thresholds that used to live here are now in `config/pathway.yaml`, versioned.


def next_capacity(
    machines: Sequence[DiagnosticMachineState],
    request: DiagnosticRequest,
    now: datetime,
) -> tuple[datetime | None, float | None]:
    """Earliest start across this modality's machines, and how confident that is.

    The probability is 1.0 whenever a start exists, because it is read off a booked schedule
    rather than predicted: unlike a bed release, which is a forecast, a scanner's next free
    slot is arithmetic on intervals already committed. Returning a fabricated 0.7 to look like
    the bed pathway would be inventing uncertainty the schedule does not have.

    This is the framework's §19 point that ``AWAIT_NEXT_CAPACITY`` finally has real semantics:
    in the bed environment the action waited on a prediction, here it waits on a timetable.
    """
    starts = [
        start
        for start in (m.earliest_start(request, now) for m in machines)
        if start is not None
    ]
    if not starts:
        return None, None
    return min(starts), 1.0


def build_options(
    request: DiagnosticRequest,
    profile: ModalityProfile,
    machines: Sequence[DiagnosticMachineState],
    now: datetime,
    alternative_machines: Sequence[DiagnosticMachineState] = (),
    monitorable: bool = True,
    min_yield_ratio: float | None = None,
) -> DiagnosticOptions:
    """Assemble one round's options for one request."""
    at, probability = next_capacity(
        [m for m in machines if m.modality is profile.modality], request, now
    )
    if min_yield_ratio is None:
        min_yield_ratio = float(diagnostic_rules()["alternative"]["min_yield_ratio"])
    return DiagnosticOptions(
        request=request,
        profile=profile,
        now=now,
        next_capacity_at=at,
        next_capacity_probability=probability,
        alternative_machines=tuple(alternative_machines),
        monitorable=monitorable,
        min_yield_ratio=min_yield_ratio,
    )


def horizon_end(profile: ModalityProfile, opened_at: datetime) -> datetime:
    """End of the allocation horizon this modality scores outcomes over."""
    return opened_at + timedelta(hours=profile.allocation_horizon_hours)
