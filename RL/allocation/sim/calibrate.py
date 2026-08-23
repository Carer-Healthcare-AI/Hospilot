"""Fitting ``common_points`` so the budget actually binds. F-27.

**Nothing downstream is meaningful until this runs.** AGENT_BUDGET §8: below a 0.40 burn rate
the budget is inert, and *"bidding maximum is free, and the RL will learn to do exactly that"*.
At the shipped Base of 700 the measured burn in this simulator is 0.5–3.9 %, an order of
magnitude below inert. In that regime the optimal policy is trivially "bid your ceiling every
time", an RL agent finds it in about fifty episodes, and it is **correct** — so a training run
would succeed, report a good return, and have learned nothing about pacing at all.

RL_READINESS §5.1 states it as a precondition rather than an improvement: *"Fitting
``common_points`` is a prerequisite, not an improvement."*

**Why this is legitimate to fit in a simulator when decision quality is not.** Burn rate is a
*mechanism* statistic — spend over allowance — and every term in it is computed by real code:
bids come from the real policy against real utilities, cost is the real
``Bid x Contention x Outcome x Rate``, and the allowance is the real shift formula. None of it
touches the outcome model. So this calibration measures the arithmetic of the mechanism, not a
clinical claim, and it is the one thing the simulator can answer without inheriting the
fabrication warning that governs everything else here.

The search is a bisection on a monotone relationship: halving Base doubles burn, near enough,
because spend is almost independent of Base until the affordability guard starts binding. It is
not exactly monotone near the bottom — once budgets bind, agents bid less, which lowers spend —
and the search reports the curve so that non-monotonicity is visible rather than averaged away.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from allocation.config import Config
from allocation.contracts import AgentKind
from allocation.sim.dataset import generate
from allocation.sim.fabricated import DEFAULT, FabricationRegister


@dataclass(frozen=True, slots=True)
class BurnPoint:
    """Measured burn at one candidate Base."""

    base: float
    burn: Mapping[str, float]
    band: Mapping[str, str]
    auctions: int

    @property
    def mean_burn(self) -> float:
        values = [v for v in self.burn.values() if v is not None]
        return sum(values) / len(values) if values else 0.0

    @property
    def worst_burn(self) -> float:
        """The lowest department's burn — the one that decides whether the budget binds.

        Mean burn hides the case that matters: a department at 0.05 has no constraint at all,
        and averaging it with one at 1.2 reports a healthy 0.6 for a system where one bidder is
        unconstrained and another is starved.
        """
        return min(self.burn.values(), default=0.0)


@dataclass(frozen=True, slots=True)
class Calibration:
    """The fitted Base and the curve behind it."""

    fitted_base: float
    curve: tuple[BurnPoint, ...]
    target: tuple[float, float]
    seeds: tuple[int, ...]

    @property
    def best(self) -> BurnPoint:
        low, high = self.target
        mid = (low + high) / 2
        return min(self.curve, key=lambda p: abs(p.mean_burn - mid))

    def report(self) -> str:
        low, high = self.target
        lines = [
            f"target burn band     {low:.2f} - {high:.2f}  (AGENT_BUDGET section 8 'working')",
            f"seeds                {list(self.seeds)}",
            f"fitted common_points {self.fitted_base:.0f}",
            "",
            f"  {'Base':>7}  {'mean':>6}  {'worst':>6}  {'er':>6}  {'ot':>6}  {'ward':>6}  band",
        ]
        for point in self.curve:
            bands = ",".join(sorted(set(point.band.values())))
            lines.append(
                f"  {point.base:>7.0f}  {point.mean_burn:>6.1%}  {point.worst_burn:>6.1%}  "
                + "  ".join(f"{point.burn.get(a, 0.0):>6.1%}" for a in ("er", "ot", "ward"))
                + f"  {bands}"
            )
        return "\n".join(lines)


def measure(
    config: Config,
    base: float,
    seeds: Sequence[int] = (1, 2, 3),
    shifts: int = 10,
    fab: FabricationRegister = DEFAULT,
) -> BurnPoint:
    """Burn per department at a candidate Base, averaged over ``seeds``.

    Averaged over several seeds because a single 10-shift run has roughly 25 auctions and the
    variance across seeds is comparable to the effect being measured. One seed would fit noise.
    """
    from allocation.budget.ledger import burn_band

    scoped = _with_base(config, base)
    totals: dict[str, list[float]] = {}
    auctions = 0

    for seed in seeds:
        dataset = generate(scoped, seed=seed, shifts=shifts, fab=fab)
        auctions += dataset.auctions
        for episode in dataset.episodes:
            # burn = spend / allowance, and the allowance is Base x the factor product. The
            # factors are recomputed per shift, so the denominator is read back off the
            # episode's own steps rather than assumed.
            allowance = _allowance(scoped, episode.agent, base)
            totals.setdefault(episode.agent.value, []).append(episode.spend / allowance)

    burn = {a: sum(v) / len(v) for a, v in totals.items() if v}
    return BurnPoint(
        base=base,
        burn=burn,
        band={a: burn_band(scoped, r) for a, r in burn.items()},
        auctions=auctions,
    )


def fit(
    config: Config,
    seeds: Sequence[int] = (1, 2, 3),
    shifts: int = 10,
    low: float = 5.0,
    high: float = 700.0,
    steps: int = 8,
    fab: FabricationRegister = DEFAULT,
) -> Calibration:
    """Bisect for the Base that puts mean burn inside the working band.

    Bounded below at 5 rather than 0: a Base small enough to make burn 1.0 by starving every
    department is not a fit, it is a different failure, and the band's ``starved_above`` exists
    to catch it. The curve is returned in full so that a run which never reaches the band is
    visibly a failed fit rather than silently the closest point to it.
    """
    band = config.budget["burn_rate_bands"]["working"]
    target_low, target_high = float(band[0]), float(band[1])
    midpoint = (target_low + target_high) / 2

    curve: list[BurnPoint] = []
    lo, hi = low, high

    for _ in range(steps):
        mid = (lo + hi) / 2
        point = measure(config, mid, seeds=seeds, shifts=shifts, fab=fab)
        curve.append(point)

        if point.mean_burn < midpoint:
            hi = mid  # burn too low: the budget is too large, shrink it
        else:
            lo = mid

    curve.sort(key=lambda p: p.base)
    best = min(curve, key=lambda p: abs(p.mean_burn - midpoint))
    return Calibration(
        fitted_base=best.base,
        curve=tuple(curve),
        target=(target_low, target_high),
        seeds=tuple(seeds),
    )


def _with_base(config: Config, base: float) -> Config:
    """A config whose budget pool carries ``base`` as ``common_points``.

    Deep-copied rather than mutated: ``Config`` is frozen and shared across a process, and a
    calibration that edited it in place would silently change the Base for every later run in
    the same session — including the evaluation that is supposed to be independent of it.
    """
    budget = {
        **config.budget,
        "base": {**config.budget["base"], "common_points": float(base)},
    }
    return replace(config, budget=budget)


def _allowance(config: Config, agent: AgentKind, base: float) -> float:
    """Base x the factor product, for the denominator of burn.

    Recomputed rather than read off a stored row because the factors move with occupancy and
    the point of the exercise is to measure burn against the allowance an agent actually had.
    """
    from allocation.budget.factors import compute_factors

    factors = compute_factors(config, agent, occupancy_4h=1.0)
    return max(1e-9, base * factors.product)
