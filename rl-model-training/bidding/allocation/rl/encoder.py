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
import math
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
    # **The heuristic's own alpha, in logit space, because the head that consumes it is a
    # logistic.** Rule 4 of `policy/heuristic.py` sets aggression to exactly what overtaking
    # costs: `lead_alpha` when already ahead, else `(rival - mine + margin) / headroom`.
    #
    # Two reasons this is not simply that ratio. First, `behind_by` and `headroom` are both
    # already in this vector, but `alpha = sigmoid(w . state + c)` is LINEAR in the features and
    # cannot form a quotient of two of them — a head asked to learn one fits the best available
    # constant instead. Measured across 1718 competing states, a TD policy's alpha spanned
    # 0.467-0.518 while the heuristic's spanned 0.250-0.643.
    #
    # Second, supplying the ratio raw is still not enough: `sigmoid(w*x + b)` is not the
    # identity, so no weight reproduces `alpha = x`. Supplying its LOGIT does — `sigmoid` of it
    # at weight 1 returns the alpha exactly. Rescaled to [0, 1] because every feature here is,
    # so the head recovers the logit with a weight near `2 * LOGIT_LIMIT`.
    #
    # This hands the learner the heuristic's answer as one input. It is not a constraint: the
    # other 22 features remain, and the head is free to weight this at anything. The point is
    # that the hypothesis class now CONTAINS the behaviour being compared against.
    ("heuristic_alpha_logit", 1.0),
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

#: Mirrors `auction.yaml`'s `policy.heuristic` block, which `HeuristicPolicy` reads for the
#: same rule. Duplicated as constants rather than passed in because :class:`StateEncoder` takes
#: no options — an encoder with knobs is one whose version does not determine its output. The
#: version hash below covers them, so changing either invalidates every fitted policy, which is
#: the correct blast radius: the feature's MEANING changes.
#:
#: They must be kept in step with `auction.yaml:69` and `:72` by hand. A drift is not silent —
#: `tests/test_encoder.py` asserts they match the config.
LEAD_ALPHA = 0.25
OVERTAKE_MARGIN = 4.0

#: Logits are unbounded and every feature here is clamped to [0, 1], so the logit is squashed
#: into that band. +-6 covers alpha in (0.0025, 0.9975), which is the whole usable range.
LOGIT_LIMIT = 6.0


def _alpha_logit(mine: float, leader: float, ceiling: float) -> float:
    """The heuristic's alpha for this position, as a logit rescaled to [0, 1]."""
    headroom = ceiling - mine
    if headroom <= 1e-9:
        alpha = 0.0
    elif mine >= leader - 1e-9:
        alpha = LEAD_ALPHA
    else:
        alpha = (leader - mine + OVERTAKE_MARGIN) / headroom
    alpha = max(1e-6, min(1.0 - 1e-6, alpha))
    z = max(-LOGIT_LIMIT, min(LOGIT_LIMIT, math.log(alpha / (1.0 - alpha))))
    return (z + LOGIT_LIMIT) / (2.0 * LOGIT_LIMIT)


NAMES: tuple[str, ...] = tuple(name for name, _ in FEATURES)
SIZE = len(FEATURES)

# ---------------------------------------------------------------------------------------
# Bidding regimes
#
# The heuristic plays three different alpha rules, not one (`policy/ladder.py`): a per-agent
# opening percentage, a flat raise while leading, and a derived jump when overtaking. A single
# sigmoid-linear head has to serve all three, and they compete — fitting all three at once
# moved the leading regime from MAE 0.001 to 0.044 while fixing opening from 0.264.
#
# **Derived from the state vector, never from the label.** At serve time the policy does not
# know what the heuristic would have played; it knows only what it can encode. These three
# predicates read features the encoder already publishes, so a regime-conditioned head is
# usable in a live auction rather than only in a post-hoc analysis.
# ---------------------------------------------------------------------------------------

OPENING, LEADING, OVERTAKING = 0, 1, 2
REGIMES: tuple[str, ...] = ("opening", "leading", "overtaking")

_STANDING_BID = NAMES.index("standing_bid")
_LEADER_BID = NAMES.index("leader_bid")
_IS_LEADING = NAMES.index("is_leading")


def regime_of(state: "Sequence[float]") -> int:
    """Which of the heuristic's three alpha rules this state falls under.

    Opening is tested first and by *both* bids being zero: `is_leading` is true at the opening
    too (nobody is ahead of nobody), so testing leadership first would swallow every opening
    round into the leading regime and reintroduce the coverage failure in a new place.
    """
    if state[_STANDING_BID] <= 0.0 and state[_LEADER_BID] <= 0.0:
        return OPENING
    return LEADING if state[_IS_LEADING] >= 0.5 else OVERTAKING


@dataclass(frozen=True, slots=True)
class StateEncoder:
    """Encodes one agent's situation into a fixed vector.

    Frozen by construction: there are no options. An encoder with knobs is an encoder whose
    version does not determine its output, which defeats the point of versioning it.
    """

    @property
    def version(self) -> str:
        """Content hash of the feature list **and the constants that shape it**.

        `heuristic_alpha_logit` is computed from `LEAD_ALPHA`, `OVERTAKE_MARGIN` and
        `LOGIT_LIMIT`, so a change to any of them changes what the vector means without changing
        a single name. Hashing names alone would let two incompatible encoders share a version —
        exactly the failure versioning exists to prevent.
        """
        payload = "|".join(NAMES) + f"|{LEAD_ALPHA}|{OVERTAKE_MARGIN}|{LOGIT_LIMIT}"
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

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
            "heuristic_alpha_logit": _alpha_logit(mine, leader, ceiling),
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
