"""Temporal-difference learning. Does it actually learn?

The tests that matter here are not "does it run" but **"does the value function end up knowing
something it did not know before"**, and they are written to fail on the specific ways TD with
linear function approximation quietly does not work:

* it diverges (the deadly triad) — caught by :func:`test_td_error_does_not_diverge`
* it memorises the buffer — caught by holding out shifts
* it never explores, so five of six actions keep their initial values — caught by
  :func:`test_exploration_visits_every_feasible_action`
* it bootstraps off actions that could never be taken — caught by
  :func:`test_the_bootstrap_only_maxes_over_feasible_actions`
* it learns nothing because every action scores alike — caught by
  :func:`test_learning_separates_the_actions`

An earlier version of ``QLearner`` failed the first of these: TD error climbed 64 -> 128 over
200 updates. The fix was reward scaling plus a Double-Q target, and
:func:`test_td_error_does_not_diverge` is the regression for it.
"""

from __future__ import annotations

import random
import statistics

import pytest

from allocation.config import load_config
from allocation.contracts import AgentKind, QAction
from allocation.reward.terms import maximum_reward
from allocation.rl.encoder import ACTIONS, SIZE, StateEncoder
from allocation.rl.policy import PARAM_COUNT, QWeights
from allocation.rl.qlearn import (
    EpsilonGreedy,
    QLearner,
    ReplayBuffer,
    fit_offline,
)
from allocation.sim.dataset import Transition, generate


@pytest.fixture(scope="module")
def config():
    return load_config()


@pytest.fixture(scope="module")
def transitions(config):
    """A dataset big enough that a fit means something."""
    rows: list[Transition] = []
    for seed in range(12):
        rows.extend(generate(config, seed=900 + seed, shifts=6).transitions)
    return [t for t in rows if t.complete]


def _learner(config, **kwargs) -> QLearner:
    return QLearner(
        weights=QWeights.zeros(StateEncoder().version),
        gamma=float(config.reward["discount_gamma"]),
        reward_scale=max(1.0, maximum_reward(config)),
        **kwargs,
    )


# ---------------------------------------------------------------------------------------
# The MDP itself
# ---------------------------------------------------------------------------------------


def test_transitions_are_chained_into_trajectories(transitions):
    """Without ``next_state`` there is no bootstrapping, only regression onto returns."""
    bootstrappable = [t for t in transitions if not t.terminal]
    assert bootstrappable, "some transitions must have a successor to bootstrap from"
    assert all(t.next_state is not None for t in bootstrappable)
    assert all(len(t.next_state) == SIZE for t in bootstrappable)


def test_the_chain_never_crosses_a_shift_boundary(config):
    """Budgets reset at the roll, so a bootstrap across it credits a decision with a return
    earned out of an allowance it never spent."""
    dataset = generate(config, seed=901, shifts=4)
    by_key = {}
    for t in dataset.transitions:
        by_key.setdefault((t.agent, t.shift_id), []).append(t)

    for rows in by_key.values():
        assert rows[-1].terminal, "the last auction of a shift must be terminal"
        for row in rows[:-1]:
            assert not row.terminal


def test_the_last_transition_of_each_shift_is_terminal(transitions):
    assert any(t.terminal for t in transitions)
    assert any(not t.terminal for t in transitions)


# ---------------------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------------------


def test_an_update_moves_the_weights(config, transitions):
    learner = _learner(config)
    before = learner.weights.flat()
    learner.update(transitions[:64])
    assert learner.weights.flat() != before


def test_only_the_taken_actions_row_moves(config):
    """Semi-gradient TD updates the row for the action taken, and no other.

    Updating every row would make the six Q-functions collapse toward each other, which is
    indistinguishable from "the actions do not matter" and is how a value-based learner ends up
    choosing arbitrarily.
    """
    learner = _learner(config)
    state = tuple(0.5 for _ in range(SIZE))
    row = Transition(
        auction_id="a", shift_id="s", agent=AgentKind.ER, candidate_id="c",
        state=state, q_action=QAction.WIN_NOW, alpha=0.5, won=True, bid=10.0,
        utility=100.0, ceiling=120.0, cost=5.0, reward=150.0, complete=True,
        feasible=("win_now", "continue"), budget_remaining=100.0, burn_rate=0.3,
        terminal=True,
    )
    learner.update([row])

    touched = ACTIONS.index(QAction.WIN_NOW)
    for index, weights in enumerate(learner.weights.rows):
        if index == touched:
            assert any(abs(w) > 1e-12 for w in weights)
        else:
            assert all(abs(w) < 1e-12 for w in weights)


def test_td_error_does_not_diverge(config, transitions):
    """Regression for a real bug: TD error once climbed 64 -> 128 over 200 updates.

    Cause was the raw reward scale (±200) against gamma 0.99 plus max-operator bias, so the
    bootstrap target and the predictions raced upward together. Fixed by dividing rewards by
    the configured maximum and decoupling action selection from valuation (Double-Q).
    """
    learner = _learner(config)
    rng = random.Random(0)
    errors = []
    for i in range(300):
        if i % 25 == 0:
            learner.sync_target()
        errors.append(learner.update(rng.sample(transitions, min(64, len(transitions)))))

    early = statistics.fmean(errors[:25])
    late = statistics.fmean(errors[-25:])
    assert late < early * 3.0, (
        f"TD error grew from {early:.3f} to {late:.3f} — the learner is diverging"
    )
    assert all(e == e for e in errors), "NaN in the TD error"
    assert max(errors) < 1e3


def test_reward_scaling_is_what_prevents_divergence(config, transitions):
    """Named directly, so the fix cannot be removed as an unexplained constant."""
    unscaled = QLearner(
        weights=QWeights.zeros(StateEncoder().version),
        gamma=0.99, reward_scale=1.0, double_q=False, huber_delta=10.0,
    )
    scaled = _learner(config)

    rng_a, rng_b = random.Random(1), random.Random(1)
    unscaled_errors, scaled_errors = [], []
    for i in range(200):
        if i % 25 == 0:
            unscaled.sync_target()
            scaled.sync_target()
        unscaled_errors.append(unscaled.update(rng_a.sample(transitions, 64)))
        scaled_errors.append(scaled.update(rng_b.sample(transitions, 64)))

    unscaled_growth = statistics.fmean(unscaled_errors[-20:]) / max(
        1e-9, statistics.fmean(unscaled_errors[:20])
    )
    scaled_growth = statistics.fmean(scaled_errors[-20:]) / max(
        1e-9, statistics.fmean(scaled_errors[:20])
    )
    assert scaled_growth < unscaled_growth


def test_holdout_td_error_falls(config, transitions):
    """The claim that matters: the value function generalises rather than memorising.

    Split by shift, not by row — transitions in a shift are chained, so a row split would leak
    a transition's own successor into the holdout.

    Asserted **relative** to the target magnitude. This test previously compared raw TD error
    against its own starting value and failed a working learner: a zero-initialised Q predicts
    0 against targets that begin at ~0.33 and bootstrap up to ~3.7, so absolute error rises
    with the scale it is denominated in, and the only way to satisfy the old assertion was to
    keep Q near zero. The three checks below pin the actual property.
    """
    fit = fit_offline(config, transitions, epochs=250, seed=3)
    assert fit.n_train > 0 and fit.n_holdout > 0
    assert fit.converged, fit.report()

    relative = fit.relative_curve
    # The value function must fill in rather than sit at its initialisation — this is the
    # check that would fail if a future change satisfied `converged` by keeping Q near zero.
    assert fit.holdout_scale[-1] > 2 * fit.holdout_scale[0], fit.report()
    # And the residual must shrink materially as a fraction of what is being predicted.
    # Thresholds sized for this fixture at 250 epochs (76.9% -> 46.0%); the longer run on
    # artifacts/sample.jsonl reaches ~13%.
    assert relative[-1] < 0.75 * relative[0], fit.report()
    assert relative[-1] < 0.60, fit.report()


def test_learning_separates_the_actions(config, transitions):
    """A value function that scores all six alike has learned nothing usable.

    This is the failure that a tidy TD curve hides: error can fall to a residual while every
    action keeps the same value, in which case the argmax is whichever sorts first.
    """
    fit = fit_offline(config, transitions, epochs=250, seed=3)
    learner = QLearner(weights=fit.weights, gamma=0.99)
    means = [
        statistics.fmean(learner.q(t.state, a) for t in transitions[:200]) for a in ACTIONS
    ]
    assert max(means) - min(means) > 0.01, f"Q-values are flat across actions: {means}"


def test_the_bootstrap_only_maxes_over_feasible_actions(config):
    """Otherwise the target is set by an exit whose plan could not be named, and the learner
    chases a return no policy could ever collect."""
    learner = _learner(config)
    learner.weights = QWeights.from_flat(
        [0.0] * PARAM_COUNT, StateEncoder().version
    )
    rows = [list(r) for r in learner.weights.rows]
    rows[ACTIONS.index(QAction.WITHDRAW_ALTERNATIVE)] = [5.0] * SIZE  # wildly attractive
    from dataclasses import replace

    learner.weights = replace(learner.weights, rows=tuple(tuple(r) for r in rows))
    learner.sync_target()

    state = tuple(0.5 for _ in range(SIZE))
    with_alt = learner.max_q(state, ("win_now", "withdraw_alternative"))
    without_alt = learner.max_q(state, ("win_now", "continue"))
    assert with_alt > without_alt


def test_incomplete_transitions_never_enter_the_buffer(config, transitions):
    """Imputing a missing reward as 0 would teach the policy an unobserved death went fine."""
    from dataclasses import replace

    buffer = ReplayBuffer()
    added = buffer.add([replace(t, complete=False) for t in transitions[:20]])
    assert added == 0
    assert len(buffer) == 0


# ---------------------------------------------------------------------------------------
# Exploration
# ---------------------------------------------------------------------------------------


def test_exploration_visits_every_feasible_action(config):
    """Without this, five of six actions keep their initial values forever — the failure that
    looks exactly like convergence."""
    from allocation.policy.heuristic import HeuristicPolicy

    explorer = EpsilonGreedy(HeuristicPolicy(config), epsilon=0.9, rng=random.Random(0))
    dataset = generate(config, seed=910, shifts=6, policy=explorer)
    seen = {t.q_action for t in dataset.transitions}
    assert len(seen) >= 3, f"exploration only reached {[a.value for a in seen]}"
    assert explorer.explored > 0


def test_exploration_stays_inside_the_feasible_set(config):
    """An exploratory exit whose plan cannot be named would fail to construct."""
    from allocation.policy.heuristic import HeuristicPolicy

    explorer = EpsilonGreedy(HeuristicPolicy(config), epsilon=1.0, rng=random.Random(1))
    dataset = generate(config, seed=911, shifts=4, policy=explorer)
    assert dataset.transitions
    for transition in dataset.transitions:
        assert transition.q_action.value in transition.feasible


def test_zero_epsilon_never_explores(config):
    from allocation.policy.heuristic import HeuristicPolicy

    explorer = EpsilonGreedy(HeuristicPolicy(config), epsilon=0.0, rng=random.Random(2))
    generate(config, seed=912, shifts=3, policy=explorer)
    assert explorer.explored == 0


# ---------------------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------------------


def test_a_dataset_round_trips_through_jsonl(config, tmp_path):
    from allocation.rl.qlearn import load_transitions

    dataset = generate(config, seed=920, shifts=3)
    path = dataset.write_jsonl(tmp_path / "t.jsonl")
    rows, header = load_transitions(path)

    assert header["encoder_version"] == dataset.encoder_version
    assert header["fabrication_version"] == dataset.fabrication_version
    assert len(rows) == len(dataset.transitions)
    assert [r.state for r in rows] == [t.state for t in dataset.transitions]
    assert [r.terminal for r in rows] == [t.terminal for t in dataset.transitions]
    assert [r.next_state for r in rows] == [t.next_state for t in dataset.transitions]
