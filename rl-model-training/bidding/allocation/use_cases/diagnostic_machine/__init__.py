"""Diagnostic-machine resource family.

CT, MRI, X-ray and ultrasound as one family: the machine is the resource and its operating
time is the capacity that gets allocated. See ``docs/diagnostic machine.md`` for the framework
this implements, and ``profiles.py`` for what a fifth modality would have to supply.

The layers, bottom up:

    contracts   requests, machines, intervals, the pathway action
    utility     the eight components and their assumed caps
    scoring     turning a request and a machine schedule into those components
    profiles    what differs between modalities — data only
    config      selecting a modality's caps table and budget pool
    budgets     opening shift budgets from this modality's pool
    policy      the deterministic bidder, on the shared alpha ladder
    auction     one auction, on the shared auction core
    simulator   a scheduled day, with fates and evaluation metrics
    scenarios   deterministic fixtures — the heuristic baseline, before any RL
"""

from allocation.use_cases.diagnostic_machine.auction import (
    DiagnosticAuctionOutcome,
    DiagnosticAuctionResult,
    DiagnosticBid,
    DiagnosticExitReason,
    DiagnosticRound,
    RescoringUtilities,
    run_diagnostic_auction,
)
from allocation.use_cases.diagnostic_machine.budgets import (
    diagnostic_shift,
    open_diagnostic_budgets,
)
from allocation.use_cases.diagnostic_machine.config import for_modality, with_diagnostic_tables
from allocation.use_cases.diagnostic_machine.contracts import (
    AllocationInterval,
    DiagnosticMachineState,
    DiagnosticModality,
    DiagnosticPathwayAction,
    DiagnosticRequest,
    MachineStatus,
)
from allocation.use_cases.diagnostic_machine.policy import (
    DiagnosticDecision,
    DiagnosticHeuristicPolicy,
    DiagnosticOptions,
    DiagnosticPlan,
    build_options,
    next_capacity,
)
from allocation.use_cases.diagnostic_machine.profiles import (
    MODALITIES,
    ModalityProfile,
    modality_profile,
)
from allocation.use_cases.diagnostic_machine.scoring import (
    DiagnosticContext,
    caps_from_config,
    score_request,
)
from allocation.use_cases.diagnostic_machine.simulator import (
    Arrival,
    Fate,
    ProcedureRecord,
    RequestOutcome,
    SimulationMetrics,
    SimulationResult,
    simulate,
)
from allocation.use_cases.diagnostic_machine.utility import (
    INITIAL_ASSUMED_CAPS,
    DiagnosticComponent,
    DiagnosticUtility,
)

__all__ = [
    "INITIAL_ASSUMED_CAPS",
    "MODALITIES",
    "AllocationInterval",
    "Arrival",
    "DiagnosticAuctionOutcome",
    "DiagnosticAuctionResult",
    "DiagnosticBid",
    "DiagnosticComponent",
    "DiagnosticContext",
    "DiagnosticDecision",
    "DiagnosticExitReason",
    "DiagnosticHeuristicPolicy",
    "DiagnosticMachineState",
    "DiagnosticModality",
    "DiagnosticOptions",
    "DiagnosticPathwayAction",
    "DiagnosticPlan",
    "DiagnosticRequest",
    "DiagnosticRound",
    "DiagnosticUtility",
    "Fate",
    "MachineStatus",
    "ModalityProfile",
    "ProcedureRecord",
    "RequestOutcome",
    "RescoringUtilities",
    "SimulationMetrics",
    "SimulationResult",
    "build_options",
    "caps_from_config",
    "diagnostic_shift",
    "for_modality",
    "modality_profile",
    "next_capacity",
    "open_diagnostic_budgets",
    "run_diagnostic_auction",
    "score_request",
    "simulate",
    "with_diagnostic_tables",
]
