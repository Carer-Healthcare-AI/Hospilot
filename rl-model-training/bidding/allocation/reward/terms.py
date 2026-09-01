"""The reward terms. RL-Steps sections 23-24.

Loaded from ``config/reward.yaml`` rather than written here, for one reason: **these numbers
are the objective function.** A cap that is wrong distorts a bid; a reward term that is wrong
teaches the policy to want the wrong thing, permanently and invisibly. They belong somewhere a
clinician can read them.

The three-state rule from the utility applies again, and matters more here::

    observed True   ->  award the points
    observed False  ->  0, the thing did not happen
    observed None   ->  MISSING, excluded, episode marked incomplete

Scoring an unobserved ``no_mortality`` as 0 would say "the patient died". Scoring it as 30
would say "the patient lived". Neither is known, and F-01 means it is never known today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from allocation.config import Config


@dataclass(frozen=True, slots=True)
class RewardTerm:
    """One named outcome condition and what it is worth."""

    name: str
    points: float
    source: str | None
    description: str
    scenario: str = "won"

    @property
    def observable(self) -> bool:
        """False when nothing in the schema can produce this observation."""
        return self.source is not None


def load_terms(config: Config) -> Mapping[str, RewardTerm]:
    spec = config.reward["terms"]
    return {
        name: RewardTerm(
            name=name,
            points=float(body["points"]),
            source=body.get("source"),
            description=str(body.get("description", "")).strip(),
            scenario=str(body.get("scenario", "won")),
        )
        for name, body in spec.items()
    }


def unobservable(config: Config) -> tuple[str, ...]:
    """Terms with no source in the schema. Today: ``no_mortality`` (F-01)."""
    return tuple(config.reward.get("unobservable_terms", ()))


def horizon_hours(config: Config) -> float:
    return float(config.reward["horizon_hours"])


def discount_gamma(config: Config) -> float:
    return float(config.reward["discount_gamma"])


def maximum_reward(config: Config) -> float:
    """Best possible episode — every section 23 term observed true.

    Section 23 lists nine terms and states R = 190; they sum to **200** (BUILD_SPEC F-22).
    The line items are authoritative, so this returns the arithmetic rather than the stated
    total: a total cannot override the items it is a total of.

    Scoped to the ``won`` scenario. Counting section 24's positive terms as well would put the
    ceiling at 235 by letting "surgery not cancelled" and "OT throughput preserved instead"
    both land in the same episode — outcomes that are mutually exclusive by construction.
    """
    return sum(t.points for t in load_terms(config).values() if t.points > 0 and t.scenario == "won")


def minimum_reward(config: Config) -> float:
    """Worst possible episode — every section 24 penalty observed true."""
    return sum(t.points for t in load_terms(config).values() if t.points < 0 and t.scenario == "lost")
