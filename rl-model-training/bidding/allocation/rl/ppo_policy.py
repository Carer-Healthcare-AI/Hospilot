"""The PPO policy head: a masked categorical over the six actions, plus a Beta over alpha.

    logits(s)  =  W · s + b            one row per action, masked to the feasible set
    p(a | s)   =  softmax(logits)_a    renormalised over the feasible set ONLY
    alpha ~ Beta(a(s), b(s))           a = 1 + softplus(u·s + p), b likewise

Three decisions worth stating, because each one is a place this could be silently wrong.

**The mask is applied before normalisation, not after sampling.** PPO's importance ratio is
``pi_new(a|s) / pi_old(a|s)``, and both sides must be densities over the *same* support. Masking
after the fact leaves a ratio between two things that are not probabilities. So the mask is part
of ``log_prob``, and the mask that was live at sampling time is recorded per step and replayed at
update time.

**Beta parameters are ``1 + softplus(z)``, never ``exp(z)`` or raw softplus.** With ``a`` or ``b``
below 1 the Beta is U-shaped with infinite density at the endpoints, and a policy that discovers
that gets unbounded log-probability for alpha = 0 or 1 — the gradient equivalent of a divide by
zero. Pinning both at or above 1 keeps the density unimodal and finite. At the zero init
``a = b = 1 + log 2 = 1.693``, so alpha starts centred on 0.5, which is where ``train.py:278``
deliberately starts CEM.

**This subclasses :class:`LinearQPolicy` to inherit ``_encode``, ``_feasible`` and ``_plan``
verbatim.** Not for convenience — for validity. §6's comparison is only meaningful if the PPO arm
sees the same state vector and the same feasibility semantics as the CEM arm, and the way to
guarantee that is to run the same code rather than a second copy of it. Only the *choice* of
action is overridden. The inherited ``QWeights`` slot is a zeros placeholder that is never read;
``_q`` and ``_alpha`` are shadowed to raise if anything ever reaches them.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from allocation.config import Config
from allocation.contracts import (
    Action,
    AgentKind,
    BudgetState,
    Candidate,
    Decision,
    FeatureSnapshot,
    PathwayOptions,
    QAction,
    RoundState,
    UtilityBreakdown,
)
from allocation.rl.encoder import ACTION_INDEX, ACTIONS, SIZE, StateEncoder
from allocation.rl.policy import LinearQPolicy, QWeights

#: 6 action rows + 2 Beta heads + 1 value head, each over SIZE features plus a bias.
PARAM_COUNT = len(ACTIONS) * (SIZE + 1) + 2 * (SIZE + 1) + (SIZE + 1)

#: Actions that carry an alpha. The Beta density only enters ``log_prob`` for these, because an
#: exit has no aggression to express and recording one would make the ratio depend on a number
#: the auction never used.
_BIDS = tuple(a for a in ACTIONS if not a.exits)

EPS = 1e-6


def softplus(z: np.ndarray | float) -> np.ndarray:
    """``log(1 + e^z)``, in the form that does not overflow for large ``z``."""
    z = np.asarray(z, dtype=float)
    return np.maximum(z, 0.0) + np.log1p(np.exp(-np.abs(z)))


def sigmoid(z: np.ndarray | float) -> np.ndarray:
    """``d/dz softplus(z)``. Also the logistic, which is why it appears twice below."""
    z = np.asarray(z, dtype=float)
    return np.where(z >= 0.0, 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30))),
                    np.exp(np.clip(z, -30, 30)) / (1.0 + np.exp(np.clip(z, -30, 30))))


# ---------------------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------------------


@dataclass
class PPOWeights:
    """The 207 parameters. Mutable, because a gradient method updates in place.

    Versioned against the encoder and the fabrication register for the same reason
    :class:`QWeights` is: a policy served a different state vector is not degraded, it is
    undefined, and a policy about a different fabricated world is a policy about nothing.
    """

    logit_rows: np.ndarray                      # (6, SIZE)
    logit_biases: np.ndarray                    # (6,)
    a_row: np.ndarray                           # (SIZE,)  Beta first shape
    a_bias: float
    b_row: np.ndarray                           # (SIZE,)  Beta second shape
    b_bias: float
    v_row: np.ndarray                           # (SIZE,)  value head
    v_bias: float
    encoder_version: str
    fabrication_version: str = ""
    policy_version: str = "rl-ppo-linear-v1"

    @classmethod
    def zeros(cls, encoder_version: str, fabrication_version: str = "") -> "PPOWeights":
        return cls(
            logit_rows=np.zeros((len(ACTIONS), SIZE)),
            logit_biases=np.zeros(len(ACTIONS)),
            a_row=np.zeros(SIZE), a_bias=0.0,
            b_row=np.zeros(SIZE), b_bias=0.0,
            v_row=np.zeros(SIZE), v_bias=0.0,
            encoder_version=encoder_version,
            fabrication_version=fabrication_version,
        )

    # -- flat vector, for the finite-difference test -----------------------------------

    def flat(self) -> np.ndarray:
        """One contiguous vector. Exists so §8's gradient check can perturb one scalar."""
        return np.concatenate([
            self.logit_rows.ravel(), self.logit_biases,
            self.a_row, [self.a_bias],
            self.b_row, [self.b_bias],
            self.v_row, [self.v_bias],
        ])

    def set_flat(self, values: np.ndarray) -> None:
        v = np.asarray(values, dtype=float)
        if v.size != PARAM_COUNT:
            raise ValueError(f"expected {PARAM_COUNT} parameters, got {v.size}")
        n, k = len(ACTIONS), SIZE
        cursor = 0
        self.logit_rows = v[cursor:cursor + n * k].reshape(n, k).copy(); cursor += n * k
        self.logit_biases = v[cursor:cursor + n].copy();                 cursor += n
        self.a_row = v[cursor:cursor + k].copy();                        cursor += k
        self.a_bias = float(v[cursor]);                                  cursor += 1
        self.b_row = v[cursor:cursor + k].copy();                        cursor += k
        self.b_bias = float(v[cursor]);                                  cursor += 1
        self.v_row = v[cursor:cursor + k].copy();                        cursor += k
        self.v_bias = float(v[cursor])

    def copy(self) -> "PPOWeights":
        out = PPOWeights.zeros(self.encoder_version, self.fabrication_version)
        out.set_flat(self.flat())
        out.policy_version = self.policy_version
        return out

    # -- persistence -------------------------------------------------------------------

    def save(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "policy_version": self.policy_version,
            "encoder_version": self.encoder_version,
            "fabrication_version": self.fabrication_version,
            "actions": [a.value for a in ACTIONS],
            "logit_rows": self.logit_rows.tolist(),
            "logit_biases": self.logit_biases.tolist(),
            "a_row": self.a_row.tolist(), "a_bias": self.a_bias,
            "b_row": self.b_row.tolist(), "b_bias": self.b_bias,
            "v_row": self.v_row.tolist(), "v_bias": self.v_bias,
        }, indent=2), encoding="utf-8")
        return out

    @classmethod
    def load(cls, path: str | Path, encoder: StateEncoder | None = None) -> "PPOWeights":
        """Load, refusing a mismatched encoding or a reordered action list.

        Same two refusals as :meth:`QWeights.load`, for the same reason: both are cases where
        the file loads, the policy runs, and every number it emits is about something else.
        """
        body = json.loads(Path(path).read_text(encoding="utf-8"))
        out = cls(
            logit_rows=np.array(body["logit_rows"], dtype=float),
            logit_biases=np.array(body["logit_biases"], dtype=float),
            a_row=np.array(body["a_row"], dtype=float), a_bias=float(body["a_bias"]),
            b_row=np.array(body["b_row"], dtype=float), b_bias=float(body["b_bias"]),
            v_row=np.array(body["v_row"], dtype=float), v_bias=float(body["v_bias"]),
            encoder_version=str(body["encoder_version"]),
            fabrication_version=str(body.get("fabrication_version", "")),
            policy_version=str(body.get("policy_version", "rl-ppo-linear-v1")),
        )
        current = (encoder or StateEncoder()).version
        if out.encoder_version != current:
            raise ValueError(
                f"policy was fitted under encoder {out.encoder_version} but this build encodes "
                f"as {current}. The vector's features have changed, so the weights refer to "
                "different quantities. Refit rather than reinterpret."
            )
        if [a.value for a in ACTIONS] != list(body["actions"]):
            raise ValueError(
                "the action ordering has changed since this policy was fitted; a logit vector "
                "is only interpretable against the ordering it was trained on."
            )
        if out.logit_rows.shape != (len(ACTIONS), SIZE):
            raise ValueError(f"logit_rows is {out.logit_rows.shape}, expected "
                             f"{(len(ACTIONS), SIZE)}")
        return out


# ---------------------------------------------------------------------------------------
# The distribution
# ---------------------------------------------------------------------------------------


def action_logprobs(
    weights: PPOWeights, states: np.ndarray, masks: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Masked log-softmax over the six actions. Returns ``(logp_all, probs)``, both ``(N, 6)``.

    Infeasible entries are ``-inf`` in ``logp_all`` and exactly ``0.0`` in ``probs``, so a
    downstream sum over actions never picks up a contribution from something that could not
    have been chosen.
    """
    logits = states @ weights.logit_rows.T + weights.logit_biases
    logits = np.where(masks, logits, -np.inf)
    shift = np.max(logits, axis=1, keepdims=True)
    exp = np.where(masks, np.exp(logits - shift), 0.0)
    total = np.sum(exp, axis=1, keepdims=True)
    probs = exp / total
    logp_all = np.where(masks, logits - shift - np.log(total), -np.inf)
    return logp_all, probs


def beta_params(weights: PPOWeights, states: np.ndarray) -> tuple[np.ndarray, np.ndarray,
                                                                  np.ndarray, np.ndarray]:
    """``(a, b, z_a, z_b)``. The pre-activations come back because the gradient needs them."""
    z_a = states @ weights.a_row + weights.a_bias
    z_b = states @ weights.b_row + weights.b_bias
    return 1.0 + softplus(z_a), 1.0 + softplus(z_b), z_a, z_b


def beta_logpdf(x: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """``log Beta(x; a, b)``, with ``x`` assumed already clamped into ``(0, 1)``."""
    from scipy.special import gammaln
    return ((a - 1.0) * np.log(x) + (b - 1.0) * np.log1p(-x)
            + gammaln(a + b) - gammaln(a) - gammaln(b))


def values(weights: PPOWeights, states: np.ndarray) -> np.ndarray:
    return states @ weights.v_row + weights.v_bias


# ---------------------------------------------------------------------------------------
# One recorded decision
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepLog:
    """What the policy did, at the moment it did it.

    **The state here is the one the sample came from**, which is why this record exists at all.
    ``sim.dataset.Transition`` re-encodes from the closed ``AuctionResult`` and the *final* bid
    (``dataset.py:333-338``), so its ``state`` is not the vector the policy was shown mid-round.
    Computing ``log pi_new`` on that vector would put a different state on each side of the
    importance ratio — a bug that produces a perfectly plausible training curve.
    """

    auction_id: str
    agent: AgentKind
    round_index: int
    state: tuple[float, ...]
    mask: tuple[bool, ...]
    action_index: int
    alpha: float | None
    logp: float
    value: float
    entropy: float


class PPOPolicy(LinearQPolicy):
    """Stochastic at collection, deterministic at serving, and a recorder either way."""

    def __init__(
        self,
        config: Config,
        weights: PPOWeights,
        encoder: StateEncoder | None = None,
        rng: random.Random | None = None,
        deterministic: bool = False,
        mask_unplanned: bool = True,
        name: str | None = None,
    ) -> None:
        encoder = encoder or StateEncoder()
        # The inherited QWeights slot is a placeholder. `_q` and `_alpha` are shadowed below so
        # that anything reading it fails loudly instead of quietly bidding on zeros.
        super().__init__(
            config,
            QWeights.zeros(encoder.version, weights.fabrication_version),
            encoder,
            name=name or f"rl:{weights.policy_version}",
        )
        if weights.encoder_version and weights.encoder_version != encoder.version:
            raise ValueError(
                f"weights encode as {weights.encoder_version}, this encoder as "
                f"{encoder.version}"
            )
        self.ppo_weights = weights
        self._rng = rng or random.Random(0)
        self._deterministic = deterministic
        self._mask_unplanned = mask_unplanned
        self.log: list[StepLog] = []

    # -- the placeholder guards --------------------------------------------------------

    def _q(self, state):  # type: ignore[override]
        raise AssertionError("PPOPolicy has no per-action value estimate; only V(s)")

    def _alpha(self, state):  # type: ignore[override]
        raise AssertionError("PPOPolicy draws alpha from a Beta, not a logistic head")

    @property
    def weights(self):  # type: ignore[override]
        raise AssertionError("read PPOPolicy.ppo_weights; the QWeights slot is a placeholder")

    # -- the mask ----------------------------------------------------------------------

    def learned_feasible(self, feasible: frozenset[QAction]) -> frozenset[QAction]:
        """The feasible set the *learned* head may choose from. Experiment A's constraint.

        ``WITHDRAW_UNPLANNED`` is removed — it is the only action the ``aband == 0`` criterion
        counts, and ``auction.yaml:97``'s ``never_abandon_when_planned_exit_available`` says the
        same thing in the rulebook.

        **The removal is conditional, and it has to be.** ``_feasible`` guarantees
        ``WITHDRAW_UNPLANNED`` is always in the set (``policy.py:288-291``) precisely because a
        patient who cannot win, has no alternative and has no predicted bed *is* abandoned. In
        that state it is the only member, and subtracting it would leave the policy with an
        empty support — no action, and a softmax over nothing. So when removal would empty the
        set, it is put back: the constraint is *never choose abandonment when anything else is
        available*, which is what the rule actually says.
        """
        if not self._mask_unplanned:
            return feasible
        reduced = feasible - {QAction.WITHDRAW_UNPLANNED}
        return reduced if reduced else feasible

    # -- the seam ----------------------------------------------------------------------

    def decide_q(
        self,
        candidate: Candidate,
        utility: UtilityBreakdown,
        ceiling: float,
        round_state: RoundState,
        budget: BudgetState,
        snapshot: FeatureSnapshot,
        pathways: PathwayOptions | None = None,
    ) -> Decision:
        state = self._encode(
            candidate, utility, ceiling, round_state, budget, snapshot, pathways
        )
        feasible = self._feasible(candidate, ceiling, round_state, pathways)
        allowed = self.learned_feasible(feasible)
        if not allowed:
            raise AssertionError(
                f"empty action support at {round_state.auction_id} for {candidate.agent.value}; "
                "the mask stripped every feasible action"
            )

        w = self.ppo_weights
        s = np.asarray(state, dtype=float)[None, :]
        mask = np.array([[a in allowed for a in ACTIONS]])
        logp_all, probs = action_logprobs(w, s, mask)
        # Guard the product, not just the sum: a masked entry is 0 * -inf, which is nan before
        # any outer where() gets to discard it.
        safe = np.where(probs > 0.0, logp_all, 0.0)
        entropy = float(-np.sum(probs * safe))

        if self._deterministic:
            index = int(np.argmax(np.where(mask[0], logp_all[0], -np.inf)))
        else:
            index = self._sample_index(probs[0])
        chosen = ACTIONS[index]

        alpha: float | None = None
        logp = float(logp_all[0, index])
        if not chosen.exits:
            a, b, _, _ = beta_params(w, s)
            if self._deterministic:
                # The Beta mode, for a >= 1 and b >= 1. Deterministic serving must not sample
                # (§0), and the mode is the point the density actually prefers; the mean would
                # be pulled off it whenever the distribution is skewed.
                alpha = float((a[0] - 1.0) / (a[0] + b[0] - 2.0)) if a[0] + b[0] > 2.0 else 0.5
            else:
                alpha = self._rng.betavariate(float(a[0]), float(b[0]))
            alpha = min(1.0 - EPS, max(EPS, float(alpha)))
            logp += float(beta_logpdf(np.array([alpha]), a, b)[0])

        self.log.append(StepLog(
            auction_id=round_state.auction_id,
            agent=candidate.agent,
            round_index=round_state.round_index,
            state=tuple(float(x) for x in state),
            mask=tuple(bool(m) for m in mask[0]),
            action_index=index,
            alpha=alpha,
            logp=logp,
            value=float(values(w, s)[0]),
            entropy=entropy,
        ))

        # `q_values` is the audit channel for "what the policy thought of every option it
        # considered" (contracts.py:609-612). A PPO head has no per-action value, so it
        # publishes the masked log-probabilities — its actual ranking — rather than inventing
        # Q-like scores that would read as value estimates in the audit log.
        published = {a: float(logp_all[0, ACTION_INDEX[a]]) for a in sorted(
            allowed, key=lambda x: ACTION_INDEX[x])}

        if not chosen.exits:
            action = Action.INCREASE_BID if (alpha or 0.0) > 1e-6 else Action.HOLD
            return Decision(
                q_action=chosen, action=action,
                alpha=alpha if action is Action.INCREASE_BID else None,
                q_values=published, feasible=feasible,
            )
        return Decision(
            q_action=chosen, action=Action.WITHDRAW,
            plan=self._plan(chosen, pathways),
            q_values=published, feasible=feasible,
        )

    def _sample_index(self, probs: np.ndarray) -> int:
        """Inverse-CDF sampling off the injected ``random.Random``.

        Deliberately not ``numpy.random``: determinism is a §1 exit criterion, and one seeded
        ``Random`` threaded in from the caller is easier to prove reproducible than a global
        numpy state that any imported module can disturb.
        """
        u = self._rng.random()
        total = 0.0
        for i, p in enumerate(probs):
            total += float(p)
            if u <= total:
                return i
        return int(np.argmax(probs))


class MixedPPOPolicy:
    """The learning agent uses the PPO head; everybody else stays on the heuristic.

    Same construction and the same reason as :class:`allocation.rl.train.MixedPolicy`
    (``train.py:223-250``): two of the three bidders are genuinely frozen, so a change in return
    is attributable to the third.
    """

    def __init__(
        self,
        config: Config,
        weights: PPOWeights,
        agent: AgentKind,
        encoder: StateEncoder | None = None,
        rng: random.Random | None = None,
        deterministic: bool = False,
        mask_unplanned: bool = True,
    ) -> None:
        from allocation.policy.heuristic import HeuristicPolicy

        self.learner = PPOPolicy(
            config, weights, encoder, rng,
            deterministic=deterministic, mask_unplanned=mask_unplanned,
        )
        self._baseline = HeuristicPolicy(config)
        self._agent = agent
        self.name = f"ppo:{agent.value}"

    @property
    def log(self) -> list[StepLog]:
        return self.learner.log

    def decide(self, candidate: Candidate, *args, **kwargs):
        target = self.learner if candidate.agent is self._agent else self._baseline
        return target.decide(candidate, *args, **kwargs)

    def decide_q(self, candidate: Candidate, *args, **kwargs):
        target = self.learner if candidate.agent is self._agent else self._baseline
        return target.decide_q(candidate, *args, **kwargs)
