"""PPO_EXPERIMENT_PLAN §8 — the tests that must pass before any PPO number is reported.

Four of these guard things that would otherwise fail *silently*, which is why the plan makes them
blocking rather than nice-to-have:

* **The gradient check.** A hand-derived score function that is wrong still trains, still draws a
  plausible curve, and still produces a number somebody would put in a table. Finite differences
  against the exact objective is the only thing that catches it.
* **The masked density.** PPO's ratio is between two densities over the same support. If the mask
  is applied after normalisation the ratio is between two things that are not probabilities, and
  nothing downstream complains.
* **The log-prob recomputation.** The update replays ``log pi`` from a stored state; if that
  replay disagrees with what was recorded at sampling time, every ratio is wrong by a constant
  nobody can see.
* **The version refusals.** A policy loaded against a different encoder runs and emits plausible
  alphas about a different world.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import pytest

np = pytest.importorskip("numpy", reason="PPO needs the [rl] extra")
pytest.importorskip("scipy", reason="PPO needs the [rl] extra")

from allocation.contracts import AgentKind, QAction  # noqa: E402
from allocation.rl.encoder import ACTION_INDEX, ACTIONS, SIZE, StateEncoder  # noqa: E402
from allocation.rl.ppo import Rollout, collect, log_prob, loss_and_grad  # noqa: E402
from allocation.rl.ppo_policy import (  # noqa: E402
    PARAM_COUNT,
    PPOPolicy,
    PPOWeights,
    action_logprobs,
    beta_logpdf,
    beta_params,
)
from allocation.sim.fabricated import DEFAULT  # noqa: E402


# ---------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------


def _weights(seed: int = 0) -> PPOWeights:
    """Non-trivial parameters. A gradient check at the zero init proves almost nothing: the
    softmax is uniform, both Beta shapes are equal, and several terms vanish together."""
    rng = np.random.default_rng(seed)
    out = PPOWeights.zeros(StateEncoder().version, DEFAULT.version)
    out.set_flat(rng.normal(0.0, 0.4, PARAM_COUNT))
    return out


def _batch(seed: int = 1, n: int = 24) -> Rollout:
    """A synthetic on-policy batch, with mixed masks and both action kinds represented."""
    rng = np.random.default_rng(seed)
    states = rng.uniform(0.0, 1.0, (n, SIZE))

    masks = rng.random((n, len(ACTIONS))) < 0.6
    for i in range(n):
        if masks[i].sum() < 2:                       # every row needs a real choice
            masks[i, : 2] = True
    actions = np.array([int(rng.choice(np.flatnonzero(masks[i]))) for i in range(n)])
    has_alpha = np.array([not ACTIONS[a].exits for a in actions])
    alphas = np.where(has_alpha, rng.uniform(0.05, 0.95, n), 0.5)

    batch = Rollout(
        states=states, masks=masks, actions=actions, alphas=alphas, has_alpha=has_alpha,
        logp_old=np.zeros(n), advantages=rng.normal(0.0, 1.0, n),
        returns=rng.normal(0.0, 1.0, n),
    )
    # On-policy means logp_old IS log pi at the parameters that produced the sample. Setting it
    # from a *different* theta would make the ratio start away from 1 and hide clip bugs.
    logp, _ = log_prob(_weights(seed=0), batch)
    return Rollout(
        states=states, masks=masks, actions=actions, alphas=alphas, has_alpha=has_alpha,
        logp_old=logp, advantages=batch.advantages, returns=batch.returns,
    )


# ---------------------------------------------------------------------------------------
# 1 · masked probabilities
# ---------------------------------------------------------------------------------------


def test_masked_probabilities_sum_to_one_over_the_feasible_set() -> None:
    batch = _batch()
    _, probs = action_logprobs(_weights(), batch.states, batch.masks)

    assert np.allclose(probs.sum(axis=1), 1.0), "the density is not normalised over the mask"


def test_an_infeasible_action_has_exactly_zero_probability() -> None:
    """Not 'small' — exactly zero, and ``-inf`` log-probability.

    A masked action with a tiny positive probability is one the sampler can still return, and it
    would return a :class:`Decision` for an exit whose plan cannot be named."""
    batch = _batch()
    logp_all, probs = action_logprobs(_weights(), batch.states, batch.masks)

    assert np.all(probs[~batch.masks] == 0.0)
    assert np.all(np.isneginf(logp_all[~batch.masks]))


def test_the_sampler_never_returns_a_masked_action() -> None:
    weights = _weights()
    probs = np.array([0.0, 0.55, 0.0, 0.45, 0.0, 0.0])
    policy = PPOPolicy.__new__(PPOPolicy)          # only _sample_index is under test
    policy._rng = random.Random(7)
    drawn = {policy._sample_index(probs) for _ in range(400)}

    assert drawn <= {1, 3}, f"sampled a zero-probability action: {drawn - {1, 3}}"


# ---------------------------------------------------------------------------------------
# 2 · log_prob against a brute-force recomputation
# ---------------------------------------------------------------------------------------


def test_log_prob_matches_a_brute_force_recomputation() -> None:
    """Recomputed with plain Python floats and ``math``, sharing no code with the vectorised path."""
    weights = _weights()
    batch = _batch()
    logp, _ = log_prob(weights, batch)

    for i in range(batch.n):
        state = batch.states[i]
        logits = [
            float(np.dot(weights.logit_rows[j], state) + weights.logit_biases[j])
            for j in range(len(ACTIONS))
        ]
        live = [j for j in range(len(ACTIONS)) if batch.masks[i, j]]
        top = max(logits[j] for j in live)
        total = sum(math.exp(logits[j] - top) for j in live)
        expected = logits[int(batch.actions[i])] - top - math.log(total)

        if batch.has_alpha[i]:
            z_a = float(np.dot(weights.a_row, state) + weights.a_bias)
            z_b = float(np.dot(weights.b_row, state) + weights.b_bias)
            a = 1.0 + math.log1p(math.exp(-abs(z_a))) + max(z_a, 0.0)
            b = 1.0 + math.log1p(math.exp(-abs(z_b))) + max(z_b, 0.0)
            x = float(batch.alphas[i])
            expected += (
                (a - 1.0) * math.log(x) + (b - 1.0) * math.log1p(-x)
                + math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
            )

        assert logp[i] == pytest.approx(expected, abs=1e-9), f"row {i}"


def test_beta_density_integrates_to_one() -> None:
    """A sanity check on the density itself, independent of the gradient."""
    weights = _weights()
    state = np.full((1, SIZE), 0.5)
    a, b, _, _ = beta_params(weights, state)
    grid = np.linspace(1e-4, 1 - 1e-4, 20001)
    mass = np.trapezoid(np.exp(beta_logpdf(grid, a[0], b[0])), grid)

    assert mass == pytest.approx(1.0, abs=2e-3)


# ---------------------------------------------------------------------------------------
# 3 · THE BLOCKING TEST — analytic gradient vs finite differences
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("clip,value_coef,entropy_coef", [
    (0.2, 0.5, 0.01),        # §4's frozen values
    (0.2, 0.0, 0.0),         # the surrogate alone, so a value/entropy bug cannot mask a policy one
    (0.05, 0.5, 0.05),       # a clip tight enough that most rows are in the flat region
])
def test_analytic_gradient_matches_finite_differences(clip, value_coef, entropy_coef) -> None:
    """Central differences on all 207 parameters, against the exact objective.

    Rows sitting on the clip boundary have a genuinely discontinuous derivative, so a handful of
    coordinates can disagree; the assertion is on the overall agreement (cosine and relative
    norm) plus a bound on how many coordinates may be off. A sign error, a missing mask, a wrong
    chain-rule factor or a dropped head all fail this comfortably.
    """
    weights = _weights(seed=3)
    batch = _batch(seed=5, n=32)
    base = weights.flat()

    def objective(theta: np.ndarray) -> float:
        probe = weights.copy()
        probe.set_flat(theta)
        report, _ = loss_and_grad(probe, batch, clip, value_coef, entropy_coef, grad=False)
        return report.total

    _, analytic = loss_and_grad(weights, batch, clip, value_coef, entropy_coef)
    assert analytic is not None and analytic.size == PARAM_COUNT

    step = 1e-6
    numeric = np.zeros(PARAM_COUNT)
    for i in range(PARAM_COUNT):
        up, down = base.copy(), base.copy()
        up[i] += step
        down[i] -= step
        numeric[i] = (objective(up) - objective(down)) / (2 * step)

    scale = max(np.linalg.norm(numeric), 1e-12)
    relative = np.linalg.norm(analytic - numeric) / scale
    cosine = float(analytic @ numeric) / (np.linalg.norm(analytic) * scale)
    off = int(np.sum(np.abs(analytic - numeric) > 1e-4 + 1e-2 * np.abs(numeric)))

    assert cosine > 0.9999, f"direction disagrees: cosine {cosine:.6f}"
    assert relative < 1e-3, f"magnitude disagrees: relative error {relative:.3e}"
    assert off <= 2, f"{off} coordinates disagree beyond tolerance"


def test_gradient_is_zero_where_the_clip_is_flat() -> None:
    """Outside the trust region the surrogate is constant, so the policy heads get nothing.

    Built by driving the ratio far past ``1 + clip`` with a positive advantage — the case where
    PPO is supposed to stop pushing. The value head still learns, so only the policy block is
    asserted zero."""
    weights = _weights(seed=11)
    batch = _batch(seed=13, n=16)
    stale = Rollout(
        states=batch.states, masks=batch.masks, actions=batch.actions, alphas=batch.alphas,
        has_alpha=batch.has_alpha,
        logp_old=batch.logp_old - 12.0,             # ratio = e^12, far above 1 + clip
        advantages=np.abs(batch.advantages) + 1.0,  # strictly positive
        returns=batch.returns,
    )
    report, gradient = loss_and_grad(weights, stale, clip=0.2, value_coef=0.5, entropy_coef=0.0)

    assert report.clip_fraction == 1.0
    policy_block = gradient[: len(ACTIONS) * (SIZE + 1) + 2 * (SIZE + 1)]
    assert np.allclose(policy_block, 0.0), "gradient leaked through the flat clip region"


# ---------------------------------------------------------------------------------------
# 4 · determinism
# ---------------------------------------------------------------------------------------


def test_same_seed_produces_an_identical_rollout(config) -> None:
    weights = _weights(seed=2)
    kwargs = dict(
        config=config, weights=weights, agent=AgentKind.ER, seeds=(11,), shifts=2,
        fab=DEFAULT, target_steps=12,
    )
    first = collect(rng=random.Random(99), **kwargs)
    second = collect(rng=random.Random(99), **kwargs)

    assert first.n == second.n and first.n > 0
    for name in ("states", "masks", "actions", "alphas", "logp_old", "advantages", "returns"):
        assert np.array_equal(getattr(first, name), getattr(second, name)), name


def test_a_different_seed_produces_a_different_rollout(config) -> None:
    """The mirror of the above: if the sampling RNG were ignored, both tests would pass."""
    weights = _weights(seed=2)
    kwargs = dict(
        config=config, weights=weights, agent=AgentKind.ER, seeds=(11,), shifts=2,
        fab=DEFAULT, target_steps=12,
    )
    first = collect(rng=random.Random(99), **kwargs)
    other = collect(rng=random.Random(1234), **kwargs)

    assert not (first.n == other.n
                and np.array_equal(first.actions, other.actions)
                and np.array_equal(first.alphas, other.alphas))


# ---------------------------------------------------------------------------------------
# 5 · the mask never strips every action
# ---------------------------------------------------------------------------------------


def test_experiment_a_mask_never_empties_the_support(config) -> None:
    """``WITHDRAW_UNPLANNED`` is the only always-feasible action (``policy.py:288-291``), so a
    naive subtraction leaves an empty support in exactly the state where the patient really is
    abandoned. The mask has to put it back."""
    policy = PPOPolicy(config, _weights(), mask_unplanned=True)

    assert policy.learned_feasible(frozenset({QAction.WITHDRAW_UNPLANNED})) == frozenset(
        {QAction.WITHDRAW_UNPLANNED}
    )
    reduced = policy.learned_feasible(
        frozenset({QAction.WITHDRAW_UNPLANNED, QAction.CONTINUE, QAction.WIN_NOW})
    )
    assert QAction.WITHDRAW_UNPLANNED not in reduced
    assert reduced == frozenset({QAction.CONTINUE, QAction.WIN_NOW})


def test_experiment_b_masks_nothing(config) -> None:
    policy = PPOPolicy(config, _weights(), mask_unplanned=False)
    full = frozenset(ACTIONS)

    assert policy.learned_feasible(full) == full


def test_experiment_a_records_no_abandonment(config) -> None:
    """The end-to-end version of the mask test: over real rollouts the learned agent must never
    take ``withdraw_unplanned``. ``aband == 0`` is a hard serving criterion (§5)."""
    batch = collect(
        config, _weights(seed=4), AgentKind.ER, seeds=(11, 12), shifts=2, fab=DEFAULT,
        rng=random.Random(5), target_steps=60, mask_unplanned=True,
    )

    assert batch.n > 0
    assert batch.action_counts.get(QAction.WITHDRAW_UNPLANNED.value, 0) == 0


# ---------------------------------------------------------------------------------------
# 6 · artifact round-trip and the refusals
# ---------------------------------------------------------------------------------------


def test_save_load_round_trip(tmp_path: Path) -> None:
    weights = _weights(seed=8)
    path = weights.save(tmp_path / "ppo.json")
    back = PPOWeights.load(path)

    assert np.allclose(back.flat(), weights.flat())
    assert back.encoder_version == weights.encoder_version
    assert back.fabrication_version == DEFAULT.version
    assert back.policy_version == "rl-ppo-linear-v1"


def test_load_refuses_a_bumped_encoder_version(tmp_path: Path) -> None:
    path = _weights().save(tmp_path / "ppo.json")
    body = json.loads(path.read_text(encoding="utf-8"))
    body["encoder_version"] = "deadbeefcafe"
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(ValueError, match="encoder"):
        PPOWeights.load(path)


def test_load_refuses_a_reordered_action_list(tmp_path: Path) -> None:
    path = _weights().save(tmp_path / "ppo.json")
    body = json.loads(path.read_text(encoding="utf-8"))
    body["actions"] = list(reversed(body["actions"]))
    path.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(ValueError, match="ordering"):
        PPOWeights.load(path)


def test_parameter_count_is_the_plan_s_207() -> None:
    """§2's arithmetic, asserted rather than trusted."""
    assert PARAM_COUNT == len(ACTIONS) * (SIZE + 1) + 2 * (SIZE + 1) + (SIZE + 1)
    assert PARAM_COUNT == 207
    assert _weights().flat().size == 207


# ---------------------------------------------------------------------------------------
# 7 · the placeholder QWeights slot must never be consulted
# ---------------------------------------------------------------------------------------


def test_the_inherited_q_head_is_unreachable(config) -> None:
    """``PPOPolicy`` subclasses ``LinearQPolicy`` to reuse ``_encode``/``_feasible``/``_plan``. The
    inherited zeros ``QWeights`` is a placeholder; if any path ever reads it the policy would
    silently bid on zeros, so all three accessors raise."""
    policy = PPOPolicy(config, _weights())

    for call in (lambda: policy._q([0.0] * SIZE),
                 lambda: policy._alpha([0.0] * SIZE),
                 lambda: policy.weights):
        with pytest.raises(AssertionError):
            call()


# ---------------------------------------------------------------------------------------
# 10 · the KL stop halts the policy, not the critic
# ---------------------------------------------------------------------------------------


def test_kl_stop_freezes_the_policy_and_only_the_policy(config) -> None:
    """The value head keeps training past the KL stop; the policy does not move at all.

    Run with a negative ``target_kl`` so the stop fires after the first epoch every time — the
    naive ``mean(logp_old - logp)`` estimator can come out slightly negative, so ``0.0`` is not
    reliably below it. The budget is small enough for exactly one iteration
    — beyond that the extra value epochs consume RNG draws and the two runs face different
    rollouts, which would make the comparison meaningless.

    Both halves matter. That the critic keeps moving is the fix. That the policy does **not** is
    the thing most likely to be silently wrong: zeroing a gradient does not freeze a parameter
    under Adam, because the first moment still carries momentum from before the stop. If that
    were missed, the policy would keep drifting on updates the trust region had already refused
    and the run would look like a critic fix while quietly being a weaker trust region.
    """
    from allocation.rl.ppo import train_ppo

    shared = dict(
        config=config, agent=AgentKind.ER, seeds=(11,), shifts=2, rollout_steps=24,
        total_steps=1, minibatch=8, epochs=3, target_kl=-1.0, ppo_seed=0, fab=DEFAULT,
    )
    legacy = train_ppo(**shared, kl_stop_freezes_value=True)
    fixed = train_ppo(**shared, kl_stop_freezes_value=False)

    assert len(legacy.iterations) == len(fixed.iterations) == 1
    assert legacy.iterations[0].epochs_run_policy == legacy.iterations[0].epochs_run_value == 1
    assert fixed.iterations[0].epochs_run_policy == 1
    assert fixed.iterations[0].epochs_run_value == 3

    policy_slice = slice(0, PARAM_COUNT - (SIZE + 1))
    value_slice = slice(PARAM_COUNT - (SIZE + 1), PARAM_COUNT)
    before, after = legacy.weights.flat(), fixed.weights.flat()

    assert np.array_equal(before[policy_slice], after[policy_slice]), (
        "the two extra value-only epochs moved a POLICY parameter. The trust region has been "
        "weakened, not preserved."
    )
    assert not np.allclose(before[value_slice], after[value_slice]), (
        "the two extra value-only epochs left the critic unchanged, so the fix does nothing."
    )


def test_world_pool_draws_fresh_seeds_and_leaves_the_fixed_arm_alone(config) -> None:
    """The ablation's two arms must differ in exactly one thing: which worlds recur.

    Both halves are load-bearing. If arm B silently reused seeds the ablation would measure
    nothing; if arm A's RNG stream shifted when the option was added, the "unchanged" control
    would not be the thing it is being compared against.
    """
    from allocation.rl.ppo import train_ppo

    shared = dict(
        config=config, agent=AgentKind.ER, seeds=(11, 12), shifts=2, rollout_steps=24,
        total_steps=60, minibatch=8, epochs=1, ppo_seed=0, fab=DEFAULT,
    )
    fixed = train_ppo(**shared)
    diverse = train_ppo(**shared, world_pool=(10_000, 100_000))

    assert len(fixed.iterations) >= 2
    assert all(it.world_seeds == (11, 12) for it in fixed.iterations)

    drawn = [it.world_seeds for it in diverse.iterations]
    assert all(len(set(s)) == 2 for s in drawn), "a rollout drew the same world twice"
    assert all(all(10_000 <= x < 100_000 for x in s) for s in drawn), "drew outside the pool"
    assert len(set(drawn)) == len(drawn), "two rollouts drew the identical seed set"
    assert not set(sum(drawn, ())) & {11, 12}, "the diverse arm reached into the fixed seeds"
