"""Diagnostic Q-learning — a linear action-value model over the diagnostic encoder.

One weight vector per :class:`DiagnosticPathwayAction`, fitted by masked TD(0) on transitions
from :mod:`.dataset`. Linear rather than a network on purpose: the dataset is ~1000
transitions from eight scenarios, which is nowhere near enough to fit anything with hidden
layers, and a linear model's weights can be read and argued with.

**Three defects from the bed Q work are designed out rather than inherited.**

*An unfitted head must not win.* The bed policy emitted ``WITHDRAW_UNPLANNED`` seventeen times
for an action ε-greedy could never construct: its head was never fitted, so it sat at zero and
beat every fitted head that had gone negative. Here, :attr:`DiagnosticQPolicy.fit_counts`
records how many updates each head received, and a head with fewer than
:data:`MIN_FITS_TO_TRUST` is held at :data:`UNFITTED_VALUE` — well below any plausible fitted
value — so it can only be chosen when nothing else is available at all.

*The bootstrap must respect the mask.* ``max_a' Q(s', a')`` taken over all actions bootstraps
from actions the next state never offered. The target here maxes over the *next* state's mask.

*The fit must be checkable.* :meth:`report` prints per-head fit counts and weight norms,
because "the model trained" is not evidence that every head did.

**The discount is charged over clinical minutes, not per decision.** A fixed ``gamma`` per
decision pays a policy for spending extra bidding rounds on a request whose fate and timing it
did not change, and the terminal row's discount must cover the tail from the last decision to the
outcome or a policy can bank a bad fate at face value by going quiet. See
:data:`~allocation.use_cases.diagnostic_machine.dataset.DISCOUNT_HORIZON_MINUTES`.

Weights are stamped with the encoder, action-space, reward and **training-objective** versions
and refuse to load under different ones — a vector fitted on a different feature layout, or
against a different objective, is silently wrong rather than loudly wrong.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from allocation.use_cases.diagnostic_machine.contracts import DiagnosticPathwayAction
from allocation.use_cases.diagnostic_machine.dataset import (
    DISCOUNT_HORIZON_MINUTES,
    DiagnosticDataset,
    DiagnosticTransition,
    step_discount,
)
from allocation.use_cases.diagnostic_machine.encoder import FEATURES, encoder_version
from allocation.use_cases.diagnostic_machine.masks import (
    ACTIONS,
    INDEX,
    action_space_version,
    argmax,
)

#: Updates a head needs before its value is trusted at serving time.
MIN_FITS_TO_TRUST = 5

#: What an untrusted head is worth. Far below any reward the objective can produce, so an
#: unfitted head is chosen only when the mask leaves nothing else — never because it happened
#: to sit above a fitted head that had learned to be negative.
UNFITTED_VALUE = -1e6

#: What the weights were fitted to MAXIMISE. Bumped whenever the training objective changes
#: shape, independently of the encoder and the action space — a policy can be perfectly
#: layout-compatible and still be optimising a different thing.
#:
#: ``diagnostic_td_clock_v1`` is masked TD(0) whose discount is charged over CLINICAL MINUTES,
#: terminal tail included. Its predecessor, ``diagnostic_td_index_v0``, charged a fixed ``gamma``
#: per DECISION and closed each trajectory at the last decision rather than at the outcome. Those
#: are not a stale file format, they are a different objective: under it, keeping a doomed request
#: bidding discounts its own penalty, so the fit is rewarded for stalling. Loading such weights
#: here would serve that policy silently. See :meth:`DiagnosticQPolicy.load`.
TRAINING_OBJECTIVE_VERSION = "diagnostic_td_clock_v1"

#: Objectives this code refuses outright, with what was wrong with each.
RETIRED_OBJECTIVES: dict[str, str] = {
    "diagnostic_td_index_v0": (
        "a fixed discount per decision index, over a trajectory closed at the last decision "
        "rather than at the outcome; the fit it produces is paid for spending extra bidding "
        "rounds and for going quiet on a request it has given up on"
    ),
}


class IncompatibleObjective(ValueError):
    """An artifact was fitted to maximise something other than what this code serves."""


@dataclass
class DiagnosticQPolicy:
    """Linear Q over the diagnostic encoder, one weight vector per pathway action."""

    weights: list[list[float]] = field(
        default_factory=lambda: [[0.0] * len(FEATURES) for _ in ACTIONS]
    )
    bias: list[float] = field(default_factory=lambda: [0.0] * len(ACTIONS))
    fit_counts: list[int] = field(default_factory=lambda: [0] * len(ACTIONS))
    encoder_version: str = field(default_factory=encoder_version)
    action_space_version: str = field(default_factory=action_space_version)
    reward_version: str = ""
    rules_version: str = ""
    trained_on: int = 0
    #: ``R / (1 - gamma)`` from the fit. No true action value can exceed it.
    value_bound: float = 0.0
    #: The objective these weights were fitted to. See :data:`TRAINING_OBJECTIVE_VERSION`.
    training_objective_version: str = TRAINING_OBJECTIVE_VERSION
    #: The discount actually used, recorded so a served policy can be checked against the
    #: objective it claims rather than against the defaults of whatever code loads it.
    gamma: float = 0.0
    discount_horizon_minutes: float = 0.0

    name = "diagnostic_q"

    # -- inference -------------------------------------------------------------------------

    def q_values(self, state: Sequence[float]) -> list[float]:
        """Raw action values, before masking and before the unfitted-head guard."""
        return [
            sum(w * x for w, x in zip(self.weights[i], state)) + self.bias[i]
            for i in range(len(ACTIONS))
        ]

    def trusted_q_values(self, state: Sequence[float]) -> list[float]:
        """Action values with untrusted heads held down.

        This is what serving uses. The distinction from :meth:`q_values` matters for
        debugging: the raw values say what the model believes, these say what it is allowed
        to act on.
        """
        values = self.q_values(state)
        return [
            v if self.fit_counts[i] >= MIN_FITS_TO_TRUST else UNFITTED_VALUE
            for i, v in enumerate(values)
        ]

    def decide(
        self, state: Sequence[float], allowed: tuple[bool, ...]
    ) -> DiagnosticPathwayAction:
        """The best available action this policy is willing to stand behind."""
        return argmax(self.trusted_q_values(state), allowed)

    def unfitted_heads(self) -> list[str]:
        return [
            ACTIONS[i].value
            for i in range(len(ACTIONS))
            if self.fit_counts[i] < MIN_FITS_TO_TRUST
        ]

    # -- training --------------------------------------------------------------------------

    def fit(
        self,
        dataset: DiagnosticDataset,
        epochs: int = 40,
        lr: float = 0.005,
        gamma: float = 0.9,
        seed: int = 0,
        discount_horizon_minutes: float = DISCOUNT_HORIZON_MINUTES,
    ) -> "DiagnosticQPolicy":
        """Masked TD(0), swept over the dataset, with the target bounded.

        Shuffled per epoch with a seeded RNG so a run is reproducible: the same dataset and
        seed produce the same weights, which is what lets a change in the numbers mean a
        change in the code.

        **The target is clipped, and without it this diverges.** Linear approximation plus
        bootstrapping plus an off-policy ``max`` is the deadly triad, and this dataset walks
        straight into it: a request that waits has a NON-TERMINAL transition whose target is
        pure bootstrap, while a request that is served has a terminal one anchored to a real
        reward. The waiting head therefore grows without anything holding it down — measured
        at ``|w| = 138`` after 60 epochs — until the policy exits every auction it enters and
        every request expires. That is not undertraining; it is divergence, and it is visible
        as monotone weight growth in ``lr`` and ``epochs``.

        The clip is not a fudge factor. With rewards bounded by ``R`` and discount ``gamma``,
        no true action value can exceed ``R / (1 - gamma)``; a target beyond that describes a
        return the environment cannot produce. ``R`` is taken from the data rather than
        declared, so it moves with the reward table instead of going stale against it.
        """
        self._check_versions(dataset)
        rng = random.Random(seed)
        rows = list(dataset.transitions)

        observed = max((abs(row.reward) for row in rows), default=1.0)
        bound = observed / max(1.0 - gamma, 1e-6)
        self.value_bound = bound

        for epoch in range(epochs):
            rng.shuffle(rows)
            # Decayed so late sweeps refine rather than overwrite. A constant rate on a
            # bootstrapped target keeps re-chasing its own moving estimate.
            rate = lr / (1.0 + epoch / 10.0)
            for row in rows:
                self._update(row, rate, gamma, bound, discount_horizon_minutes)

        self.reward_version = dataset.reward_version
        self.rules_version = dataset.rules_version
        self.trained_on = len(rows)
        self.training_objective_version = TRAINING_OBJECTIVE_VERSION
        self.gamma = gamma
        self.discount_horizon_minutes = discount_horizon_minutes
        return self

    def _update(
        self,
        row: DiagnosticTransition,
        lr: float,
        gamma: float,
        bound: float,
        horizon: float = DISCOUNT_HORIZON_MINUTES,
    ) -> None:
        # ``elapsed_minutes`` is the clinical interval from THIS decision to whatever comes next
        # for this request: the next decision, or — on a terminal row — the moment the fate
        # actually occurred. Both the reward and the bootstrap arrive at the far end of that
        # interval, so both are discounted across it.
        #
        # Discounting over CLINICAL MINUTES rather than per decision is what stops a policy being
        # paid for spending extra bidding rounds on a request whose fate and timing it did not
        # change; carrying the terminal tail is what stops a bad outcome looking as though it
        # happened at the last bid rather than at the deadline. Several rounds inside one auction
        # are the same clinical instant and cost ``gamma ** 0 == 1``, which is correct: they are
        # not three hours apart, they are seconds apart.
        factor = step_discount(gamma, row.elapsed_minutes, horizon)
        target = factor * row.reward
        if not row.done and row.next_state is not None and row.next_mask is not None:
            # Bootstrap over the NEXT state's mask, not over every action. Maxing over actions
            # the next state never offered imports value from a branch that did not exist.
            future = [
                v
                for v, ok in zip(self.q_values(row.next_state), row.next_mask)
                if ok
            ]
            if future:
                target += factor * max(future)

        target = max(-bound, min(bound, target))
        i = row.action
        error = target - (
            sum(w * x for w, x in zip(self.weights[i], row.state)) + self.bias[i]
        )
        for j, x in enumerate(row.state):
            self.weights[i][j] += lr * error * x
        self.bias[i] += lr * error
        self.fit_counts[i] += 1

    def _check_versions(self, dataset: DiagnosticDataset) -> None:
        if dataset.encoder_version != self.encoder_version:
            raise ValueError(
                f"dataset was encoded under {dataset.encoder_version}, this policy expects "
                f"{self.encoder_version}; weights fitted across a feature-layout change are "
                "silently wrong, not loudly wrong"
            )
        if dataset.action_space_version != self.action_space_version:
            raise ValueError(
                f"dataset action space {dataset.action_space_version} != "
                f"{self.action_space_version}"
            )

    # -- persistence -----------------------------------------------------------------------

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "kind": "diagnostic_q_linear",
                    "encoder_version": self.encoder_version,
                    "action_space_version": self.action_space_version,
                    "reward_version": self.reward_version,
                    "rules_version": self.rules_version,
                    "features": list(FEATURES),
                    "actions": [a.value for a in ACTIONS],
                    "weights": self.weights,
                    "bias": self.bias,
                    "fit_counts": self.fit_counts,
                    "trained_on": self.trained_on,
                    "value_bound": self.value_bound,
                    "min_fits_to_trust": MIN_FITS_TO_TRUST,
                    "training_objective_version": self.training_objective_version,
                    "gamma": self.gamma,
                    "discount_horizon_minutes": self.discount_horizon_minutes,
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path) -> "DiagnosticQPolicy":
        """Load weights, refusing anything fitted under a different layout.

        Also refuses a *bed* artifact outright. The two families' weight files are the same
        shape of JSON, and loading one into the other would produce a policy that runs, emits
        numbers, and means nothing.

        And refuses an artifact fitted against a **different objective**, which is a strictly
        stronger check than the layout ones: a decision-index fit matches the encoder and the
        action space exactly and is still optimising something else.
        """
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("kind") != "diagnostic_q_linear":
            raise ValueError(
                f"{path.name} is not a diagnostic Q artifact (kind={data.get('kind')!r}); "
                "a bed policy loaded here would run and mean nothing"
            )
        objective = data.get("training_objective_version")
        if objective is None:
            raise IncompatibleObjective(
                f"{path.name} records no training_objective_version. Every artifact written "
                f"before {TRAINING_OBJECTIVE_VERSION!r} was fitted against a FIXED discount per "
                "DECISION, over a trajectory closed at the last decision instead of at the "
                "outcome. Those weights are not a stale file format — they encode a policy that "
                "is paid for extra bidding rounds and for going quiet. Refit rather than load."
            )
        objective = str(objective)
        if objective in RETIRED_OBJECTIVES:
            raise IncompatibleObjective(
                f"{path.name} was fitted under the retired objective {objective!r} "
                f"({RETIRED_OBJECTIVES[objective]}). Current objective is "
                f"{TRAINING_OBJECTIVE_VERSION!r}; refit rather than load."
            )
        if objective != TRAINING_OBJECTIVE_VERSION:
            raise IncompatibleObjective(
                f"{path.name} was fitted to maximise {objective!r}, this code serves "
                f"{TRAINING_OBJECTIVE_VERSION!r}. A policy can match the encoder and the "
                "action space exactly and still be optimising a different thing."
            )
        policy = cls(
            weights=[list(map(float, row)) for row in data["weights"]],
            bias=[float(b) for b in data["bias"]],
            fit_counts=[int(c) for c in data["fit_counts"]],
            encoder_version=str(data["encoder_version"]),
            action_space_version=str(data["action_space_version"]),
            reward_version=str(data.get("reward_version", "")),
            rules_version=str(data.get("rules_version", "")),
            trained_on=int(data.get("trained_on", 0)),
            value_bound=float(data.get("value_bound", 0.0)),
            training_objective_version=objective,
            gamma=float(data.get("gamma", 0.0)),
            discount_horizon_minutes=float(data.get("discount_horizon_minutes", 0.0)),
        )
        if policy.encoder_version != encoder_version():
            raise ValueError(
                f"{path.name} was fitted under encoder {policy.encoder_version}, current is "
                f"{encoder_version()}"
            )
        if policy.action_space_version != action_space_version():
            raise ValueError(f"{path.name} was fitted under a different action space")
        if policy.discount_horizon_minutes <= 0.0:
            raise IncompatibleObjective(
                f"{path.name} claims {TRAINING_OBJECTIVE_VERSION!r} but records "
                f"discount_horizon_minutes={policy.discount_horizon_minutes}. That objective is "
                "defined by a positive clinical-time horizon; a zero one is what a "
                "decision-index fit would leave behind."
            )
        return policy

    # -- reporting -------------------------------------------------------------------------

    def report(self) -> str:
        """Per-head fit counts and weight norms. "It trained" is not evidence every head did."""
        lines = [
            f"encoder {self.encoder_version}  actions {self.action_space_version}",
            f"reward  {self.reward_version}  rules {self.rules_version}",
            f"objective {self.training_objective_version}",
            f"gamma {self.gamma}  horizon {self.discount_horizon_minutes:g} min",
            f"fitted on {self.trained_on} transitions",
            "",
            f"  {'action':22} {'fits':>6} {'|w|':>9}  trusted",
        ]
        for i, action in enumerate(ACTIONS):
            norm = sum(w * w for w in self.weights[i]) ** 0.5
            trusted = "yes" if self.fit_counts[i] >= MIN_FITS_TO_TRUST else "NO - held down"
            lines.append(
                f"  {action.value:22} {self.fit_counts[i]:6} {norm:9.3f}  {trusted}"
            )
        return "\n".join(lines)

    def top_features(self, action: DiagnosticPathwayAction, n: int = 5) -> list[tuple[str, float]]:
        """The features this head leans on hardest, largest magnitude first."""
        row = self.weights[INDEX[action]]
        ranked = sorted(zip(FEATURES, row), key=lambda kv: -abs(kv[1]))
        return ranked[:n]


__all__ = [
    "DISCOUNT_HORIZON_MINUTES",
    "MIN_FITS_TO_TRUST",
    "RETIRED_OBJECTIVES",
    "TRAINING_OBJECTIVE_VERSION",
    "UNFITTED_VALUE",
    "DiagnosticQPolicy",
    "IncompatibleObjective",
]
