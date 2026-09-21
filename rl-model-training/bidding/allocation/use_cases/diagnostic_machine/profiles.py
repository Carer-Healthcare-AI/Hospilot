"""Modality profiles — what differs between CT, MRI, X-ray and ultrasound.

**Four modalities, one algorithm.** Everything below is data: who may bid, how long a
request's score stays valid, how many rounds the auction gets, how long a procedure and its
setup/cleanup take, and which caps and budget tables the modality is scored and funded
against. The auction, the ladder, the utility mathematics and the simulator are shared and
appear nowhere in this file. If adding a fifth modality (PET, mammography, DEXA) needs
anything other than a new call to :func:`modality_profile` and a new pair of config files,
the seam is in the wrong place.

**This registry is deliberately separate from ``allocation.use_cases.bed.profiles.REGISTRY``.** That one is
keyed by :class:`~allocation.contracts.ResourceType`, whose six values are a ``<unit>_bed``
vocabulary tied to ``units.yaml``; a CT scanner has no unit and sits on no care ladder.
Registering here keeps query resolution, ``GET /use-cases`` and the bed family exactly as they
were, which is the only way "preserve existing bed behavior" can be true rather than hoped
for.

**Caps and budget tables are per modality and have no defaults**, for the same reason the bed
family enforces it: utility points are only comparable within one caps table, so a modality
drawing on another's pool inherits a calibration that was never valid for it and nothing
downstream can detect the mistake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Mapping

from allocation.contracts import AgentKind
from allocation.use_cases.diagnostic_machine.contracts import DiagnosticModality
from allocation.use_cases.diagnostic_machine.utility import DiagnosticComponent

#: Every component applies to every modality. What changes between them is the *score*, not
#: which questions get asked — transport burden is small for a portable ultrasound and large
#: for an MRI, and that is a number, not a missing component.
DIAGNOSTIC_COMPONENTS: tuple[DiagnosticComponent, ...] = tuple(DiagnosticComponent)

#: Same round structure as the bed family: 3 rounds x 120 s. A diagnostic auction is opened
#: against a capacity window minutes away, so the clock budget is the same shape.
DIAGNOSTIC_MAX_ROUNDS = 3
DIAGNOSTIC_ROUND_SECONDS = 120
DIAGNOSTIC_AUCTION_KEY_BUCKET_MINUTES = 15


@dataclass(frozen=True, slots=True)
class ModalityProfile:
    """Everything specific to one diagnostic modality.

    Structurally parallel to :class:`~allocation.profiles.registry.ResourceProfile` and
    duck-compatible with :meth:`allocation.config.Config.for_resource`, which reads only
    ``resource_type.value``, ``caps_config`` and ``budget_config``. Reusing that method rather
    than writing a diagnostic one is the point: per-resource config selection is already
    solved and is not bed-specific.
    """

    #: Named ``resource_type`` rather than ``modality`` so ``Config.for_resource`` accepts it
    #: unchanged. The value is the modality — ``ct``, ``mri`` — never a ``<unit>_bed``.
    resource_type: DiagnosticModality
    description: str
    eligible_agents: tuple[AgentKind, ...]
    components: tuple[DiagnosticComponent, ...]
    ttl_minutes: Mapping[AgentKind, int]
    max_rounds: int
    round_seconds: int
    allocation_horizon_hours: float
    auction_key_bucket_minutes: int
    caps_config: str
    budget_config: str
    #: Default procedure length when a request does not carry its own. A request always may.
    typical_duration: timedelta
    #: Time the machine is unavailable before and after a procedure. Real scheduling capacity
    #: is the sum, and a scanner booked back-to-back at its nominal duration is over-booked.
    setup_minutes: int
    cleanup_minutes: int
    #: Procedure capabilities the modality can offer at all. A machine declares the subset it
    #: actually has; this is the vocabulary those are drawn from.
    capabilities: frozenset[str]
    #: Physically moving the patient to the machine. Ultrasound comes to the bedside; MRI does
    #: not, and that difference is the whole of the transport component for most requests.
    portable: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def modality(self) -> DiagnosticModality:
        """The modality this profile describes. ``resource_type`` under its real name."""
        return self.resource_type

    def ttl_for(self, agent: AgentKind) -> int:
        try:
            return self.ttl_minutes[agent]
        except KeyError as exc:
            raise KeyError(
                f"no TTL configured for {agent.value} in modality {self.resource_type.value}"
            ) from exc

    def is_eligible(self, agent: AgentKind) -> bool:
        return agent in self.eligible_agents

    def occupies(self, duration: timedelta) -> timedelta:
        """How long the machine is actually held for a procedure of ``duration``."""
        return duration + timedelta(minutes=self.setup_minutes + self.cleanup_minutes)


def modality_profile(
    modality: DiagnosticModality,
    description: str,
    eligible_agents: tuple[AgentKind, ...],
    ttl_minutes: Mapping[AgentKind, int],
    typical_duration: timedelta,
    setup_minutes: int,
    cleanup_minutes: int,
    capabilities: frozenset[str],
    allocation_horizon_hours: float,
    portable: bool = False,
    notes: tuple[str, ...] = (),
) -> ModalityProfile:
    """A profile for one modality, with the family's shared structure filled in.

    A factory rather than a base class for the same reason the bed family uses one:
    :class:`ModalityProfile` is a frozen ``slots=True`` dataclass, and subclassing to supply
    defaults fights the dataclass machinery for no benefit.
    """
    return ModalityProfile(
        resource_type=modality,
        description=description,
        eligible_agents=eligible_agents,
        components=DIAGNOSTIC_COMPONENTS,
        ttl_minutes=ttl_minutes,
        max_rounds=DIAGNOSTIC_MAX_ROUNDS,
        round_seconds=DIAGNOSTIC_ROUND_SECONDS,
        allocation_horizon_hours=allocation_horizon_hours,
        auction_key_bucket_minutes=DIAGNOSTIC_AUCTION_KEY_BUCKET_MINUTES,
        caps_config=f"caps_{modality.value}.yaml",
        budget_config=f"budget_{modality.value}.yaml",
        typical_duration=typical_duration,
        setup_minutes=setup_minutes,
        cleanup_minutes=cleanup_minutes,
        capabilities=capabilities,
        portable=portable,
        notes=notes,
    )


class ModalityRegistry:
    """Modality -> profile. One registration per modality, checked at import."""

    def __init__(self) -> None:
        self._profiles: dict[DiagnosticModality, ModalityProfile] = {}

    def register(self, profile: ModalityProfile) -> ModalityProfile:
        if profile.resource_type in self._profiles:
            raise ValueError(f"profile for {profile.resource_type.value} already registered")
        self._profiles[profile.resource_type] = profile
        return profile

    def get(self, modality: DiagnosticModality) -> ModalityProfile:
        try:
            return self._profiles[modality]
        except KeyError as exc:
            raise KeyError(
                f"no profile registered for {modality.value}; "
                f"registered: {[m.value for m in self._profiles]}"
            ) from exc

    def all(self) -> tuple[ModalityProfile, ...]:
        return tuple(self._profiles.values())


MODALITIES = ModalityRegistry()


# -- the four modalities -------------------------------------------------------------------
#
# ICU bids in every one of them. That is the point of this family and the reason D-A is a
# blocker for it: the bed use case never had to decide whether ICU holds a budget, because ICU
# is demand there and not a bidder. A ventilated ICU patient needing a head CT is a bidder,
# and no amount of bed-side reasoning settles it.
#
# Every duration, TTL and horizon below is an ASSUMPTION. None is clinically signed off, and
# the caps files say so in their own status field.

CT = MODALITIES.register(
    modality_profile(
        DiagnosticModality.CT,
        description="CT scanner — fast, high throughput, the emergency workhorse",
        # AgentKind.APPOINTMENTS (agent-extension, 2026-08-27): scheduled/outpatient CT
        # demand, added as a genuine 4th bidder for this modality only — MRI/X-ray/
        # ultrasound below are untouched, so their eligible_agents still name exactly ER/
        # ICU/OT.
        eligible_agents=(AgentKind.ER, AgentKind.ICU, AgentKind.OT, AgentKind.APPOINTMENTS),
        ttl_minutes={
            AgentKind.ER: 10, AgentKind.ICU: 15, AgentKind.OT: 20,
            # Longest of the four — a scheduled request has more slack by declared nature.
            AgentKind.APPOINTMENTS: 30,
        },
        typical_duration=timedelta(minutes=15),
        setup_minutes=5,
        cleanup_minutes=5,
        capabilities=frozenset({"head", "chest", "abdomen", "angiography", "contrast"}),
        allocation_horizon_hours=4.0,
        notes=("durations assumed, not measured",),
    )
)

MRI = MODALITIES.register(
    modality_profile(
        DiagnosticModality.MRI,
        description="MRI scanner — slow, scarce, high yield for soft tissue",
        eligible_agents=(AgentKind.ER, AgentKind.ICU, AgentKind.OT),
        ttl_minutes={AgentKind.ER: 15, AgentKind.ICU: 20, AgentKind.OT: 30},
        # The scarcity that makes MRI worth auctioning at all: ~45 minutes a study against
        # CT's 15, on typically one machine rather than two.
        typical_duration=timedelta(minutes=45),
        setup_minutes=10,
        cleanup_minutes=5,
        capabilities=frozenset({"brain", "spine", "abdomen", "cardiac", "contrast"}),
        allocation_horizon_hours=6.0,
        notes=(
            "safety screening (implants, ferrous foreign bodies) is a HARD eligibility "
            "constraint and is expressed through machine capabilities, never as a penalty a "
            "bidder can outspend",
        ),
    )
)

X_RAY = MODALITIES.register(
    modality_profile(
        DiagnosticModality.X_RAY,
        description="X-ray — quick, plentiful, often the sufficient answer",
        eligible_agents=(AgentKind.ER, AgentKind.ICU, AgentKind.OT),
        ttl_minutes={AgentKind.ER: 10, AgentKind.ICU: 15, AgentKind.OT: 15},
        typical_duration=timedelta(minutes=10),
        setup_minutes=2,
        cleanup_minutes=3,
        capabilities=frozenset({"chest", "abdomen", "limb", "portable"}),
        allocation_horizon_hours=4.0,
        # Portable units reach the bedside, which is why transport burden is scored per
        # modality rather than per patient.
        portable=True,
    )
)

ULTRASOUND = MODALITIES.register(
    modality_profile(
        DiagnosticModality.ULTRASOUND,
        description="Ultrasound — bedside capable, operator dependent",
        eligible_agents=(AgentKind.ER, AgentKind.ICU, AgentKind.OT),
        ttl_minutes={AgentKind.ER: 10, AgentKind.ICU: 15, AgentKind.OT: 20},
        typical_duration=timedelta(minutes=20),
        setup_minutes=3,
        cleanup_minutes=2,
        capabilities=frozenset({"abdomen", "vascular", "cardiac", "obstetric", "portable"}),
        allocation_horizon_hours=4.0,
        portable=True,
        notes=("scarce resource is the sonographer, not the probe; not modelled yet",),
    )
)
