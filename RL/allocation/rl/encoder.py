"""The state vector. Frozen, versioned, and the thing a policy's validity is pinned to.

**F-24**: RL-Steps gives two different state lists, neither binding. That is not a documentation
problem — a policy is a function of its encoding, so a policy trained on one vector and served
another is not degraded, it is *undefined*. The parameters mean different things.

Three rules this module enforces.

**The order is fixed and the version is a hash of it.** :attr:`StateEncoder.version` hashes the
feature names, so adding, removing or reordering a feature changes the version. A trained policy
stores the version it was fitted under and refuses to load against a different one. Nothing
about that is optional: a silently reordered vector produces a policy that runs, emits plausible
alphas, and is wrong in a way no test would catch.

**Everything is normalised to [0, 1] against a stated scale.** Points, minutes and bed counts
have no common scale, and a linear model over raw units learns coefficient magnitudes that are
really unit conversions. The divisors are declared next to the features so the vector can be
read back into human terms.

**Absence is encoded as a value plus a presence flag, never as zero.** This is the same rule the
utility layer enforces via ``Signal``, and it matters more here: a policy given ``0.0`` for an
unknown safe-wait window learns "unknown means the patient cannot wait", which is precisely
backwards from what the exits do with that state. Every feature that can be missing carries a
companion ``*_known`` flag, so the model can learn a separate response to ignorance.

The features are deliberately few. RL_READINESS §5.3 ② puts the representation ladder at
"parametric rules + CEM → tabular Q → linear Q → network", and with roughly six auctions per
department per shift there is nowhere near the data to fit a wide vector. Twenty features is
already generous for the sample sizes involved.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from allocation.contracts import AgentKind, BudgetState, FeatureSnapshot, QAction

#: ``(name, divisor)`` — the divisor is the value that maps to 1.0. Order is load-bearing.
FEATURES: tuple[tuple[str, float], ...] = (
    # --- the bid position ------------------------------------------------------------
    ("utility", 200.0),            # the 0-200 scale RL-Steps §2 defines
    ("ceiling", 200.0),
    ("headroom", 200.0),           # ceiling - standing bid: what is left to expose
    ("standing_bid", 200.0),
    ("leader_bid", 200.0),         # the highest rival, which §14 requires observing
    ("behind_by", 200.0),          # leader - mine, clamped at 0; the overtaking cost
    ("is_leading", 1.0),
    # --- competition ------------------------------------------------------------------
    ("n_bidders", 4.0),
    ("contention", 1.3),           # the clamp ceiling from budget/spend.py
    ("round_index", 3.0),
    ("rounds_left", 3.0),
    # --- the budget, which is what makes this a sequential problem --------------------
    ("budget_remaining", 1200.0),
    ("burn_rate", 1.5),            # the "starved" band boundary
    ("shift_fraction_elapsed", 1.0),
    # --- the world --------------------------------------------------------------------
    ("occupancy", 1.0),
    ("boarding", 15.0),
    # --- what the exits need ----------------------------------------------------------
    ("safe_wait", 240.0),          # minutes, against the 4 h allocation horizon
    ("safe_wait_known", 1.0),
    ("alternative_hold", 240.0),   # best usable alternative's safe-hold, minutes
    ("alternative_available", 1.0),
    ("release_probability", 1.0),
    ("release_known", 1.0),
)

NAMES: tuple[str, ...] = tuple(name for name, _ in FEATURES)
SIZE = len(FEATURES)


@dataclass(frozen=True, slots=True)
class StateEncoder:
    """Encodes one agent's situation into a fixed vector.

    Frozen by construction: there are no options. An encoder with knobs is an encoder whose
    version does not determine its output, which defeats the point of versioning it.
    """

    @property
    def version(self) -> str:
        """Content hash of the feature list. Changes whenever the vector's meaning changes."""
        return hashlib.sha256("|".join(NAMES).encode()).hexdigest()[:12]

    @property
    def size(self) -> int:
        return SIZE

    def encode(
        self,
        agent: AgentKind,
        utility: float,
        ceiling: float,
        budget: BudgetState,
        result: Any,
        snapshot: FeatureSnapshot,
        options: Any = None,
        round_index: int | None = None,
    ) -> tuple[float, ...]:
        """Build the vector. Every value is clamped to ``[0, 1]``.

        Clamping rather than letting values run past 1.0 keeps a single outlier — a ceiling
        above 200 after an uplift, a burn rate above 1.5 — from dominating a linear model's
        gradient. The information lost at the boundary is small; the instability it prevents
        is not.
        """
        position = result.positions.get(agent) if hasattr(result, "positions") else None
        mine = position.current_bid if position else 0.0
        leader = self._leader_bid(result, agent)
        index = round_index if round_index is not None else max(0, result.rounds_run - 1)

        shift_span = (budget.shift_end - budget.shift_start).total_seconds() or 1.0
        elapsed = (snapshot.taken_at - budget.shift_start).total_seconds() / shift_span

        alternative = getattr(options, "best_alternative", None) if options else None
        wait = getattr(options, "safe_wait_minutes", None) if options else None
        probability = getattr(options, "next_release_probability", None) if options else None

        raw: dict[str, float] = {
            "utility": utility,
            "ceiling": ceiling,
            "headroom": max(0.0, ceiling - mine),
            "standing_bid": mine,
            "leader_bid": leader,
            "behind_by": max(0.0, leader - mine),
            "is_leading": 1.0 if mine >= leader else 0.0,
            "n_bidders": float(len(result.positions)) if hasattr(result, "positions") else 1.0,
            "contention": float(getattr(result, "contention", 1.0)),
            "round_index": float(index),
            "rounds_left": float(max(0, getattr(result, "max_rounds", 1) - index - 1)),
            "budget_remaining": budget.budget_remaining,
            "burn_rate": budget.burn_rate,
            "shift_fraction_elapsed": elapsed,
            "occupancy": snapshot.hospital.occupancy,
            "boarding": float(snapshot.hospital.boarding_count or 0),
            # Absent stays absent: the value is 0.0 but the flag says so, and the model is free
            # to learn a distinct response to "nobody can vouch for this patient waiting".
            "safe_wait": wait if wait is not None else 0.0,
            "safe_wait_known": 1.0 if wait is not None else 0.0,
            "alternative_hold": alternative.safe_hold_minutes if alternative else 0.0,
            "alternative_available": 1.0 if alternative else 0.0,
            "release_probability": probability if probability is not None else 0.0,
            "release_known": 1.0 if probability is not None else 0.0,
        }

        return tuple(
            max(0.0, min(1.0, raw[name] / divisor)) for name, divisor in FEATURES
        )

    @staticmethod
    def _leader_bid(result: Any, agent: AgentKind) -> float:
        """The best standing bid held by somebody else.

        Excluding the agent's own is what §13 requires — ER reads "highest opponent = 75" while
        itself holding 85. An encoder that included it would tell a leading policy it was
        behind itself.
        """
        if not hasattr(result, "positions"):
            return 0.0
        rivals = [p.current_bid for a, p in result.positions.items() if a is not agent]
        return max(rivals, default=0.0)

    def describe(self, vector: Sequence[float]) -> str:
        """The vector in human terms. For reading a decision back out of a log."""
        return "\n".join(
            f"  {name:<24} {value:>6.3f}  (x{divisor:g} = {value * divisor:.1f})"
            for (name, divisor), value in zip(FEATURES, vector)
        )


#: Which of the six actions the value function scores. Fixed alongside the encoder, because a
#: Q-vector is only interpretable against a known action ordering — the same argument as for the
#: feature order, and the same failure mode if it drifts.
ACTIONS: tuple[QAction, ...] = (
    QAction.WIN_NOW,
    QAction.CONTINUE,
    QAction.WITHDRAW_ALTERNATIVE,
    QAction.AWAIT_NEXT_RESOURCE,
    QAction.RE_ENTER_LATER,
    QAction.WITHDRAW_UNPLANNED,
)

ACTION_INDEX: Mapping[QAction, int] = {a: i for i, a in enumerate(ACTIONS)}
