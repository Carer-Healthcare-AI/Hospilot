"""The diagnostic state vector, extended with the four columns bed's encoder has and this
family's did not.

Append-only and separately versioned, exactly as
``critical_care_equipment.research_encoder`` is: ``encoder.FEATURES`` is untouched, every
existing diagnostic artifact keeps loading, and ``encoder.encoder_version()`` keeps its value.

The four columns and their justification are identical to critical care's, because the gap
against ``allocation/rl/encoder.py`` is the same gap:

``ladder_alpha_logit``
    Bed's ``heuristic_alpha_logit`` (``rl/encoder.py:50-68``). The ladder plays
    ``(rival - mine + margin) / (ceiling - mine)``; diagnostic's encoder publishes
    ``behind_by_scaled`` and ``headroom_fraction`` separately and **no linear model forms a
    quotient of two of its own features**. Bed supplies the LOGIT rather than the ratio because
    a head read through a sigmoid cannot reproduce the identity; this does the same, rescaled to
    ``[0, 1]``.

``is_opening``
    Bed's ``regime_of`` split (``rl/encoder.py:153-162``), a conjunction of two features that a
    linear model also cannot represent.

``round_index_scaled``
    Bed carries this alongside ``rounds_left``; diagnostic carried only the latter.

``shift_fraction_elapsed``
    Bed's world clock. Diagnostic's temporal columns (``safe_wait_fraction``,
    ``next_capacity_slack``) are all scoped to the request, so nothing told the learner where in
    the shift a decision sat.

Nothing here reads an outcome, a fate, a reward or a label; every column is computable at
serving time from the round view the policy is handed.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

from allocation.config import Config
from allocation.contracts import Action, AgentKind, BudgetState, RoundState
from allocation.features.scale import clamp
from allocation.use_cases.diagnostic_machine.encoder import (
    FEATURES as BASE_FEATURES,
    encode as encode_base,
    encoder_version as base_encoder_version,
)
from allocation.use_cases.diagnostic_machine.policy import DiagnosticOptions
from allocation.use_cases.diagnostic_machine.utility import DiagnosticUtility

EXTRA_FEATURES: tuple[str, ...] = (
    "ladder_alpha_logit",
    "is_opening",
    "round_index_scaled",
    "shift_fraction_elapsed",
)

RESEARCH_FEATURES: tuple[str, ...] = BASE_FEATURES + EXTRA_FEATURES

#: Same constant and same reasoning as ``rl/encoder.py:111``: +-6 covers alpha in
#: (0.0025, 0.9975), the whole usable range.
LOGIT_LIMIT = 6.0

EPS = 1e-9


@dataclass(frozen=True, slots=True)
class LadderParams:
    """The three ``ladder.climb`` tuning inputs, captured so the version can hash them.

    ``ladder_alpha_logit`` is a function of these, so a change to any of them changes what the
    column MEANS without changing a name — the same argument bed makes for hashing
    ``LEAD_ALPHA``/``OVERTAKE_MARGIN`` into its own version (``rl/encoder.py:174-183``).
    """

    opening_alpha: Mapping[str, float]
    lead_alpha: float
    overtake_margin: float

    @classmethod
    def from_config(cls, config: Config) -> "LadderParams":
        cfg = config.auction["policy"]["heuristic"]
        return cls(
            opening_alpha={k: float(v) for k, v in sorted(cfg["opening_alpha"].items())},
            lead_alpha=float(cfg["lead_alpha"]),
            overtake_margin=float(cfg["overtake_margin"]),
        )

    def opening_for(self, agent: AgentKind) -> float:
        return float(self.opening_alpha.get(agent.value, self.opening_alpha["default"]))

    @property
    def digest(self) -> str:
        parts = [f"{k}={v!r}" for k, v in sorted(self.opening_alpha.items())]
        parts.append(f"lead={self.lead_alpha!r}")
        parts.append(f"margin={self.overtake_margin!r}")
        parts.append(f"logit_limit={LOGIT_LIMIT!r}")
        return "|".join(parts)


def research_encoder_version(params: LadderParams) -> str:
    """Content hash of the full column list AND the constants that shape the added columns."""
    payload = "|".join(RESEARCH_FEATURES) + "||" + params.digest
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class ResearchEncodedState:
    """One encoded decision point under the extended layout."""

    values: tuple[float, ...]
    version: str

    def __post_init__(self) -> None:
        if len(self.values) != len(RESEARCH_FEATURES):
            raise ValueError(
                f"expected {len(RESEARCH_FEATURES)} features, got {len(self.values)}"
            )

    def named(self) -> dict[str, float]:
        return dict(zip(RESEARCH_FEATURES, self.values))


def _standing(round_state: RoundState, agent: AgentKind) -> float:
    for bid in round_state.bids:
        if bid.agent is agent:
            return 0.0 if bid.action is Action.WITHDRAW else bid.amount
    return 0.0


def _leader(round_state: RoundState, agent: AgentKind) -> float:
    rivals = [
        bid.amount
        for bid in round_state.bids
        if bid.agent is not agent and bid.action is not Action.WITHDRAW
    ]
    return max(rivals, default=0.0)


def ladder_alpha(
    mine: float, leader: float, ceiling: float, agent: AgentKind, params: LadderParams
) -> float:
    """The alpha ``ladder.climb`` would play from this position — the three COMPETE branches.

    Deliberately not a call into :func:`~.ladder.climb`: climb also returns EXIT and HOLD
    verdicts that depend on affordability and on config the encoder does not take, and an
    encoder that could return "no alpha" would need a presence flag for a quantity that is
    always defined as a bid ratio. Bed's ``_alpha_logit`` makes the same choice.
    """
    headroom = ceiling - mine
    if headroom <= EPS:
        return 0.0
    if mine <= EPS and leader <= EPS:
        return params.opening_for(agent)
    if mine >= leader - EPS:
        return params.lead_alpha
    return clamp((leader - mine + params.overtake_margin) / headroom)


def alpha_logit(alpha: float) -> float:
    """``alpha`` as a logit, squashed and rescaled to ``[0, 1]``."""
    bounded = max(1e-6, min(1.0 - 1e-6, alpha))
    z = max(-LOGIT_LIMIT, min(LOGIT_LIMIT, math.log(bounded / (1.0 - bounded))))
    return (z + LOGIT_LIMIT) / (2.0 * LOGIT_LIMIT)


def encode_research(
    utility: DiagnosticUtility,
    ceiling: float,
    round_state: RoundState,
    budget: BudgetState,
    options: DiagnosticOptions,
    agent: AgentKind,
    max_rounds: int,
    contention: float,
    now: datetime,
    params: LadderParams,
) -> ResearchEncodedState:
    """The base columns, then the four added ones."""
    base = encode_base(
        utility=utility, ceiling=ceiling, round_state=round_state, budget=budget,
        options=options, agent=agent, max_rounds=max_rounds, contention=contention, now=now,
    )
    mine = _standing(round_state, agent)
    leader = _leader(round_state, agent)

    span = (budget.shift_end - budget.shift_start).total_seconds()
    elapsed = (now - budget.shift_start).total_seconds() / span if span > 0 else 0.0

    extra = (
        alpha_logit(ladder_alpha(mine, leader, ceiling, agent, params)),
        1.0 if (mine <= EPS and leader <= EPS) else 0.0,
        clamp(round_state.round_index / max(max_rounds, 1)),
        clamp(elapsed),
    )
    return ResearchEncodedState(
        values=base.values + extra, version=research_encoder_version(params)
    )


def describe(states: Sequence[ResearchEncodedState]) -> dict[str, tuple[float, float]]:
    """Per-feature ``(min, max)`` — the dead-column check, on the extended layout."""
    if not states:
        return {}
    columns = list(zip(*(state.values for state in states)))
    return {name: (min(col), max(col)) for name, col in zip(RESEARCH_FEATURES, columns)}


__all__ = [
    "EXTRA_FEATURES",
    "LOGIT_LIMIT",
    "RESEARCH_FEATURES",
    "LadderParams",
    "ResearchEncodedState",
    "alpha_logit",
    "base_encoder_version",
    "describe",
    "encode_research",
    "ladder_alpha",
    "research_encoder_version",
]
