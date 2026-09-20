"""The bed-style diagnostic model and the policy that serves it.

**Split out of ``research_q`` so that serving does not drag in the experiment harness.**
``research_q`` imports ``baseline_q``, which pulls the collection, the explorers, the paired
statistics and the world generators — none of which a request-time inference path needs, and
none of which the deployment tree even ships. Everything required to *evaluate* a trained
policy lives here; everything required to *fit* one stays in ``research_q``, which imports
these two classes rather than defining its own.

That direction matters. The alternative — a second serving implementation in the deployment
tree — is how a served policy silently stops being the policy that was validated. Here there
is exactly one :class:`ResearchServedPolicy`, and it is the one
``results/diagnostic_machine/bedstyle_q.json`` was produced with.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Sequence

from allocation.config import Config
from allocation.use_cases.diagnostic_machine.config import diagnostic_rules
from allocation.use_cases.diagnostic_machine.dataset import (
    DISCOUNT_HORIZON_MINUTES,
    step_discount,
)
from allocation.use_cases.diagnostic_machine.ladder_reachability import reachable_actions
from allocation.use_cases.diagnostic_machine.masks import (
    ACTIONS,
    action_space_version,
    argmax,
    feasible_actions,
    mask,
)
from allocation.use_cases.diagnostic_machine.oracle_fork import build_forced_decision
from allocation.use_cases.diagnostic_machine.policy import DiagnosticOptions
from allocation.use_cases.diagnostic_machine.research_encoder import (
    RESEARCH_FEATURES,
    LadderParams,
    encode_research,
)


@dataclass
class BedStyleQLearner:
    """Linear Q over :data:`RESEARCH_FEATURES`, fitted the way bed fits its own.

    Semi-gradient TD on replayed minibatches, bootstrapping off a frozen copy synced once per
    round, with the step Huber-clipped in scaled reward units and the next-state max taken over
    the recorded feasibility mask only. See ``research_q``'s module docstring for why each of
    those four pieces is here and what happened in this family without them.
    """

    encoder_version: str
    n_features: int = len(RESEARCH_FEATURES)
    gamma: float = 0.99
    learning_rate: float = 0.02
    huber_delta: float = 1.0
    reward_scale: float = 1.0
    double_q: bool = True
    discount_horizon_minutes: float = DISCOUNT_HORIZON_MINUTES

    rows: list[list[float]] = field(default_factory=list)
    bias: list[float] = field(default_factory=list)
    fit_counts: list[int] = field(default_factory=lambda: [0] * len(ACTIONS))
    action_space_version: str = field(default_factory=action_space_version)
    _target_rows: list[list[float]] = field(default_factory=list)
    _target_bias: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.rows:
            self.rows = [[0.0] * self.n_features for _ in ACTIONS]
            self.bias = [0.0] * len(ACTIONS)
        self.sync_target()

    # -- inference ---------------------------------------------------------------------

    def sync_target(self) -> None:
        """Freeze the current weights as the bootstrap target. Called between rounds."""
        self._target_rows = [list(r) for r in self.rows]
        self._target_bias = list(self.bias)

    def _q(self, state: Sequence[float], index: int, target: bool = False) -> float:
        rows, bias = (
            (self._target_rows, self._target_bias) if target else (self.rows, self.bias)
        )
        return sum(w * x for w, x in zip(rows[index], state)) + bias[index]

    def q_values(self, state: Sequence[float]) -> list[float]:
        return [self._q(state, i) for i in range(len(ACTIONS))]

    def max_q(self, state: Sequence[float], allowed: Sequence[bool]) -> float:
        """Bootstrap value of ``state``, over the recorded mask only.

        With ``double_q`` the ONLINE weights choose and the FROZEN weights value. A single max
        over one noisy estimator is biased upward, and under bootstrapping that bias compounds
        — the mechanism this family measured at +18.44 per bootstrapping head.
        """
        indices = [i for i, ok in enumerate(allowed) if ok] or list(range(len(ACTIONS)))
        if not self.double_q:
            return max(self._q(state, i, target=True) for i in indices)
        choice = max(indices, key=lambda i: self._q(state, i, target=False))
        return self._q(state, choice, target=True)

    # -- the update --------------------------------------------------------------------

    def update(self, batch: Sequence[object]) -> float:
        """One semi-gradient pass over a minibatch. Returns the mean absolute TD error.

        The number to watch: it should fall and then flatten. A steady rise is the linear-TD
        divergence the frozen target and the Huber cut exist to prevent.
        """
        if not batch:
            return 0.0
        errors: list[float] = []
        for row in batch:
            state = row.state
            index = row.action

            target = row.reward / self.reward_scale
            if not row.done and row.next_state is not None and row.next_mask is not None:
                factor = step_discount(
                    self.gamma, row.elapsed_minutes, self.discount_horizon_minutes
                )
                target += factor * self.max_q(row.next_state, row.next_mask)

            predicted = self._q(state, index)
            delta = target - predicted
            errors.append(abs(delta))

            # Huber: linear beyond the cut, so one outlier moves the weights by a bounded step.
            # ``row.weight`` is the same importance correction the BASELINE's least squares
            # applies, so both arms weight the same rows identically.
            clipped = max(-self.huber_delta, min(self.huber_delta, delta))
            step = self.learning_rate * getattr(row, "weight", 1.0) * clipped
            for i, feature in enumerate(state):
                self.rows[index][i] += step * feature
            self.bias[index] += step
            self.fit_counts[index] += 1
        return statistics.fmean(errors)

    # -- diagnostics -------------------------------------------------------------------

    def finite(self) -> bool:
        return all(
            math.isfinite(v) for row in self.rows for v in row
        ) and all(math.isfinite(b) for b in self.bias)

    def weight_norm(self) -> float:
        return sum(v * v for row in self.rows for v in row) ** 0.5

    def unfitted_heads(self) -> list[str]:
        return [ACTIONS[i].value for i, n in enumerate(self.fit_counts) if n == 0]

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "diagnostic_bedstyle_q_linear",
            "encoder_version": self.encoder_version,
            "action_space_version": self.action_space_version,
            "features": list(RESEARCH_FEATURES),
            "actions": [a.value for a in ACTIONS],
            "rows": [list(r) for r in self.rows],
            "bias": list(self.bias),
            "fit_counts": list(self.fit_counts),
            "gamma": self.gamma,
            "reward_scale": self.reward_scale,
            "huber_delta": self.huber_delta,
            "double_q": self.double_q,
            "discount_horizon_minutes": self.discount_horizon_minutes,
        }


class ResearchServedPolicy:
    """``state -> per-action scores`` behind the auction's ``decide`` seam.

    Shared by both experiment arms and by production serving: BASELINE scores with its ridge
    rows, Q with its TD rows, and both route through ``build_forced_decision`` so the excluded
    heuristic is never constructed.

    The argmax ranges over the LADDER-REACHABLE set, not the raw feasible set. Choosing a
    compete action the ladder has already decided to override does not produce that action — it
    produces ``_best_available_exit(feasible)``, a fixed rule that ignores the policy's own
    ranking over the exits. Restricting the argmax here is what makes the action the policy
    picks the action that actually happens.
    """

    def __init__(self, config: Config, params: LadderParams, score_fn, name: str) -> None:
        self._config = config
        self._params = params
        self._score_fn = score_fn
        rules = diagnostic_rules()
        self._min_capacity_p = float(rules["next_capacity"]["min_probability"])
        self.name = name

    def decide(self, agent, ceiling, round_state, budget, options: DiagnosticOptions, **context):
        state = encode_research(
            utility=context["utility"], ceiling=ceiling, round_state=round_state, budget=budget,
            options=options, agent=agent, max_rounds=context["max_rounds"],
            contention=context["contention"], now=options.now, params=self._params,
        )
        feasible = reachable_actions(
            self._config, agent, ceiling, round_state, budget,
            feasible_actions(options, self._min_capacity_p),
        )
        allowed = mask(feasible)
        chosen = argmax(self._score_fn(list(state.values)), allowed)
        return build_forced_decision(
            self._config, chosen, agent, options, feasible, ceiling, round_state, budget,
        )


__all__ = ["BedStyleQLearner", "ResearchServedPolicy"]
