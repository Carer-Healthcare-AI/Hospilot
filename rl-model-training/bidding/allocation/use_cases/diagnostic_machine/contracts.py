"""Use-case contracts for diagnostic-machine allocation.

These types describe CT, MRI, X-ray, ultrasound, and future modalities without putting
machine schedules into the bed-oriented ``HospitalState`` contract.  Auction mechanics remain
the existing ``Action`` type; ``DiagnosticPathwayAction`` records what happens to the request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Mapping

from allocation.contracts import Action, AgentKind
from allocation.lifecycle import auction_exit_actions, lifecycle_errors, terminal_actions


class DiagnosticModality(str, Enum):
    CT = "ct"
    MRI = "mri"
    X_RAY = "x_ray"
    ULTRASOUND = "ultrasound"


class MachineStatus(str, Enum):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    RESERVED = "reserved"
    MAINTENANCE = "maintenance"
    OUT_OF_SERVICE = "out_of_service"


class DiagnosticPathwayAction(str, Enum):
    """What happens to the diagnostic request, separate from the bid mechanic."""

    WIN_NOW = "win_now"
    CONTINUE = "continue"
    USE_ALTERNATIVE = "use_alternative"
    AWAIT_NEXT_CAPACITY = "await_next_capacity"
    RE_ENTER_LATER = "re_enter_later"
    WITHDRAW_UNPLANNED = "withdraw_unplanned"

    @property
    def auction_action(self) -> Action:
        if self in {self.WIN_NOW, self.CONTINUE}:
            return Action.INCREASE_BID
        return Action.WITHDRAW

    @property
    def leaves_auction(self) -> bool:
        """True when this action stops the request bidding in the auction it is in now.

        Four of the six. **Not** the same question as terminality — see
        :attr:`terminates_request` and :mod:`allocation.lifecycle`.
        """
        return self.auction_action is Action.WITHDRAW

    @property
    def terminates_request(self) -> bool:
        """True when this action ends the request's life. Two of the six.

        ``simulator._honour_exits`` writes ``DIVERTED`` for ``USE_ALTERNATIVE`` and
        ``ABANDONED`` for ``WITHDRAW_UNPLANNED`` and drops both from ``pending``.
        ``AWAIT_NEXT_CAPACITY`` and ``RE_ENTER_LATER`` deliberately stay in the queue — "that is
        the whole difference between a strategic exit and an abandonment" — and go on making
        decisions. Measured over shift seeds 901-905 x rollouts 1-2: ``await_next_capacity`` was
        followed by a further decision for the same request in 745 of 837 takings.
        """
        return self in {self.USE_ALTERNATIVE, self.WITHDRAW_UNPLANNED}

    @property
    def exits(self) -> bool:
        """Deprecated spelling of :attr:`leaves_auction`, kept so existing callers still read."""
        return self.leaves_auction

    @property
    def arranges_diagnostic_pathway(self) -> bool:
        return self in {
            self.USE_ALTERNATIVE,
            self.AWAIT_NEXT_CAPACITY,
            self.RE_ENTER_LATER,
        }


#: The four actions that leave the current auction, terminal or not.
AUCTION_EXIT_ACTIONS: frozenset[DiagnosticPathwayAction] = auction_exit_actions(
    DiagnosticPathwayAction
)

#: The two actions that end a request's life. See
#: :attr:`DiagnosticPathwayAction.terminates_request`.
TERMINAL_ACTIONS: frozenset[DiagnosticPathwayAction] = terminal_actions(
    DiagnosticPathwayAction
)

_LIFECYCLE_PROBLEMS = lifecycle_errors(DiagnosticPathwayAction)
if _LIFECYCLE_PROBLEMS:  # pragma: no cover - a structural error, caught at import
    raise RuntimeError(
        "DiagnosticPathwayAction does not describe a lifecycle: "
        + "; ".join(_LIFECYCLE_PROBLEMS)
    )


@dataclass(frozen=True, slots=True)
class DiagnosticRequest:
    request_id: str
    patient_token: str
    agent: AgentKind
    clinical_question: str
    requested_procedure: str
    eligible_modalities: frozenset[DiagnosticModality]
    requested_at: datetime
    latest_useful_at: datetime
    estimated_duration: timedelta
    diagnostic_yield: float
    management_impact_probability: float
    management_impact_importance: float
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    alternative_procedures: tuple[str, ...] = ()
    transport_minutes: int = 0
    #: Yield **per modality for this clinical question** — the framework's §13, and the place
    #: the bed family's care ladder does not translate.
    #:
    #: Beds have one ordering: ICU is above HDU is above ward, for every patient. Imaging has
    #: no such ordering. "Suspected pulmonary embolism" is answered by CT and barely at all by
    #: MRI; "suspected cord compression" reverses that completely. A single ranked ladder of
    #: modalities would be wrong for half of all questions, so the ranking is carried by the
    #: question rather than by the resource.
    #:
    #: Empty means only the requested modality's yield is known, which is the honest state for
    #: a request nobody has scored alternatives for — :meth:`yield_for` then returns 0.0 for
    #: the others rather than inventing a substitute.
    modality_yields: Mapping[DiagnosticModality, float] = field(default_factory=dict)
    #: agent-extension (2026-08-27): True for scheduled/outpatient demand (APPOINTMENTS)
    #: that can genuinely be rebooked, as opposed to urgent unscheduled demand from a
    #: department already treating the patient. Defaults False, so every existing request
    #: (ER/ICU/OT) is completely unaffected — this is a new declared signal, not a
    #: reinterpretation of anything ``safe_delay``/``latest_useful_at`` already carry.
    reschedulable: bool = False

    def __post_init__(self) -> None:
        if not self.request_id or not self.clinical_question.strip():
            raise ValueError("a diagnostic request needs an id and clinical question")
        if not self.eligible_modalities:
            raise ValueError("a diagnostic request needs at least one eligible modality")
        if self.latest_useful_at <= self.requested_at:
            raise ValueError("latest_useful_at must be after requested_at")
        if self.estimated_duration <= timedelta(0):
            raise ValueError("estimated_duration must be positive")
        if self.transport_minutes < 0:
            raise ValueError("transport_minutes cannot be negative")
        for name, value in (
            ("diagnostic_yield", self.diagnostic_yield),
            ("management_impact_probability", self.management_impact_probability),
            ("management_impact_importance", self.management_impact_importance),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")

    @property
    def diagnostic_value(self) -> float:
        return (
            self.diagnostic_yield
            * self.management_impact_probability
            * self.management_impact_importance
        )

    def safe_delay_remaining(self, now: datetime) -> timedelta:
        return max(self.latest_useful_at - now, timedelta(0))

    @property
    def safe_delay(self) -> timedelta:
        """The whole window in which the answer is still clinically useful.

        The diagnostic replacement for the bed family's ``safe_hold_hours``, and a different
        question: not *how long can this patient wait for a resource* but *how long can the
        ANSWER wait before it stops changing anything*.
        """
        return self.latest_useful_at - self.requested_at

    def yield_for(self, modality: DiagnosticModality) -> float:
        """Diagnostic yield of ``modality`` for this clinical question.

        The requested modality's yield is :attr:`diagnostic_yield` whether or not it also
        appears in :attr:`modality_yields`; an unscored alternative is 0.0, never a guess.
        """
        if modality in self.modality_yields:
            return float(self.modality_yields[modality])
        if modality in self.eligible_modalities and not self.modality_yields:
            return self.diagnostic_yield
        return 0.0

    def best_alternative_modality(
        self, exclude: DiagnosticModality
    ) -> tuple[DiagnosticModality, float] | None:
        """The eligible modality other than ``exclude`` that best answers the question.

        Returns ``None`` when nothing else is eligible or nothing else has any yield — which
        is a real and common state, and is what makes "use an alternative" infeasible rather
        than merely unattractive.
        """
        others = [
            (modality, self.yield_for(modality))
            for modality in self.eligible_modalities
            if modality is not exclude
        ]
        scored = [(modality, value) for modality, value in others if value > 0.0]
        if not scored:
            return None
        return max(scored, key=lambda item: (item[1], item[0].value))


@dataclass(frozen=True, slots=True)
class AllocationInterval:
    machine_id: str
    request_id: str
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.ends_at <= self.starts_at:
            raise ValueError("an allocation interval must end after it starts")

    def overlaps(self, other: "AllocationInterval") -> bool:
        return self.starts_at < other.ends_at and other.starts_at < self.ends_at


@dataclass(frozen=True, slots=True)
class DiagnosticMachineState:
    machine_id: str
    modality: DiagnosticModality
    status: MachineStatus
    window_starts_at: datetime
    window_ends_at: datetime
    capabilities: frozenset[str] = field(default_factory=frozenset)
    allocations: tuple[AllocationInterval, ...] = ()
    setup_minutes: int = 0
    cleanup_minutes: int = 0

    def __post_init__(self) -> None:
        if self.window_ends_at <= self.window_starts_at:
            raise ValueError("machine availability window must have positive duration")
        if self.setup_minutes < 0 or self.cleanup_minutes < 0:
            raise ValueError("setup and cleanup minutes cannot be negative")
        ordered = sorted(self.allocations, key=lambda item: item.starts_at)
        if any(left.overlaps(right) for left, right in zip(ordered, ordered[1:])):
            raise ValueError("machine allocations cannot overlap")
        if any(item.machine_id != self.machine_id for item in self.allocations):
            raise ValueError("all allocations must belong to this machine")

    def is_eligible(self, request: DiagnosticRequest) -> bool:
        return (
            self.modality in request.eligible_modalities
            and request.required_capabilities <= self.capabilities
            and self.status not in {MachineStatus.MAINTENANCE, MachineStatus.OUT_OF_SERVICE}
        )

    def can_allocate(self, request: DiagnosticRequest, starts_at: datetime) -> bool:
        if not self.is_eligible(request):
            return False
        occupied_from = starts_at - timedelta(minutes=self.setup_minutes)
        occupied_until = (
            starts_at
            + request.estimated_duration
            + timedelta(minutes=self.cleanup_minutes)
        )
        proposed = AllocationInterval(
            machine_id=self.machine_id,
            request_id=request.request_id,
            starts_at=occupied_from,
            ends_at=occupied_until,
        )
        return (
            proposed.starts_at >= self.window_starts_at
            and proposed.ends_at <= self.window_ends_at
            and proposed.ends_at <= request.latest_useful_at
            and not any(proposed.overlaps(existing) for existing in self.allocations)
        )

    def interval_for(
        self, request: DiagnosticRequest, starts_at: datetime
    ) -> AllocationInterval:
        """The interval the machine is actually held for, setup and cleanup included.

        Booking a scanner for the nominal procedure length over-books it. The interval starts
        before the patient does and ends after they leave, and it is this — not the procedure
        — that must not overlap anything else.
        """
        return AllocationInterval(
            machine_id=self.machine_id,
            request_id=request.request_id,
            starts_at=starts_at - timedelta(minutes=self.setup_minutes),
            ends_at=starts_at + request.estimated_duration + timedelta(minutes=self.cleanup_minutes),
        )

    def earliest_start(
        self, request: DiagnosticRequest, not_before: datetime
    ) -> datetime | None:
        """First moment at or after ``not_before`` this request could start, or ``None``.

        Walks the gaps rather than sampling the clock: the only start times worth testing are
        ``not_before`` itself and the instant after each existing booking releases the
        machine. Anything between the two is either inside a booking or a later start with no
        advantage, so a scan of the boundaries is exhaustive.

        ``None`` is a real answer and the one that makes ``AWAIT_NEXT_CAPACITY`` honest — it
        means no start exists that still lands inside both the machine's operating window and
        the request's clinically useful window, so waiting for this machine achieves nothing.
        """
        if not self.is_eligible(request):
            return None
        setup = timedelta(minutes=self.setup_minutes)
        candidates = [max(not_before, self.window_starts_at + setup)]
        candidates += [
            max(not_before, booking.ends_at + setup)
            for booking in self.allocations
            if booking.ends_at >= not_before
        ]
        starts = sorted(set(candidates))
        return next((start for start in starts if self.can_allocate(request, start)), None)
