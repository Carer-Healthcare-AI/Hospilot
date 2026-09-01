"""The three shift factors. AGENT_BUDGET section 5.

``B = Base x Demand x Fairness x Scarcity``

Criticality is **not** here. AGENT_BUDGET 5.2 drops it for two independent reasons: it
double-counts Urgency (already a 40-point component, so paying ER a budget multiplier *because
its cases are time-critical* charges the same clinical fact twice), and it is unmeasurable —
it needs the gap between when a request was raised and when admission was needed, and nothing
stores the request.

**Scarcity is global.** RL-Steps applies it per agent, which cannot be right: if ICU is at 96 %
with one expected discharge, it is 96 % for everyone. Applied per agent it is either
meaningless or a second copy of Demand. Applied globally it does something real — during a
crunch, no department should be prevented from bidding by budget exhaustion — and, being an
identical multiplier, **it cannot change who wins**.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from allocation.config import Config
from allocation.contracts import AgentKind
from allocation.features.scale import clamp


@dataclass(frozen=True, slots=True)
class BudgetFactors:
    """The three multipliers, each with its provenance.

    All three are stored on the budget row, not just their product: *"A budget of 80 with no
    record of which factor moved it is unauditable, and re-deriving it after a cap change is
    impossible."*
    """

    demand: float
    criticality: float
    fairness: float
    scarcity: float
    demand_source: str
    criticality_source: str
    fairness_source: str
    scarcity_source: str

    @property
    def product(self) -> float:
        return self.demand * self.criticality * self.fairness * self.scarcity

    @property
    def sources(self) -> dict[str, str]:
        """Provenance per factor, for the budget row and the derivation printout."""
        return {
            "demand": self.demand_source,
            "criticality": self.criticality_source,
            "fairness": self.fairness_source,
            "scarcity": self.scarcity_source,
        }


def demand_factor(
    config: Config, forecast: float | None, median_30d: float | None
) -> tuple[float, str]:
    """``clamp(forecast / median_30(forecast), 0.8, 1.3)``.

    Numerator and denominator both come from ``/icu/demand``, so any bias in the forecast
    cancels instead of compounding.

    **Historical ICU admissions must never be used as the denominator.** They count requests
    that were *granted* and silently drop those denied, and the omission is biased per
    department: one that loses often looks like it demands less, so it gets a smaller budget,
    so it loses more. Self-reinforcing.

    Falls back to 1.0 while the retention table is short of 30 days — see F-18.
    """
    cfg = config.budget["factors"]["demand"]
    lo, hi = (float(x) for x in cfg["clamp"])
    fallback = float(cfg["fallback"])

    if forecast is None or median_30d is None or median_30d <= 0:
        return fallback, "fallback (no 30-day forecast history yet — F-18)"
    return clamp(forecast / median_30d, lo, hi), "/icu/demand ÷ median30(/icu/demand)"


def fairness_factor(
    config: Config,
    agent: AgentKind,
    expected_share: float | None = None,
    actual_share: float | None = None,
) -> tuple[float, str]:
    """v1 is a constant 1.0; v2 is win-share based and needs ~10 shifts of auction log.

    RL-Steps defines fairness twice in one paragraph — ER *"won disproportionately many"*
    (win share) and OT *"lost several high-priority cases despite high utility"* (forgone
    utility). They are different measures. The second is better, because it catches a
    department losing narrowly on genuinely strong cases, but it needs utilities *and*
    outcomes logged for losers. That is v3.

    The +-0.05 clamp keeps it *"a small correction, not something that should override clinical
    urgency"* — RL-Steps' own words.
    """
    cfg = config.budget["factors"]["fairness"]
    version = str(cfg.get("version", "v1"))

    if version == "v1" or expected_share is None or actual_share is None:
        return float(cfg["value"]), f"{version} constant (auction log not available — B.12)"

    lo, hi = (float(x) for x in cfg["clamp"])
    weight = float(cfg["v2_weight"])
    value = clamp(1.0 + weight * (expected_share - actual_share), lo, hi)
    return value, f"{version} win-share over {cfg['v2_window_shifts']} shifts"


def criticality_factor(config: Config, agent: AgentKind) -> tuple[float, str]:
    """How time-sensitive this department's requests typically are. RL-Steps section 4.

    A **department-level, shift-invariant** property — the share of its ICU requests that
    need admission within 30 minutes. Distinct from Urgency, which scores *this patient in
    this auction*; the two share a subject but not a granularity.

    Hard-coded to RL-Steps' published values today. The doc's own instruction is *"This should
    come from data, not a hard-coded belief that ER is always more important"* (line 192), and
    the data is the auction log, which does not exist (F-02). Returning the published number
    with a source of ``rule_table`` keeps that visible on every budget row.
    """
    cfg = config.budget["factors"]["criticality"]
    values: Mapping[str, Any] = cfg.get("values", {})

    if agent.value not in values:
        default = float(cfg.get("default", 1.0))
        return default, f"default:{default:g} (no entry for {agent.value})"

    return float(values[agent.value]), str(cfg.get("source", "rule_table"))


def criticality_from_share(config: Config, share_within_threshold: float) -> tuple[float, str]:
    """The factor a department's observed share maps to. Unused until the log exists.

    Present so the band table in the budget pool is executable rather than documentation —
    a mapping nothing can run is a mapping that silently stops matching its own comment.
    """
    bands = config.budget["factors"]["criticality"]["thresholds"]["bands"]
    for band in sorted(bands, key=lambda b: -float(b["min_share"])):
        if share_within_threshold >= float(band["min_share"]):
            return float(band["factor"]), f"observed_share:{share_within_threshold:.2f}"
    return float(config.budget["factors"]["criticality"].get("default", 1.0)), "below_all_bands"


def scarcity_factor(config: Config, occupancy_4h: float | None) -> tuple[float, str]:
    """``clamp(1 + 0.3 x (occupancy - 0.85) / 0.15, 1.0, 1.3)`` — **one value for all agents**.

    Raising every budget together during a crunch makes the constraint less binding for
    everyone at once. It alters no relative position, so it cannot change an allocation.
    """
    cfg = config.budget["factors"]["scarcity"]
    lo, hi = (float(x) for x in cfg["clamp"])

    if occupancy_4h is None:
        return lo, "fallback (no occupancy forecast)"

    onset = float(cfg["onset_occupancy"])
    span = float(cfg["span"])
    slope = float(cfg["slope"])
    value = clamp(1.0 + slope * (occupancy_4h - onset) / span, lo, hi)
    return value, "/icu/occupancy (global)"


def compute_factors(
    config: Config,
    agent: AgentKind,
    occupancy_4h: float | None,
    demand_forecast: float | None = None,
    demand_median_30d: float | None = None,
    expected_share: float | None = None,
    actual_share: float | None = None,
) -> BudgetFactors:
    demand, demand_src = demand_factor(config, demand_forecast, demand_median_30d)
    criticality, criticality_src = criticality_factor(config, agent)
    fairness, fairness_src = fairness_factor(config, agent, expected_share, actual_share)
    scarcity, scarcity_src = scarcity_factor(config, occupancy_4h)
    return BudgetFactors(
        demand=demand,
        criticality=criticality,
        fairness=fairness,
        scarcity=scarcity,
        demand_source=demand_src,
        criticality_source=criticality_src,
        fairness_source=fairness_src,
        scarcity_source=scarcity_src,
    )


def expected_shares(targets: Mapping[str, Mapping[str, Any]]) -> dict[str, float]:
    """Each department's share of total target wins — the baseline Fairness v2 compares to."""
    total = sum(int(t["n_win"]) for t in targets.values())
    if total <= 0:
        return {agent: 0.0 for agent in targets}
    return {agent: int(t["n_win"]) / total for agent, t in targets.items()}


def actual_shares(wins_by_agent: Sequence[tuple[str, int]]) -> dict[str, float]:
    """Observed win share over a trailing window. Needs the auction log — B.12."""
    total = sum(count for _, count in wins_by_agent)
    if total <= 0:
        return {agent: 0.0 for agent, _ in wins_by_agent}
    return {agent: count / total for agent, count in wins_by_agent}
