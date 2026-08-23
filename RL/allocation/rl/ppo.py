"""PPO against the simulator. Analytic gradients, on-policy, one agent learning.

**The environment is ``generate()``, and a rollout is a ``Dataset``.** PPO_EXPERIMENT_PLAN §1
originally specified a gym-shaped ``reset``/``step`` pair. That is the wrong seam here, for two
reasons found while building it:

1. ``sim.dataset.generate`` (``dataset.py:208``) is a closed loop that constructs the world, runs
   the auctions, rolls every participant to the horizon and *then* scores outcomes. Turning it
   inside out means either a coroutine rewrite or a second implementation, and a second
   implementation is exactly how the PPO arm ends up facing a subtly different simulator than the
   CEM arm — which would void §6's comparison silently.
2. ``step()`` could not honestly return a reward when called. The reward for a decision is scored
   four hours of simulated time later, once the patient's fate is known
   (``Transition``, ``dataset.py:57-65``).

So the policy is a *recorder*: ``generate()`` drives, :class:`PPOPolicy` logs
``(state, action, alpha, log pi, V)`` as it is asked, and afterwards the log is joined to
``dataset.transitions`` for the rewards. PPO and CEM then face the same simulator by construction
rather than by assertion, and determinism comes free from ``generate(seed=...)``.

**One step is one (agent, auction), not one round.** ``Transition`` is stored per agent per
auction because the reward is measured once per auction: *"Per-round transitions would make the
credit assignment finer than the reward"* (``dataset.py:62-65``). The policy is called once per
round, so the join keeps the **last** call per auction — the decision the auction actually
recorded as ``final``. This is the same granularity CEM's fitness and the TD corpus use, which is
what keeps the three arms commensurable.

**The gradient is hand-derived, so §8's finite-difference check is not optional.** A score
function that is subtly wrong still trains, still produces a curve, and still yields a number
somebody would report.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

import numpy as np

from allocation.config import Config
from allocation.contracts import AgentKind, QAction
from allocation.reward.terms import discount_gamma, maximum_reward
from allocation.rl.encoder import ACTION_INDEX, ACTIONS, SIZE, StateEncoder
from allocation.rl.ppo_policy import (
    PARAM_COUNT,
    MixedPPOPolicy,
    PPOWeights,
    StepLog,
    action_logprobs,
    beta_logpdf,
    beta_params,
    sigmoid,
    values,
)
from allocation.sim.dataset import Transition, generate
from allocation.sim.fabricated import DEFAULT, FabricationRegister

EPS = 1e-6


# ---------------------------------------------------------------------------------------
# A collected batch
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rollout:
    """One on-policy batch, everything the update needs and nothing it does not."""

    states: np.ndarray        # (N, SIZE)
    masks: np.ndarray         # (N, 6) bool
    actions: np.ndarray       # (N,) int index into ACTIONS
    alphas: np.ndarray        # (N,) in (0,1); meaningless where has_alpha is False
    has_alpha: np.ndarray     # (N,) bool
    logp_old: np.ndarray      # (N,) log pi_behaviour, recorded at sampling time
    advantages: np.ndarray    # (N,) GAE, already normalised per batch (§3)
    returns: np.ndarray       # (N,) advantage + V(s), the value target
    #: Per-episode discounted return in *reward units*, for the training-curve report.
    episode_returns: tuple[float, ...] = ()
    episodes: int = 0
    dropped_episodes: int = 0
    auctions: int = 0
    abandonments: int = 0
    action_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return int(self.states.shape[0])

    def slice(self, index: np.ndarray) -> "Rollout":
        return Rollout(
            states=self.states[index], masks=self.masks[index], actions=self.actions[index],
            alphas=self.alphas[index], has_alpha=self.has_alpha[index],
            logp_old=self.logp_old[index], advantages=self.advantages[index],
            returns=self.returns[index],
        )


#: Which of the flat parameters belong to the value head. ``PPOWeights.flat`` lays the vector out
#: as ``logit_rows | logit_biases | a_row | a_bias | b_row | b_bias | v_row | v_bias``, so the
#: critic is the trailing ``SIZE + 1``. This exists so the KL early stop can halt the *policy*
#: without halting the *critic* — see the note in :func:`train_ppo`.
VALUE_MASK = np.zeros(PARAM_COUNT, dtype=bool)
VALUE_MASK[PARAM_COUNT - (SIZE + 1):] = True


def critic_fit(predicted: np.ndarray, actual: np.ndarray) -> tuple[float, float]:
    """Explained variance and correlation of a value prediction against realised returns.

    Both are reported because they disagree in an informative way. EV punishes a critic that is
    correctly *ordered* but wrongly *scaled*; correlation does not. When ``corr**2`` sits well
    above EV — run 2 measured 0.352 against 0.217 — the critic points the right way and is too
    flat, which is a fitting deficit rather than a representation one.
    """
    residual = actual - predicted
    ev = 1.0 - float(np.var(residual) / (np.var(actual) + 1e-12))
    if predicted.std() < 1e-9 or actual.std() < 1e-9:
        return ev, float("nan")
    return ev, float(np.corrcoef(predicted, actual)[0, 1])


def _final_per_auction(log: Sequence[StepLog], agent: AgentKind) -> dict[str, StepLog]:
    """The last decision the policy made in each auction — the one the auction recorded."""
    out: dict[str, StepLog] = {}
    for entry in log:
        if entry.agent is agent:
            prior = out.get(entry.auction_id)
            if prior is None or entry.round_index >= prior.round_index:
                out[entry.auction_id] = entry
    return out


def collect(
    config: Config,
    weights: PPOWeights,
    agent: AgentKind,
    seeds: Sequence[int],
    shifts: int,
    fab: FabricationRegister,
    rng: random.Random,
    target_steps: int,
    lam: float = 0.95,
    encoder: StateEncoder | None = None,
    mask_unplanned: bool = True,
) -> Rollout:
    """Roll out until ``target_steps`` env-steps are gathered, then compute GAE.

    Seeds are cycled. With 8 training seeds at 4 shifts each, one sweep yields far fewer than a
    2048-step batch, so the same worlds recur inside one batch — see the note in
    PPO_EXPERIMENT_PLAN §1. That is a variance property to report, not a correctness bug: every
    step in the batch was still generated by the current policy.
    """
    encoder = encoder or StateEncoder()
    gamma = discount_gamma(config)
    scale = max(1.0, maximum_reward(config))

    states: list[tuple[float, ...]] = []
    masks: list[tuple[bool, ...]] = []
    actions: list[int] = []
    alphas: list[float] = []
    has_alpha: list[bool] = []
    logp_old: list[float] = []
    advantages: list[float] = []
    returns: list[float] = []
    episode_returns: list[float] = []
    action_counts: dict[str, int] = {}
    episodes = dropped = auctions = abandonments = 0
    mismatched = 0

    cursor = 0
    while len(states) < target_steps:
        seed = seeds[cursor % len(seeds)]
        cursor += 1
        # The policy's own RNG is derived from (world seed, sweep index) so a rollout is
        # reproducible: §1's determinism criterion covers the sampling, not just the world.
        policy = MixedPPOPolicy(
            config, weights, agent, encoder,
            rng=random.Random(rng.randrange(2**31)),
            deterministic=False, mask_unplanned=mask_unplanned,
        )
        dataset = generate(config, seed=seed, shifts=shifts, policy=policy, fab=fab,
                           encoder=encoder)
        auctions += dataset.auctions
        abandonments += dataset.abandonments

        logged = _final_per_auction(policy.log, agent)
        usable = {(e.agent, e.shift_id) for e in dataset.complete_episodes}

        chains: dict[str, list[Transition]] = {}
        for transition in dataset.transitions:
            if transition.agent is not agent:
                continue
            if (transition.agent, transition.shift_id) not in usable:
                continue
            chains.setdefault(transition.shift_id, []).append(transition)

        all_shifts = {t.shift_id for t in dataset.transitions if t.agent is agent}
        dropped += len(all_shifts) - len(chains)

        for shift_id, chain in chains.items():
            rows: list[tuple[StepLog, Transition]] = []
            for transition in chain:
                entry = logged.get(transition.auction_id)
                if entry is None:
                    continue
                if entry.action_index != ACTION_INDEX[transition.q_action]:
                    mismatched += 1
                    continue
                rows.append((entry, transition))
            if not rows:
                continue

            episodes += 1
            episode_returns.append(
                sum(gamma**t * r.reward for t, (_, r) in enumerate(rows))
            )

            # GAE, backwards along the chain. V(s_T+1) = 0 at the shift roll: bootstrapping
            # past it would credit a decision with a return earned in a shift whose budget had
            # already been reset (`dataset.py:96-99`).
            vals = [entry.value for entry, _ in rows]
            gae = 0.0
            chain_adv = [0.0] * len(rows)
            for t in reversed(range(len(rows))):
                next_value = vals[t + 1] if t + 1 < len(rows) else 0.0
                delta = rows[t][1].reward / scale + gamma * next_value - vals[t]
                gae = delta + gamma * lam * gae
                chain_adv[t] = gae

            for t, (entry, transition) in enumerate(rows):
                states.append(entry.state)
                masks.append(entry.mask)
                actions.append(entry.action_index)
                alphas.append(entry.alpha if entry.alpha is not None else 0.5)
                has_alpha.append(entry.alpha is not None)
                logp_old.append(entry.logp)
                advantages.append(chain_adv[t])
                returns.append(chain_adv[t] + vals[t])
                key = transition.q_action.value
                action_counts[key] = action_counts.get(key, 0) + 1

    if mismatched:
        raise AssertionError(
            f"{mismatched} logged decisions disagreed with the recorded Transition action. The "
            "join between the policy log and the dataset is wrong, and every importance ratio "
            "built from it would be a ratio between two different decisions."
        )

    advantage = np.asarray(advantages, dtype=float)
    # §3: normalised **per batch**, here, once. The value target keeps its own scale — it is a
    # regression onto returns, and standardising it would fit V to a moving target.
    if advantage.size > 1:
        advantage = (advantage - advantage.mean()) / (advantage.std() + 1e-8)

    return Rollout(
        states=np.asarray(states, dtype=float),
        masks=np.asarray(masks, dtype=bool),
        actions=np.asarray(actions, dtype=int),
        alphas=np.asarray(alphas, dtype=float),
        has_alpha=np.asarray(has_alpha, dtype=bool),
        logp_old=np.asarray(logp_old, dtype=float),
        advantages=advantage,
        returns=np.asarray(returns, dtype=float),
        episode_returns=tuple(episode_returns),
        episodes=episodes, dropped_episodes=dropped, auctions=auctions,
        abandonments=abandonments, action_counts=action_counts,
    )


# ---------------------------------------------------------------------------------------
# log pi, and its gradient
# ---------------------------------------------------------------------------------------


def log_prob(weights: PPOWeights, batch: Rollout) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """``log pi_theta(a, alpha | s)`` for every row, plus the intermediates the gradient needs.

    The categorical term always applies. The Beta term applies only where the chosen action
    carries an alpha: an exit has no aggression, and including a density for a number the
    auction never used would make the ratio depend on noise.
    """
    logp_all, probs = action_logprobs(weights, batch.states, batch.masks)
    rows = np.arange(batch.n)
    logp = logp_all[rows, batch.actions].copy()

    a, b, z_a, z_b = beta_params(weights, batch.states)
    x = np.clip(batch.alphas, EPS, 1.0 - EPS)
    beta_term = beta_logpdf(x, a, b)
    logp = logp + np.where(batch.has_alpha, beta_term, 0.0)

    return logp, {"logp_all": logp_all, "probs": probs, "a": a, "b": b,
                  "z_a": z_a, "z_b": z_b, "x": x}


@dataclass(frozen=True, slots=True)
class LossParts:
    total: float
    policy: float
    value: float
    entropy: float
    approx_kl: float
    clip_fraction: float


def loss_and_grad(
    weights: PPOWeights,
    batch: Rollout,
    clip: float = 0.2,
    value_coef: float = 0.5,
    entropy_coef: float = 0.01,
    grad: bool = True,
) -> tuple[LossParts, np.ndarray | None]:
    """The clipped surrogate, the value loss and the entropy bonus, with analytic gradients.

    ``L = -mean(min(r*A, clip(r)*A)) + c_v * 0.5 * mean((V - target)^2) - c_ent * mean(H)``

    **The entropy bonus is over the categorical head only.** The exploration that matters here is
    *which* action, and alpha already explores through the Beta's own variance. Adding a Beta
    entropy term would pull in trigamma derivatives for a bonus worth a fraction of the
    categorical one — more surface area for a hand-derived gradient to be wrong on than the term
    is worth. Stated rather than silently omitted, because §8's check validates whatever
    objective is written here, including a needlessly narrow one.
    """
    from scipy.special import digamma

    n = batch.n
    logp, parts = log_prob(weights, batch)
    logp_all, probs = parts["logp_all"], parts["probs"]
    a, b, z_a, z_b = parts["a"], parts["b"], parts["z_a"], parts["z_b"]
    x = parts["x"]

    # Already normalised, once per rollout, in `collect` — §3 says "per batch", and doing it
    # here instead would renormalise inside every minibatch and make the loss depend on how the
    # shuffle happened to split the data.
    adv = batch.advantages

    ratio = np.exp(np.clip(logp - batch.logp_old, -20.0, 20.0))
    t1 = ratio * adv
    t2 = np.clip(ratio, 1.0 - clip, 1.0 + clip) * adv
    unclipped = t1 <= t2                       # which branch min() selected
    policy_loss = -float(np.mean(np.minimum(t1, t2)))

    v = values(weights, batch.states)
    residual = v - batch.returns
    value_loss = 0.5 * float(np.mean(residual**2))

    safe_logp = np.where(probs > 0.0, logp_all, 0.0)
    row_entropy = -np.sum(np.where(probs > 0.0, probs * safe_logp, 0.0), axis=1)
    entropy = float(np.mean(row_entropy))

    total = policy_loss + value_coef * value_loss - entropy_coef * entropy
    report = LossParts(
        total=total, policy=policy_loss, value=value_loss, entropy=entropy,
        approx_kl=float(np.mean(batch.logp_old - logp)),
        clip_fraction=float(np.mean(~unclipped)),
    )
    if not grad:
        return report, None

    # -- d(policy loss) / d(log pi) ----------------------------------------------------
    # Only the selected branch carries gradient. When min() took the clipped term and the ratio
    # is outside the band, clip() is flat and the gradient is exactly zero — that flat region is
    # the whole point of PPO's trust region.
    d_logp = np.where(unclipped, -adv * ratio / n, 0.0)

    # -- categorical head --------------------------------------------------------------
    # d log pi(a) / d logit_j = 1{j == a} - p_j, over the feasible set only.
    onehot = np.zeros_like(probs)
    onehot[np.arange(n), batch.actions] = 1.0
    d_logits = (onehot - probs) * batch.masks * d_logp[:, None]

    # Entropy contributes -c_ent * mean(H); dH_i/d logit_j = -p_j (log p_j + H_i).
    d_entropy_logits = -probs * (safe_logp + row_entropy[:, None]) * batch.masks
    d_logits += (-entropy_coef / n) * d_entropy_logits

    g_logit_rows = d_logits.T @ batch.states
    g_logit_biases = d_logits.sum(axis=0)

    # -- Beta heads --------------------------------------------------------------------
    # d log Beta(x; a, b) / da = ln x + psi(a+b) - psi(a);  a = 1 + softplus(z), da/dz = sig(z).
    live = batch.has_alpha
    d_a = np.where(live, np.log(x) + digamma(a + b) - digamma(a), 0.0) * sigmoid(z_a) * d_logp
    d_b = np.where(live, np.log1p(-x) + digamma(a + b) - digamma(b), 0.0) * sigmoid(z_b) * d_logp
    g_a_row = batch.states.T @ d_a
    g_b_row = batch.states.T @ d_b

    # -- value head --------------------------------------------------------------------
    d_v = value_coef * residual / n
    g_v_row = batch.states.T @ d_v

    return report, np.concatenate([
        g_logit_rows.ravel(), g_logit_biases,
        g_a_row, [float(d_a.sum())],
        g_b_row, [float(d_b.sum())],
        g_v_row, [float(d_v.sum())],
    ])


# ---------------------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Iteration:
    """One rollout plus the updates it paid for."""

    index: int
    env_steps: int
    cumulative_steps: int
    mean_episode_return: float
    loss: float
    policy_loss: float
    value_loss: float
    entropy: float
    approx_kl: float
    clip_fraction: float
    grad_norm: float
    #: Epochs in which the POLICY was updated, and epochs in which the CRITIC was. They differ
    #: whenever the KL stop fires: before the fix below they were the same number by
    #: construction, and run 2 spent its first 22 iterations giving the critic 2-3 of its
    #: intended 5 passes because a policy trust-region condition was ending the value
    #: regression too.
    epochs_run_policy: int
    epochs_run_value: int
    abandonments: int
    dropped_episodes: int
    action_mix: Mapping[str, float]
    #: Critic quality on THIS rollout, measured against the returns the rollout realised.
    #: ``ev`` uses the value function that generated the batch — the honest on-policy number,
    #: and the one comparable to the 0.212 held-out baseline. ``ev_fitted`` is the same
    #: measurement after this iteration's value updates, so it is in-sample and optimistic; the
    #: pair together says whether the regression is moving at all.
    ev: float = 0.0
    ev_fitted: float = 0.0
    corr_v_return: float = 0.0
    sd_v: float = 0.0
    sd_return: float = 0.0
    #: The world seeds this rollout was drawn from. Constant across iterations on the fixed-world
    #: arm and fresh every iteration under ``world_pool``; recorded so a log can be audited for
    #: which arm actually ran rather than trusting the label on it.
    world_seeds: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class PPORun:
    """A completed fit."""

    weights: PPOWeights
    iterations: tuple[Iteration, ...]
    agent: AgentKind
    seeds: tuple[int, ...]
    ppo_seed: int
    encoder_version: str
    fabrication_version: str
    experiment: str
    #: ``(cumulative_env_steps, held_out_return)`` from the deterministic probe. §4 requires this
    #: curve, and :meth:`rising` requires it to mean anything — see the note there.
    curve: tuple[tuple[int, float], ...] = ()
    #: The best validation score seen, and where. Run 1 hit its best at iteration 10 and then
    #: spent 93 % of the budget getting worse; without this the winning parameters are lost.
    best_value: float | None = None
    best_iteration: int = 0
    best_steps: int = 0

    @property
    def cumulative_steps(self) -> int:
        return self.iterations[-1].cumulative_steps if self.iterations else 0

    def _slope(self, y: Sequence[float], tail: int) -> float:
        """OLS slope over the last ``tail`` points, in units of the whole curve's spread.

        Normalised by the spread so a flat line with noise does not read as a trend, and so the
        same threshold works whether returns are in the tens or the hundreds.
        """
        if len(y) < max(3, tail):
            return 0.0
        window = list(y[-tail:])
        x = list(range(len(window)))
        mx, my = statistics.fmean(x), statistics.fmean(window)
        denominator = sum((xi - mx) ** 2 for xi in x)
        if denominator == 0:
            return 0.0
        slope = sum((xi - mx) * (yi - my) for xi, yi in zip(x, window)) / denominator
        spread = statistics.pstdev(list(y)) or 1.0
        return slope / spread

    def overfitting(self, tail: int = 4) -> bool:
        """Training return climbing while the held-out probe falls.

        A distinct finding from "flattened below the gate", because the remedy is distinct: more
        world diversity, not more budget. Requires the held-out curve — without it this cannot be
        detected at all, which is why :attr:`curve` exists.
        """
        if len(self.curve) < max(3, tail):
            return False
        held_out = self._slope([value for _, value in self.curve], tail)
        train = self._slope([it.mean_episode_return for it in self.iterations], tail * 2)
        return held_out < -0.05 and train > 0.0

    def rising(self, tail: int = 4) -> bool:
        """Is the **held-out** return still climbing when the budget runs out?

        §4's pre-commitment turns on this: a still-rising curve at budget exhaustion is reported
        INCONCLUSIVE — budget-limited, never as a rejection of PPO.

        **Measured on the held-out probe, not on the training return.** The original version of
        this method read ``mean_episode_return``, which is the training curve under sampling on
        the 8 training seeds. That is the wrong signal for the question §4 asks: the clause is
        about whether *more budget* would help, and under overfitting the training curve rises
        while more budget actively hurts. Seed 0 of run 1 is exactly that case — training return
        246 -> 509 while the held-out probe fell 717 -> 487 — so the original logic would have
        reported INCONCLUSIVE for a run that must be rejected.

        Falls back to the training curve only when no probe was collected, and
        :meth:`report` says so rather than passing one off as the other.
        """
        if self.curve:
            return self._slope([value for _, value in self.curve], tail) > 0.05
        if len(self.iterations) < max(4, tail):
            return True
        y = [it.mean_episode_return for it in self.iterations[-tail:]]
        x = list(range(len(y)))
        mx, my = statistics.fmean(x), statistics.fmean(y)
        denominator = sum((xi - mx) ** 2 for xi in x)
        if denominator == 0:
            return False
        slope = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / denominator
        spread = statistics.pstdev([it.mean_episode_return for it in self.iterations]) or 1.0
        return slope > 0.05 * spread

    def report(self) -> str:
        lines = [
            f"experiment            {self.experiment}",
            f"agent under training  {self.agent.value}  (others stay on the heuristic)",
            f"seeds                 {list(self.seeds)}   ppo_seed {self.ppo_seed}",
            f"encoder_version       {self.encoder_version}",
            f"fabrication_version   {self.fabrication_version}",
            f"parameters            {PARAM_COUNT}",
            "",
            f"  {'it':>3}  {'steps':>8}  {'ep_return':>10}  {'loss':>9}  {'pol':>8}  "
            f"{'val':>8}  {'ent':>6}  {'kl':>7}  {'clip':>6}  {'|g|':>8}  {'ep_p':>4}  "
            f"{'ep_v':>4}  {'EV':>6}  {'EVfit':>6}  {'corr':>6}  {'sdV':>6}  {'sdR':>6}  "
            f"{'ab':>3}",
        ]
        for it in self.iterations:
            lines.append(
                f"  {it.index:>3}  {it.cumulative_steps:>8}  {it.mean_episode_return:>10.2f}  "
                f"{it.loss:>9.4f}  {it.policy_loss:>8.4f}  {it.value_loss:>8.4f}  "
                f"{it.entropy:>6.3f}  {it.approx_kl:>7.4f}  {it.clip_fraction:>6.1%}  "
                f"{it.grad_norm:>8.4f}  {it.epochs_run_policy:>4}  {it.epochs_run_value:>4}  "
                f"{it.ev:>+6.3f}  {it.ev_fitted:>+6.3f}  {it.corr_v_return:>+6.3f}  "
                f"{it.sd_v:>6.3f}  {it.sd_return:>6.3f}  {it.abandonments:>3}"
            )
        if self.iterations:
            last = self.iterations[-1]
            source = "held-out probe" if self.curve else "TRAINING return (no probe collected)"
            lines += [
                "",
                f"final mean episode return  {last.mean_episode_return:.2f}  "
                f"after {last.cumulative_steps} env-steps  (training seeds, sampled)",
            ]
            if self.curve:
                lines.append(
                    f"held-out probe             "
                    + "  ".join(f"{value:.1f}@{steps // 1000}k" for steps, value in self.curve)
                )
            shape = "STILL RISING" if self.rising() else (
                "DECLINING — OVERFITTING" if self.overfitting() else "FLATTENED")
            lines.append(f"curve ({source})   {shape}")
            if self.best_value is not None:
                decay = last.mean_episode_return
                lines.append(
                    f"best on validation         {self.best_value:.2f} at iteration "
                    f"{self.best_iteration} ({self.best_steps} env-steps, "
                    f"{self.best_steps / max(1, last.cumulative_steps):.0%} of the budget)"
                )
                if self.curve and self.best_value > self.curve[-1][1]:
                    lines.append(
                        f"  the remaining {100 - self.best_steps / max(1, last.cumulative_steps) * 100:.0f}% "
                        f"of the budget moved validation from {self.best_value:.1f} to "
                        f"{self.curve[-1][1]:.1f}. The BEST checkpoint is the one that gets gated; "
                        "the final one is reported so the decay stays on the record."
                    )
            if self.overfitting():
                lines.append(
                    "  the training return climbed while the held-out probe fell. More budget "
                    "makes this worse, not better: the remedy is world diversity, not steps."
                )
            elif self.rising():
                lines.append(
                    "  the held-out curve had not flattened when the budget ran out. Per §4 this "
                    "run is INCONCLUSIVE — budget-limited, and is not evidence against PPO."
                )
        return "\n".join(lines)


def compare_ppo(
    config: Config,
    weights: PPOWeights,
    agent: AgentKind = AgentKind.ER,
    seeds: Sequence[int] = (101, 102, 103),
    shifts: int = 6,
    fab: FabricationRegister = DEFAULT,
    mask_unplanned: bool = True,
):
    """The frozen PPO model against the heuristic, through the *same* ``compare`` machinery.

    Not a parallel evaluation path: ``evaluate.measure`` already takes a policy object, so the PPO
    arm is scored by the identical function that produced every CEM number in ``artifacts/``. Any
    second implementation of the metric would be a second definition of the thing being compared.

    Deterministic, per §0: sampling at evaluation time would make the reported number depend on a
    draw, and two runs of the same frozen model would disagree.
    """
    from allocation.rl.evaluate import Comparison, measure

    encoder = StateEncoder()
    baseline = measure(config, "heuristic", seeds, shifts, fab, agent, None, encoder)
    learned = measure(
        config, "rl-ppo", seeds, shifts, fab, agent,
        MixedPPOPolicy(config, weights, agent, encoder, deterministic=True,
                       mask_unplanned=mask_unplanned),
        encoder,
    )
    return Comparison(baseline=baseline, learned=learned, seeds=tuple(seeds), agent=agent)


def train_ppo(
    config: Config,
    agent: AgentKind = AgentKind.ER,
    seeds: Sequence[int] = (11, 12, 13, 14, 15, 16, 17, 18),
    shifts: int = 4,
    rollout_steps: int = 2048,
    total_steps: int = 296_000,
    minibatch: int = 256,
    epochs: int = 5,
    learning_rate: float = 3e-4,
    clip: float = 0.2,
    value_coef: float = 0.5,
    entropy_coef: float = 0.01,
    lam: float = 0.95,
    max_grad_norm: float = 0.5,
    target_kl: float = 0.02,
    ppo_seed: int = 0,
    fab: FabricationRegister = DEFAULT,
    mask_unplanned: bool = True,
    checkpoint: str | None = None,
    on_iteration: Callable[[Iteration, PPOWeights], None] | None = None,
    probe: Callable[[PPOWeights], float] | None = None,
    probe_every: int = 5,
    probe_dense_until: int = 40,
    probe_dense_every: int = 2,
    best_checkpoint: str | None = None,
    kl_stop_freezes_value: bool = False,
    world_pool: tuple[int, int] | None = None,
) -> PPORun:
    """Run 1. Every default here is §4's frozen value; nothing is tuned inside this function.

    Adam is used rather than plain SGD, and that is worth being explicit about: §4 froze the
    learning rate at ``3e-4``, which is an Adam-scaled figure — the same number under raw SGD on
    a 207-parameter linear model would barely move the weights inside the budget. The optimiser
    was not named in §4; this is the reading of it that makes the frozen number mean what it
    conventionally means.

    ``kl_stop_freezes_value=True`` restores run 1 and run 2's behaviour, where the KL stop ended
    the value regression as well as the policy update. It is kept only so the fix can be
    measured against the thing it replaced on the same budget and the same seeds; nothing should
    run it otherwise.

    ``world_pool=(lo, hi)`` draws a **fresh** set of world seeds from ``range(lo, hi)`` for every
    rollout instead of replaying ``seeds``. It draws ``len(seeds)`` of them, without replacement,
    so the batch still contains the same number of distinct worlds and the same ~5x within-batch
    replay — the *only* thing that changes is whether the same worlds recur across iterations.
    That is deliberate: varying the worlds-per-batch at the same time would confound world
    diversity with batch diversity, and the ablation could not attribute its own result. Left
    ``None``, the seed handling is byte-for-byte the existing path, including its RNG draws.
    """
    encoder = StateEncoder()
    weights = PPOWeights.zeros(encoder.version, fab.version)
    rng = random.Random(ppo_seed)

    # Adam state.
    m = np.zeros(PARAM_COUNT)
    v = np.zeros(PARAM_COUNT)
    beta1, beta2, adam_eps = 0.9, 0.999, 1e-8
    adam_step = 0

    iterations: list[Iteration] = []
    curve: list[tuple[int, float]] = []
    best = {"value": None, "iteration": 0, "steps": 0}
    cumulative = 0
    index = 0

    while cumulative < total_steps:
        index += 1
        # Drawn only when a pool is configured, so condition A keeps the exact RNG stream the
        # current code has: an unconditional draw here would reseed every downstream rollout and
        # the "unchanged" arm would not reproduce.
        batch_seeds = (tuple(rng.sample(range(*world_pool), len(seeds)))
                       if world_pool is not None else tuple(seeds))
        batch = collect(
            config, weights, agent, batch_seeds, shifts, fab, rng,
            target_steps=rollout_steps, lam=lam, encoder=encoder,
            mask_unplanned=mask_unplanned,
        )
        if batch.n == 0:
            raise RuntimeError("a rollout produced no usable steps; every episode was dropped")
        cumulative += batch.n

        # The critic that produced this batch, scored against the returns the batch realised.
        # Taken BEFORE any update, so `values(weights, ...)` here is exactly the `entry.value`
        # each step was logged with: this is the on-policy critic quality, not a refit.
        v_before = values(weights, batch.states)
        ev_before, corr_before = critic_fit(v_before, batch.returns)

        order = np.arange(batch.n)
        last: LossParts | None = None
        grad_norm = 0.0
        epochs_policy = epochs_value = 0
        policy_frozen = False
        for _ in range(epochs):
            epochs_value += 1
            if not policy_frozen:
                epochs_policy += 1
            rng_np = np.random.default_rng(rng.randrange(2**31))
            rng_np.shuffle(order)
            for start in range(0, batch.n, minibatch):
                index_slice = order[start:start + minibatch]
                if index_slice.size < 2:
                    continue
                report, gradient = loss_and_grad(
                    weights, batch.slice(index_slice), clip, value_coef, entropy_coef,
                )
                last = report
                assert gradient is not None
                if policy_frozen:
                    gradient = np.where(VALUE_MASK, gradient, 0.0)
                norm = float(np.linalg.norm(gradient))
                if not policy_frozen:
                    # Reported only from policy-live minibatches: a value-only gradient has 23
                    # live entries instead of 207 and its norm is not the same quantity, so
                    # mixing them would make the column incomparable across iterations.
                    grad_norm = norm
                if norm > max_grad_norm:
                    gradient = gradient * (max_grad_norm / (norm + 1e-12))

                adam_step += 1
                m_next = beta1 * m + (1 - beta1) * gradient
                v_next = beta2 * v + (1 - beta2) * gradient**2
                m_hat = m_next / (1 - beta1**adam_step)
                v_hat = v_next / (1 - beta2**adam_step)
                step = learning_rate * m_hat / (np.sqrt(v_hat) + adam_eps)
                if policy_frozen:
                    # Zeroing the gradient is NOT enough to freeze a parameter under Adam: the
                    # first moment still carries the momentum it came in with, so `m_hat` stays
                    # non-zero and the policy would keep drifting for the rest of the iteration
                    # on updates the trust region has already refused. The moments are held too,
                    # so a value-only epoch leaves the policy and its optimiser state exactly as
                    # the KL check found them.
                    m = np.where(VALUE_MASK, m_next, m)
                    v = np.where(VALUE_MASK, v_next, v)
                    step = np.where(VALUE_MASK, step, 0.0)
                else:
                    m, v = m_next, v_next
                weights.set_flat(weights.flat() - step)

            if policy_frozen:
                # The policy has not moved since the freeze, so its KL against `logp_old` cannot
                # have changed. Re-measuring it would cost a full-batch pass to learn nothing.
                continue

            # Approx-KL early stop, measured on the whole batch after the epoch: the on-policy
            # assumption behind the clipped ratio is what fails first, and abandoning the rest
            # of the *policy* updates is cheaper than pretending it still holds.
            #
            # **It stops the policy, not the critic.** This used to `break`, which ended the
            # epoch loop outright — and the value regression with it. That coupled a policy
            # trust-region condition to an unrelated supervised fit: run 2's critic got 2-3 of
            # its 5 passes through the first 22 iterations, exactly while the returns it was
            # chasing climbed 60 -> 520, and settled at EV 0.212 against the 0.477 the same 22
            # features support. The trust region itself is untouched — the policy stops at the
            # same epoch, on the same threshold, under the same clip. Only the critic keeps
            # going, and the critic has no trust region to violate: it is a regression onto
            # fixed targets computed before any of these updates.
            check, _ = loss_and_grad(weights, batch, clip, value_coef, entropy_coef, grad=False)
            if check.approx_kl > target_kl:
                policy_frozen = True
                if kl_stop_freezes_value:
                    break

        ev_fitted, _ = critic_fit(values(weights, batch.states), batch.returns)
        total_actions = sum(batch.action_counts.values()) or 1
        iteration = Iteration(
            index=index, env_steps=batch.n, cumulative_steps=cumulative,
            mean_episode_return=statistics.fmean(batch.episode_returns)
            if batch.episode_returns else 0.0,
            loss=last.total if last else 0.0,
            policy_loss=last.policy if last else 0.0,
            value_loss=last.value if last else 0.0,
            entropy=last.entropy if last else 0.0,
            approx_kl=last.approx_kl if last else 0.0,
            clip_fraction=last.clip_fraction if last else 0.0,
            grad_norm=grad_norm,
            epochs_run_policy=epochs_policy, epochs_run_value=epochs_value,
            abandonments=batch.abandonments, dropped_episodes=batch.dropped_episodes,
            action_mix={k: n / total_actions for k, n in sorted(batch.action_counts.items())},
            ev=ev_before, ev_fitted=ev_fitted, corr_v_return=corr_before,
            sd_v=float(v_before.std()), sd_return=float(batch.returns.std()),
            world_seeds=batch_seeds,
        )
        iterations.append(iteration)
        # The held-out probe is part of the RUN, not of the logging around it: §4's verdict
        # depends on this curve, so a run that forgot to collect it cannot be adjudicated.
        # Denser early, because run 1's peak was at iteration 10 and a stride of 10 straddled it.
        stride = probe_dense_every if index <= probe_dense_until else probe_every
        if probe is not None and stride and index % stride == 0:
            value = float(probe(weights))
            curve.append((cumulative, value))
            # Keep-best. Without this the winning parameters exist only as a number in a log —
            # which is exactly what run 1 lost at iteration 10.
            if best["value"] is None or value > best["value"]:
                best.update(value=value, iteration=index, steps=cumulative)
                if best_checkpoint:
                    weights.save(best_checkpoint)
        if on_iteration:
            # The live weights, not a copy: the caller's probe must score the parameters
            # this iteration actually produced.
            on_iteration(iteration, weights)
        if checkpoint:
            weights.save(checkpoint)

    return PPORun(
        weights=weights, iterations=tuple(iterations), agent=agent, seeds=tuple(seeds),
        ppo_seed=ppo_seed, encoder_version=encoder.version, fabrication_version=fab.version,
        experiment="A (withdraw_unplanned masked)" if mask_unplanned else "B (all six actions)",
        curve=tuple(curve),
        best_value=best["value"], best_iteration=best["iteration"], best_steps=best["steps"],
    )
