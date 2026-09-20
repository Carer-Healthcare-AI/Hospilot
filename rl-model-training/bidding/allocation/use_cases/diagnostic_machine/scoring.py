"""Scoring a diagnostic request into the eight components of :mod:`.utility`.

Every function returns a score in ``[0, 1]``; the caps carry the points and the signs, exactly
as the bed engine does. Keeping that split means the caps table stays the single place a
magnitude can be changed, and a component can never quietly award itself more than its cap.

**Two things here are structurally different from the bed components, and both come straight
from the framework:**

*Urgency is about the answer, not the patient.* The bed family asks how sick someone is;
this asks how fast the diagnostic answer stops being able to change what anyone does. A
stable patient with a 30-minute question outranks a sicker one whose scan can wait four
hours, and that inversion is intended.

*Alternative Quality is scored against the clinical question, not against a ladder.* There is
no universal ordering of modalities — see ``DiagnosticRequest.modality_yields``.

**Nothing below is fitted.** The shapes are linear and the reference points are choices, named
in each docstring. They are the framework's structure with plausible numbers, which is what
lets the mechanism run before clinical governance has supplied real ones — not a claim that
these are the real ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from allocation.features.scale import clamp
from allocation.use_cases.diagnostic_machine import capacity
from allocation.use_cases.diagnostic_machine.contracts import (
    DiagnosticMachineState,
    DiagnosticModality,
    DiagnosticRequest,
    MachineStatus,
)
from allocation.use_cases.diagnostic_machine.profiles import ModalityProfile
from allocation.use_cases.diagnostic_machine.utility import (
    DiagnosticComponent,
    DiagnosticUtility,
)

#: A safe-delay window this short scores full urgency. Chosen, not measured: it is roughly the
#: window a hyperacute stroke or suspected aortic dissection question runs on, and those are
#: the questions the mechanism must not let lose an auction.
FULL_URGENCY_MINUTES = 30.0

#: Transport this long scores the full (negative) burden. Chosen: an hour of a nurse and a
#: monitored trolley is the point at which moving the patient is itself the clinical event.
FULL_TRANSPORT_MINUTES = 60.0


@dataclass(frozen=True, slots=True)
class DiagnosticContext:
    """World inputs a request cannot know about itself.

    Every field is optional and every one has a stated fallback, because the alternative —
    scoring an absent input as zero — is the specific failure the bed engine's "drop and
    renormalise" rule exists to prevent. A component with no data must not push the utility
    down as if it had measured something.
    """

    #: Downstream flow this answer unblocks: a theatre list that cannot start, a discharge
    #: that cannot happen. ``None`` scores neutral, not zero.
    operational_impact: float | None = None
    #: Staffing, contrast, monitoring and equipment pressure on the modality right now.
    resource_stress: float | None = None
    #: Requests already queued for this modality, by agent. Used for nothing yet except
    #: making the queue visible to a policy that wants it.
    queue_depth: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("operational_impact", "resource_stress"):
            value = getattr(self, name)
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1] or None")


# -- the eight components ------------------------------------------------------------------


def diagnostic_value(request: DiagnosticRequest, modality: DiagnosticModality) -> float:
    """Yield x probability of management impact x importance of that impact.

    The framework's §12 decomposition, and the one component that is a product rather than a
    weighted sum. The distinction it encodes is *diagnostic accuracy is not operational
    value*: a highly accurate test that changes nothing scores low here, and should.
    """
    return clamp(
        request.yield_for(modality)
        * request.management_impact_probability
        * request.management_impact_importance
    )


def urgency(request: DiagnosticRequest) -> float:
    """How harmful it is to delay the ANSWER, from the safe diagnostic delay.

    Linear from ``FULL_URGENCY_MINUTES`` (score 1.0) to the request's own horizon. A window
    at or below the reference is fully urgent; one several hours long is not.
    """
    window = request.safe_delay.total_seconds() / 60.0
    if window <= FULL_URGENCY_MINUTES:
        return 1.0
    return clamp(FULL_URGENCY_MINUTES / window)


def delay_pressure(request: DiagnosticRequest, now: datetime) -> float:
    """How much of the safe delay has already been consumed.

    Separate from urgency on purpose. Urgency is a property of the question and does not move;
    delay pressure rises every round the request goes unserved, and it is what makes a
    long-waiting routine request eventually outbid a fresh urgent one. Without it the queue
    starves its own tail.
    """
    window = request.safe_delay.total_seconds()
    if window <= 0:
        return 1.0
    consumed = (now - request.requested_at).total_seconds()
    return clamp(consumed / window)


def operational_impact(context: DiagnosticContext) -> float:
    """Downstream flow the answer unblocks. Absent reads neutral (0.5), never 0."""
    return 0.5 if context.operational_impact is None else clamp(context.operational_impact)


def machine_scarcity(
    machines: Sequence[DiagnosticMachineState],
    request: DiagnosticRequest,
    now: datetime,
    horizon: timedelta,
) -> float:
    """Fraction of this modality's capacity already committed over the horizon.

    Delegates to :mod:`~allocation.use_cases.diagnostic_machine.capacity`, which is the single
    implementation of this arithmetic — see that module for why there is only one.
    """
    return capacity.scarcity(machines, request, now, horizon)


def alternative_quality(
    request: DiagnosticRequest, modality: DiagnosticModality
) -> float:
    """How well another modality answers the same question, relative to this one.

    Scored as the best alternative's yield over the requested modality's, so a substitute that
    is as good scores 1.0 and drives the full -20 — the request should not fight for a scanner
    when something equivalent is free. A substitute that answers half the question scores 0.5.

    Capped at 1.0 rather than allowed to exceed it. An alternative *better* than the requested
    modality is a fact about the request being wrong, not about this auction, and letting it
    run past the cap would express that as a bid rather than as the referral error it is.
    """
    best = request.best_alternative_modality(exclude=modality)
    if best is None:
        return 0.0
    requested = request.yield_for(modality)
    if requested <= 0.0:
        return 1.0
    return clamp(best[1] / requested)


def transport_burden(request: DiagnosticRequest, profile: ModalityProfile) -> float:
    """Cost and risk of moving the patient to the machine.

    A portable modality scores zero regardless of distance: the machine comes to the patient,
    so there is no transport to burden anyone. That is the whole reason portability is a
    profile field and not a comment.
    """
    if profile.portable:
        return 0.0
    return clamp(request.transport_minutes / FULL_TRANSPORT_MINUTES)


def resource_stress(
    context: DiagnosticContext, machines: Sequence[DiagnosticMachineState]
) -> float:
    """Staffing, contrast, monitoring and equipment pressure.

    With no measurement supplied, falls back to the fraction of this modality's machines that
    are out of service or in maintenance — which is a genuine stress signal the machine states
    already carry, rather than a placeholder constant.
    """
    if context.resource_stress is not None:
        return clamp(context.resource_stress)
    if not machines:
        return 1.0
    down = sum(
        1
        for m in machines
        if m.status in {MachineStatus.MAINTENANCE, MachineStatus.OUT_OF_SERVICE}
    )
    return clamp(down / len(machines))


# -- assembly ------------------------------------------------------------------------------


def score_request(
    request: DiagnosticRequest,
    profile: ModalityProfile,
    machines: Sequence[DiagnosticMachineState],
    now: datetime,
    context: DiagnosticContext | None = None,
    caps: Mapping[DiagnosticComponent, float] | None = None,
) -> DiagnosticUtility:
    """Score one request against one modality, producing a capped utility.

    ``caps`` defaults to :data:`~allocation.use_cases.diagnostic_machine.utility.INITIAL_ASSUMED_CAPS`;
    pass the modality's table from ``config.for_modality(...)`` to score against the file the
    audit row will name.
    """
    context = context or DiagnosticContext()
    horizon = timedelta(hours=profile.allocation_horizon_hours)
    modality = profile.modality

    scores = {
        DiagnosticComponent.DIAGNOSTIC_VALUE: diagnostic_value(request, modality),
        DiagnosticComponent.URGENCY: urgency(request),
        DiagnosticComponent.DELAY_PRESSURE: delay_pressure(request, now),
        DiagnosticComponent.OPERATIONAL_IMPACT: operational_impact(context),
        DiagnosticComponent.MACHINE_SCARCITY: machine_scarcity(machines, request, now, horizon),
        DiagnosticComponent.ALTERNATIVE_QUALITY: alternative_quality(request, modality),
        DiagnosticComponent.TRANSPORT_BURDEN: transport_burden(request, profile),
        DiagnosticComponent.RESOURCE_STRESS: resource_stress(context, machines),
    }
    if caps is None:
        return DiagnosticUtility(scores=scores)
    return DiagnosticUtility(scores=scores, caps=dict(caps))


def caps_from_config(config_caps: Mapping[str, object]) -> dict[DiagnosticComponent, float]:
    """The modality's caps table as a component mapping.

    Reads the same ``components: {name: {cap: x}}`` shape the bed caps files use, so a
    diagnostic caps file is not a second format to maintain.
    """
    components = config_caps["components"]
    assert isinstance(components, dict)
    missing = [c.value for c in DiagnosticComponent if c.value not in components]
    if missing:
        raise KeyError(f"caps table is missing diagnostic components: {missing}")
    return {
        component: float(components[component.value]["cap"])
        for component in DiagnosticComponent
    }
