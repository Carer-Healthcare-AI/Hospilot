"""Opening diagnostic shift budgets.

Thin by design. Every number here comes from :mod:`allocation.budget` — the base, the four
factors, the ledger — and the only diagnostic content is *which occupancy* the scarcity factor
is computed from: this modality's committed capacity, never a ward's.

That substitution is the whole reason this file exists rather than a direct call. Scarcity is
declared ``scope: per_resource_type`` in every pool file, and feeding a bed occupancy into a
CT pool would inflate scanner budgets during a bed crunch that has nothing to do with imaging.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping, Sequence

from dataclasses import replace

from allocation.budget.base import BaseBudget, derive_base
from allocation.budget.factors import compute_factors
from allocation.budget.ledger import open_shift
from allocation.budget.shifts import Shift
from allocation.config import Config
from allocation.contracts import AgentKind, BudgetState
from allocation.use_cases.diagnostic_machine import capacity
from allocation.use_cases.diagnostic_machine.contracts import DiagnosticMachineState
from allocation.use_cases.diagnostic_machine.profiles import ModalityProfile


#: The simulation-only regimes a run may select. Never applied by default.
REGIMES = ("normal", "constrained", "exhausted")


def simulation_base(config: Config, regime: str) -> float:
    """Base points for a simulation regime, from this modality's pool file.

    Raises rather than defaulting on an unknown regime: silently falling back to the live Base
    would produce an inert run labelled ``constrained``, and the label is the whole point.
    """
    simulation = config.budget.get("simulation")
    if not simulation:
        raise KeyError(
            f"budget pool {config.budget.get('resource_type')!r} declares no simulation "
            "regimes; a simulated budget must be calibrated, not inherited from the live pool"
        )
    try:
        return float(simulation["regimes"][regime]["base_points"])
    except KeyError as exc:
        raise KeyError(
            f"unknown simulation regime {regime!r}; declared: "
            f"{sorted(simulation['regimes'])}"
        ) from exc


def open_diagnostic_budgets(
    config: Config,
    profile: ModalityProfile,
    shift: Shift,
    machines: Sequence[DiagnosticMachineState] = (),
    agents: Sequence[AgentKind] | None = None,
    previous: Mapping[AgentKind, BudgetState] | None = None,
    regime: str | None = None,
) -> dict[AgentKind, BudgetState]:
    """One budget row per eligible agent, funded from this modality's pool.

    ``config`` must already be scoped by ``config.for_modality(...)``; passing an unscoped one
    funds a scanner auction from a bed pool, which is the exact error the per-resource pool
    split exists to make impossible. Checked rather than trusted.
    """
    declared = str(config.budget.get("resource_type", ""))
    if declared != profile.modality.value:
        raise ValueError(
            f"config is scoped to budget pool {declared!r} but the profile is "
            f"{profile.modality.value!r}; call config.for_modality() first"
        )

    occupancy = _committed(machines, shift.start, shift.end)
    previous = previous or {}
    override = None if regime is None else simulation_base(config, regime)
    return {
        agent: open_shift(
            config,
            _base(config, agent, override),
            compute_factors(config, agent, occupancy_4h=occupancy),
            shift,
            previous=previous.get(agent),
        )
        for agent in (agents or profile.eligible_agents)
    }


def _base(config: Config, agent: AgentKind, override: float | None) -> BaseBudget:
    """This agent's Base, with a simulation regime applied if one was selected.

    The override replaces only the Base. Every factor still applies, so a regime scales the
    whole field together and cannot reorder who can afford what — which is what keeps a
    simulated run comparable to a live one rather than a different mechanism.
    """
    base = derive_base(config, agent)
    return base if override is None else replace(base, base=override)


def diagnostic_shift(start: datetime, hours: float = 8.0, label: str = "day") -> Shift:
    """A shift for a diagnostic run. Same 8-hour default the bed pools assume."""
    return Shift(
        label=label,
        shift_id=f"{label}-{start:%Y%m%d-%H%M}",
        start=start,
        end=start + timedelta(hours=hours),
    )


def _committed(
    machines: Sequence[DiagnosticMachineState], start: datetime, end: datetime
) -> float | None:
    """Fraction of the shift's machine time already booked, or ``None`` with no machines.

    ``None`` is passed straight through to
    :func:`~allocation.budget.factors.scarcity_factor`, which falls back to the bottom of its
    clamp on a missing reading — the conservative direction. A fabricated 0.0 would claim the
    modality is empty.
    """
    return capacity.committed_fraction(machines, start, end)
