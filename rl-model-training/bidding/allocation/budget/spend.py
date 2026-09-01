"""Contention, cost, and the affordability constraint. AGENT_BUDGET section 7.

``Cost = Bid x Contention x Outcome x CommitmentRate``

**Settled: the commitment rate, not full deduction.** RL-Steps specifies spend three
incompatible ways — section 4's table deducts the full bid, section 4's text three paragraphs
later sets a 25 % rate, and section 19 deducts the full bid again — differing about 5x in
shift capacity.

**The contention formula is honest about what it is.** RL-Steps supplies a range (0.8-1.3),
four ordered qualitative labels, and *two contradictory numeric anchors for the same label*
(the factor table says high = 1.2, the worked example says 1.1). There is nothing to derive
from, so this is a three-parameter fit to four qualitative points. It is monotone in the right
directions and bounded, so bad input cannot blow up a budget, and it reproduces every example
RL-Steps gives — which is the only validation available.

That latitude is tolerable **only because contention scales how fast budget burns, never who
wins.** Getting it wrong distorts pacing, not allocation. The same latitude must not be
extended to the utility caps.
"""

from __future__ import annotations

from dataclasses import dataclass

from allocation.config import Config
from allocation.features.scale import clamp


@dataclass(frozen=True, slots=True)
class SpendResult:
    """A settled cost, with every input retained for the audit log."""

    bid: float
    contention: float
    outcome_factor: float
    commitment_rate: float
    cost: float
    won: bool


def contention(
    config: Config,
    n_bidders: int,
    occupancy: float,
    expected_discharges_4h: float | None,
) -> float:
    """``clamp(0.80 + 0.10(n-1) + 0.15 occ_stress + 0.05 disch_stress, 0.8, 1.3)``.

    Reproduces RL-Steps' four labels: 1 patient -> 0.80 (low), 2 -> 0.97 (normal),
    4 -> 1.19 (high), 4 at 98 % with no discharge -> 1.28 (very high).

    An earlier form, ``0.70 + 0.15 n + 0.15 occ_stress``, was wrong: it saturated at four
    bidders regardless of occupancy, so "high" and "very high" both clamped to 1.30 and the
    occupancy term stopped working exactly where it matters most.
    """
    cfg = config.budget["spend"]["contention"]
    lo, hi = (float(x) for x in cfg["clamp"])

    if n_bidders < 1:
        raise ValueError("contention needs at least one bidder")

    occ_stress = clamp(
        (occupancy - float(cfg["occupancy_onset"])) / float(cfg["occupancy_span"])
    )
    discharges = 0.0 if expected_discharges_4h is None else expected_discharges_4h
    disch_stress = clamp(1.0 - discharges / float(cfg["discharge_clear_at"]))

    value = (
        float(cfg["floor"])
        + float(cfg["per_extra_bidder"]) * (n_bidders - 1)
        + float(cfg["occupancy_weight"]) * occ_stress
        + float(cfg["discharge_weight"]) * disch_stress
    )
    return clamp(value, lo, hi)


def outcome_factor(config: Config, won: bool) -> float:
    """Win 1.0, lose 0.1. The small participation charge stops endless meaningless auctions."""
    factors = config.budget["spend"]["outcome_factor"]
    return float(factors["win"] if won else factors["lose"])


def commitment_rate(config: Config) -> float:
    return float(config.budget["spend"]["commitment_rate"])


def compute_cost(
    config: Config, bid: float, contention_factor: float, won: bool
) -> SpendResult:
    """``Cost = Bid x Contention x Outcome x CommitmentRate``."""
    if bid < 0:
        raise ValueError("bid cannot be negative")
    outcome = outcome_factor(config, won)
    rate = commitment_rate(config)
    return SpendResult(
        bid=bid,
        contention=contention_factor,
        outcome_factor=outcome,
        commitment_rate=rate,
        cost=bid * contention_factor * outcome * rate,
        won=won,
    )


def max_affordable_bid(
    config: Config, remaining: float, contention_factor: float, won: bool = True
) -> float:
    """``Bid <= Remaining / (Contention x Outcome x Rate)`` — AGENT_BUDGET 7.3.

    RL-Steps section 4 writes ``Bid <= RemainingBudget``. Under a 0.25 commitment rate only a
    quarter of the bid is deducted, so that **over-restricts by 4x**: a department late in a
    shift would be throttled four times harder than the commitment-rate design intends.

    The constraint is on cost, not on the bid.
    """
    if remaining <= 0:
        return 0.0
    divisor = contention_factor * outcome_factor(config, won) * commitment_rate(config)
    if divisor <= 0:
        return float("inf")
    return remaining / divisor


def can_afford(
    config: Config, bid: float, remaining: float, contention_factor: float, won: bool = True
) -> bool:
    return bid <= max_affordable_bid(config, remaining, contention_factor, won) + 1e-9
