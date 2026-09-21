"""Comparing policies on unseen scenarios.

*"Did the learned policy allocate diagnostic machine capacity better than a reasonable
non-learning policy on previously unseen scenarios?"* — the framework's §31 headline, and the
question this module answers. Note what it is not: **not** "did RL obtain a higher reward".
Reward is the thing being optimised, so it cannot also be the thing that certifies the
optimiser. Every column below except ``return`` is an outcome the reward does not directly
contain.

**Train and test scenarios are disjoint by construction.** :func:`split` partitions by
scenario name, never by transition — neighbouring transitions inside one episode are heavily
correlated, so a random split over rows leaks the answer into the training set and every
number that follows is optimistic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable, Mapping, Sequence

from allocation.config import Config
from allocation.use_cases.diagnostic_machine.budgets import (
    diagnostic_shift,
    open_diagnostic_budgets,
)
from allocation.use_cases.diagnostic_machine.config import for_modality
from allocation.use_cases.diagnostic_machine.reward import (
    DiagnosticRewardObserver,
    episode_return,
)
from allocation.use_cases.diagnostic_machine.scenarios import EPOCH, Scenario
from allocation.use_cases.diagnostic_machine.simulator import SimulationResult, simulate

#: A policy factory: given the scoped config, return something with ``.decide(...)``.
PolicyFactory = Callable[[Config], object]


@dataclass(frozen=True, slots=True)
class PolicyScore:
    """One policy's results over one set of scenarios."""

    policy: str
    scenarios: int
    requests: int
    answered: int
    diverted: int
    expired: int
    abandoned: int
    timeliness: float
    management_impact_rate: float
    average_delay_minutes: float
    utilisation: float
    mean_burn: float
    constraint_violations: int
    total_return: float

    def row(self) -> str:
        return (
            f"  {self.policy:20} {self.timeliness:9.3f} {self.management_impact_rate:8.3f} "
            f"{self.average_delay_minutes:8.1f} {self.utilisation:7.3f} "
            f"{self.abandoned:5} {self.expired:5} {self.mean_burn:7.3f} "
            f"{self.total_return:10.1f}"
        )


HEADER = (
    f"  {'policy':20} {'timeliness':>9} {'impact':>8} {'delay':>8} {'util':>7} "
    f"{'aban':>5} {'exp':>5} {'burn':>7} {'return':>10}"
)


def split(
    scenarios: Sequence[Scenario], holdout: int = 3
) -> tuple[tuple[Scenario, ...], tuple[Scenario, ...]]:
    """Partition by scenario, deterministically.

    The last ``holdout`` scenarios by name are the test set. Sorted rather than sampled so the
    split does not move between runs — a benchmark that reshuffles itself cannot be compared
    against yesterday's number.
    """
    ordered = sorted(scenarios, key=lambda s: s.name)
    if holdout >= len(ordered):
        raise ValueError("holdout must leave at least one training scenario")
    return tuple(ordered[:-holdout]), tuple(ordered[-holdout:])


def run_policy(
    config: Config,
    scenario: Scenario,
    factory: PolicyFactory | None = None,
    regime: str | None = "normal",
) -> SimulationResult:
    """Run one scenario under one policy."""
    scoped = for_modality(config, scenario.modality)
    shift = diagnostic_shift(EPOCH, hours=8.0)
    budgets = open_diagnostic_budgets(
        scoped, scenario.profile, shift, machines=scenario.machines, regime=regime
    )
    return simulate(
        scoped,
        scenario.profile,
        scenario.machines,
        scenario.arrivals,
        budgets,
        starts_at=shift.start,
        ends_at=shift.start + timedelta(hours=scenario.hours),
        alternative_machines=scenario.alternative_machines,
        policy=None if factory is None else factory(scoped),  # type: ignore[arg-type]
    )


def score(
    config: Config,
    name: str,
    scenarios: Sequence[Scenario],
    factory: PolicyFactory | None = None,
    regime: str | None = "normal",
) -> PolicyScore:
    """Aggregate one policy across a scenario set."""
    observer = DiagnosticRewardObserver()
    totals = {
        "requests": 0, "answered": 0, "diverted": 0, "expired": 0, "abandoned": 0,
        "violations": 0,
    }
    delays: list[float] = []
    utilisations: list[float] = []
    burns: list[float] = []
    impacts: list[tuple[float, float]] = []
    returns = 0.0

    for scenario in scenarios:
        result = run_policy(config, scenario, factory, regime)
        m = result.metrics
        totals["requests"] += m.requests
        totals["answered"] += m.answered
        totals["diverted"] += m.diverted
        totals["expired"] += m.expired
        totals["abandoned"] += m.abandoned
        totals["violations"] += m.constraint_violations
        if result.procedures:
            delays.append(m.average_delay_minutes)
        utilisations.append(m.utilisation)
        burns.extend(m.burn_rate.values())
        delivered = sum(p.diagnostic_value for p in result.procedures if p.on_time)
        requested = sum(o.requested_value for o in result.outcomes)
        impacts.append((delivered, requested))

        transport = {
            a.request.request_id: a.request.transport_minutes for a in scenario.arrivals
        }
        returns += episode_return(observer.score_all(result.outcomes, transport))

    delivered = sum(d for d, _ in impacts)
    requested = sum(r for _, r in impacts)
    return PolicyScore(
        policy=name,
        scenarios=len(scenarios),
        requests=totals["requests"],
        answered=totals["answered"],
        diverted=totals["diverted"],
        expired=totals["expired"],
        abandoned=totals["abandoned"],
        timeliness=totals["answered"] / totals["requests"] if totals["requests"] else 0.0,
        management_impact_rate=delivered / requested if requested else 0.0,
        average_delay_minutes=sum(delays) / len(delays) if delays else 0.0,
        utilisation=sum(utilisations) / len(utilisations) if utilisations else 0.0,
        mean_burn=sum(burns) / len(burns) if burns else 0.0,
        constraint_violations=totals["violations"],
        total_return=returns,
    )


def compare(
    config: Config,
    scenarios: Sequence[Scenario],
    policies: Mapping[str, PolicyFactory | None],
    regime: str | None = "normal",
) -> list[PolicyScore]:
    """Score every policy on the same scenarios, best timeliness first."""
    scores = [
        score(config, name, scenarios, factory, regime)
        for name, factory in policies.items()
    ]
    return sorted(scores, key=lambda s: (-s.timeliness, -s.management_impact_rate))


def table(scores: Sequence[PolicyScore]) -> str:
    lines = [HEADER, "  " + "-" * (len(HEADER) - 2)]
    lines += [s.row() for s in scores]
    return "\n".join(lines)


__all__ = ["PolicyScore", "compare", "run_policy", "score", "split", "table"]
