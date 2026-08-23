"""Temporal-difference Q-learning. The part that is actually reinforcement learning.

``train.py``'s cross-entropy method is policy *search*: it samples whole parameter vectors,
keeps the ones that scored well, and refits a Gaussian. It never opens a transition. That makes
it a black-box optimiser — legitimate, and a useful baseline, but it learns nothing *from
experience*, and every state vector the encoder builds is wasted on it.

This module learns. The update is the Bellman one::

    target      =  r                                    if the shift ended here
                =  r + gamma * max_a' Q(s', a')         otherwise, over FEASIBLE a' only

    delta       =  target - Q(s, a)
    w_a        +=  lr * delta * s                       semi-gradient, only the taken action
    b_a        +=  lr * delta

Four properties that make this real rather than decorative, each of which took a specific piece
of machinery elsewhere in the codebase:

**It bootstraps across a shift.** ``Transition.next_state`` exists because the objective in
RL-Steps §21 is the discounted return over eight hours, not over one auction. Without s' this
would be supervised regression onto observed returns, which cannot express *"an agent that bids
hard at 09:00 and has nothing left at 17:00 made a bad decision at 09:00"* — the value of the
17:00 state has to propagate backwards, one bootstrap at a time.

**The max is over feasible actions only.** ``max_a' Q(s', a')`` over all six would let the target
be set by an exit whose plan could not be named, so the learner would chase a return no policy
could ever collect. ``Transition.next_feasible`` is on the row for exactly this.

**It explores.** :class:`EpsilonGreedy` wraps the policy during collection. Without exploration
a greedy policy visits only the states its current weights favour, and the Q-values everywhere
else stay at their initial values forever — the classic failure that looks like convergence.

**It alternates collection and fitting.** Each round collects fresh transitions *with the
current policy* and then fits. That is what makes it on-policy improvement rather than one
offline regression: the data distribution moves as the policy does, which is both the point and
the reason the target network below exists.

**Why a target network with linear features.** Bootstrapping off a Q that is itself being
updated is the standard divergence risk (the "deadly triad" — function approximation, off-policy
targets, bootstrapping). A frozen copy of the weights used for ``max_a' Q(s', a')``, refreshed
between rounds, is the cheap and standard mitigation, and with linear features it is enough.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence

from allocation.config import Config
from allocation.contracts import (
    Action,
    AgentKind,
    BiddingPolicy,
    BudgetState,
    Candidate,
    Decision,
    FeatureSnapshot,
    PathwayOptions,
    QAction,
    RoundState,
    UtilityBreakdown,
)
from allocation.pathway.plans import build_plan
from allocation.reward.terms import discount_gamma
from allocation.rl.encoder import ACTION_INDEX, ACTIONS, SIZE, StateEncoder
from allocation.rl.policy import PARAM_COUNT, LinearQPolicy, QWeights
from allocation.sim.dataset import Transition, generate
from allocation.sim.fabricated import DEFAULT, FabricationRegister


# ---------------------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------------------


@dataclass
class ReplayBuffer:
    """Transitions to learn from, oldest evicted first.

    A buffer rather than learning from each transition as it arrives, for the usual reason and
    one specific one. The usual: consecutive auctions in a shift are strongly correlated —
    same patients, same occupancy, budget draining monotonically — and fitting a linear model on
    correlated samples in order makes it track the most recent shift rather than the problem.

    The specific one: there are only about six auctions per department per shift, so a single
    pass over one collection round is a handful of updates. Replaying the buffer several times
    per round is what makes the sample budget usable at all.
    """

    capacity: int = 20_000
    items: list[Transition] = field(default_factory=list)

    def add(self, transitions: Sequence[Transition], agent: AgentKind | None = None) -> int:
        """Add transitions, keeping only complete ones.

        **Incomplete transitions are dropped, not zero-filled.** ``reward/episode.py``: an
        episode with an unscored auction is unusable, and imputing its reward as 0 would teach
        the policy that an unobserved death went fine. The same rule has to hold one level down
        or the buffer quietly re-introduces what the episode filter removed.
        """
        fresh = [
            t for t in transitions
            if t.complete and (agent is None or t.agent is agent)
        ]
        self.items.extend(fresh)
        if len(self.items) > self.capacity:
            self.items = self.items[-self.capacity :]
        return len(fresh)

    def sample(self, rng: random.Random, size: int) -> list[Transition]:
        if not self.items:
            return []
        if len(self.items) <= size:
            return list(self.items)
        return rng.sample(self.items, size)

    def __len__(self) -> int:
        return len(self.items)


# ---------------------------------------------------------------------------------------
# The learner
# ---------------------------------------------------------------------------------------


@dataclass
class QLearner:
    """Linear Q with semi-gradient TD updates and a frozen target copy."""

    weights: QWeights
    gamma: float = 0.99
    learning_rate: float = 0.02
    #: Huber cut on the TD error, in *scaled* reward units. One anomalous reward — a -60
    #: deterioration on a shift where everything else scored +50 — otherwise moves every weight
    #: in the row by a large multiple of a normal step, and with 22 correlated features that is
    #: where linear TD diverges.
    huber_delta: float = 1.0
    #: Rewards are divided by this before any update. **Not cosmetic — the first version of this
    #: class diverged without it**, TD error climbing 64 -> 128 over 200 updates instead of
    #: falling.
    #:
    #: Two reasons. Numerically: raw rewards run to ±200 and, bootstrapped at gamma 0.99 over
    #: six auctions, imply weights around 55 per feature; starting from zero with features in
    #: [0, 1], the max operator inflates the target faster than the predictions can follow, and
    #: the two race upward together. Structurally: the reward's absolute scale is *unsigned
    #: config* — ``reward.yaml``'s point values have never been fitted and will change. Dividing
    #: by the configured maximum makes the learner invariant to that, so re-signing the reward
    #: terms does not silently require re-tuning the learning rate.
    reward_scale: float = 200.0
    #: Double-Q target: pick the next action with the ONLINE weights, value it with the frozen
    #: target. Plain ``max_a' Q_target`` takes a max over noisy estimates, which is biased
    #: upward by construction, and that bias is precisely what compounds through the bootstrap.
    #: Decoupling selection from valuation is the standard, and cheap, correction.
    double_q: bool = True
    #: Semi-gradient steps taken per action, keyed by ``QAction.value``. **An action missing here
    #: has a weight row that is still all zeros**, so ``Q(s, a)`` is exactly 0 for every state —
    #: the absence of an opinion, not a learned indifference. Tracked because the two are
    #: indistinguishable in the fitted values: a never-updated row and a row that genuinely
    #: learned "worth nothing" both print 0.0000, and the first is dangerous at deployment
    #: because a greedy argmax picks it whenever every learned action scores negative.
    updates: Counter = field(default_factory=Counter)
    _target: QWeights | None = None

    def __post_init__(self) -> None:
        self._target = self.weights

    def sync_target(self) -> None:
        """Freeze the current weights as the bootstrap target. Called between rounds."""
        self._target = self.weights

    # -- Q ----------------------------------------------------------------------------

    def q(self, state: Sequence[float], action: QAction, target: bool = False) -> float:
        weights = (self._target if target else self.weights) or self.weights
        index = ACTION_INDEX[action]
        return sum(w * s for w, s in zip(weights.rows[index], state)) + weights.biases[index]

    def max_q(
        self, state: Sequence[float], feasible: Sequence[str], target: bool = True
    ) -> float:
        """The bootstrap value of ``state``, over feasible actions only.

        Feasible-only because ``max`` over all six would let the target be set by an exit whose
        plan could not be named — the learner would chase a return no policy could collect.
        Falls back to the full set when the row carries no feasibility list; returning 0.0
        instead would be a silent claim that the next state is worthless.

        With ``double_q``, the *online* weights choose the action and the *frozen* weights
        value it. A single max over one noisy estimator is biased upward, and under
        bootstrapping that bias compounds — it is the mechanism behind the divergence this
        class originally showed.
        """
        allowed = [a for a in ACTIONS if a.value in feasible] or list(ACTIONS)
        if not self.double_q or not target:
            return max(self.q(state, a, target=target) for a in allowed)
        choice = max(allowed, key=lambda a: self.q(state, a, target=False))
        return self.q(state, choice, target=True)

    # -- the update -------------------------------------------------------------------

    def update(self, batch: Sequence[Transition]) -> float:
        """One semi-gradient pass. Returns the mean absolute TD error.

        Mean absolute TD error is the number to watch. It should fall and then flatten; if it
        rises steadily the learning rate is too high or the target is being synced too often,
        which is the linear-TD divergence this class's target copy exists to prevent.
        """
        if not batch:
            return 0.0

        rows = [list(r) for r in self.weights.rows]
        biases = list(self.weights.biases)
        errors: list[float] = []

        for transition in batch:
            state = transition.state
            index = ACTION_INDEX[transition.q_action]

            target = transition.reward / self.reward_scale
            if not transition.terminal and transition.next_state is not None:
                target += self.gamma * self.max_q(
                    transition.next_state, transition.next_feasible, target=True
                )

            predicted = sum(w * s for w, s in zip(rows[index], state)) + biases[index]
            delta = target - predicted
            errors.append(abs(delta))

            # Huber: linear beyond the cut, so one outlier moves the weights by a bounded step.
            step = self.learning_rate * max(-self.huber_delta, min(self.huber_delta, delta))

            for i, feature in enumerate(state):
                rows[index][i] += step * feature
            biases[index] += step
            self.updates[transition.q_action.value] += 1

        self.weights = replace(
            self.weights, rows=tuple(tuple(r) for r in rows), biases=tuple(biases)
        )
        return statistics.fmean(errors)

    def fit_alpha(self, batch: Sequence[Transition]) -> float:
        """Fit the aggression head by advantage-weighted regression on observed alphas.

        Separate from the Q update because alpha is not an action the Q-function ranks — it is
        a magnitude attached to two of the six actions. Regressing it onto *every* observed bid
        would copy the behaviour policy, so each sample is weighted by its advantage: alphas
        from decisions that beat the state's own value are pulled toward, the rest are not.

        This is the standard advantage-weighted-regression trick, and it is the honest way to
        learn a continuous head from a value function that only ranks discrete actions.
        """
        samples = [
            t for t in batch
            if t.alpha is not None and not t.q_action.exits and t.next_state is not None
        ]
        if not samples:
            return 0.0

        row = list(self.weights.alpha_row)
        bias = self.weights.alpha_bias
        errors: list[float] = []

        for transition in samples:
            value = self.max_q(transition.state, transition.feasible, target=True)
            advantage = self.q(transition.state, transition.q_action) - value
            weight = math.exp(max(-8.0, min(2.0, advantage / self.huber_delta)))

            z = sum(w * s for w, s in zip(row, transition.state)) + bias
            predicted = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            delta = (transition.alpha - predicted) * weight
            errors.append(abs(transition.alpha - predicted))

            # Logistic derivative, so the step shrinks as the head saturates.
            step = self.learning_rate * delta * predicted * (1.0 - predicted)
            for i, feature in enumerate(transition.state):
                row[i] += step * feature
            bias += step

        self.weights = replace(self.weights, alpha_row=tuple(row), alpha_bias=bias)
        return statistics.fmean(errors)


# ---------------------------------------------------------------------------------------
# Exploration
# ---------------------------------------------------------------------------------------


class EpsilonGreedy:
    """Wraps a policy, taking a random *feasible* action with probability epsilon.

    **Without this there is no learning worth the name.** A greedy policy visits only the states
    its current weights favour, so every other state keeps its initial Q-value forever. The run
    converges, reports a stable return, and has explored nothing — which is indistinguishable
    from success unless you were watching for it.

    Random over *feasible* actions, never all six: an exploratory ``WITHDRAW_ALTERNATIVE`` with
    no alternative would fail to construct. Exploration must stay inside the action space, not
    outside it.
    """

    def __init__(self, inner: BiddingPolicy, epsilon: float, rng: random.Random) -> None:
        self._inner = inner
        self._epsilon = epsilon
        self._rng = rng
        self.name = f"eps{epsilon:.2f}({getattr(inner, 'name', '?')})"
        self.explored = 0
        self.total = 0

    def decide(self, *args, **kwargs):
        decision = self.decide_q(*args, **kwargs)
        return decision.action, decision.alpha

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
        greedy = self._inner.decide_q(
            candidate, utility, ceiling, round_state, budget, snapshot, pathways
        )
        self.total += 1
        if self._rng.random() >= self._epsilon:
            return greedy

        options = sorted(greedy.feasible, key=lambda a: a.value)
        if not options:
            return greedy
        choice = self._rng.choice(options)
        if choice is greedy.q_action:
            return greedy

        self.explored += 1
        if not choice.exits:
            # A random alpha too, or exploration would only ever probe *which* action and never
            # *how hard* — and the aggression head would be fitted on one policy's behaviour.
            return Decision.compete(
                choice, Action.INCREASE_BID, self._rng.random(), feasible=greedy.feasible
            )

        # Built here, not fetched off the inner policy: the explorer wraps ANY policy, and
        # only the learned one ever had a `_plan` method. Reaching for it crashed on the
        # heuristic, which is the policy the very first collection round wraps.
        rebuilt = build_plan(choice, pathways, note="exploratory exit")
        if rebuilt is None:
            # Nothing can be arranged for this action, so it is not really available. Falling
            # back to the greedy choice is right: Decision would refuse to construct it anyway.
            return greedy
        return Decision(
            q_action=choice, action=Action.WITHDRAW, plan=rebuilt, feasible=greedy.feasible
        )


# ---------------------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Round:
    """One collect-then-fit cycle."""

    index: int
    epsilon: float
    collected: int
    buffer_size: int
    td_error: float
    alpha_error: float
    mean_return: float
    burn: float
    win_share: float
    explored: float


@dataclass(frozen=True, slots=True)
class QRun:
    """A completed TD run."""

    weights: QWeights
    rounds: tuple[Round, ...]
    agent: AgentKind
    baseline_return: float
    encoder_version: str
    fabrication_version: str
    #: Per-action coverage over everything the run collected — see :func:`coverage`. The reason
    #: this loop exists rather than ``fit_offline`` alone: exploration is what puts the other
    #: actions in the buffer, and this table is where you check that it did.
    action_coverage: tuple[CoverageRow, ...] = ()

    @property
    def untrained(self) -> tuple[str, ...]:
        return tuple(name for name, _u, _f, verdict in self.action_coverage if verdict == UNTRAINED)

    @property
    def improvement(self) -> float:
        if not self.rounds or self.baseline_return == 0:
            return 0.0
        return (self.rounds[-1].mean_return - self.baseline_return) / abs(self.baseline_return)

    def report(self) -> str:
        lines = [
            f"agent               {self.agent.value}  (others frozen on the heuristic)",
            f"encoder_version     {self.encoder_version}",
            f"fabrication_version {self.fabrication_version}",
            f"heuristic baseline  {self.baseline_return:8.2f}",
            "",
            f"  {'rnd':>3} {'eps':>5} {'new':>5} {'buf':>6} {'td_err':>8} {'a_err':>6} "
            f"{'return':>8} {'burn':>6} {'win':>5} {'explored':>8}",
        ]
        for r in self.rounds:
            lines.append(
                f"  {r.index:>3} {r.epsilon:>5.2f} {r.collected:>5} {r.buffer_size:>6} "
                f"{r.td_error:>8.2f} {r.alpha_error:>6.3f} {r.mean_return:>8.2f} "
                f"{r.burn:>6.1%} {r.win_share:>5.0%} {r.explored:>8.1%}"
            )
        final = self.rounds[-1].mean_return if self.rounds else 0.0
        lines += [
            "",
            f"final return        {final:8.2f}   ({self.improvement:+.1%} vs heuristic)",
        ]
        if self.action_coverage:
            lines += ["", coverage_report(self.action_coverage)]
        lines += [
            "",
            "  TD error should fall and flatten. Rising steadily means the learning rate is too",
            "  high or the target is synced too often — the linear-TD divergence the frozen",
            "  target copy exists to prevent.",
        ]
        return "\n".join(lines)


def train_q(
    config: Config,
    agent: AgentKind = AgentKind.ER,
    rounds: int = 12,
    shifts_per_round: int = 6,
    seeds_per_round: int = 3,
    batch_size: int = 128,
    updates_per_round: int = 40,
    learning_rate: float = 0.02,
    epsilon_start: float = 0.60,
    epsilon_end: float = 0.05,
    seed: int = 0,
    fab: FabricationRegister = DEFAULT,
    on_round=None,
) -> QRun:
    """Collect with the current policy, fit, repeat.

    Epsilon decays geometrically from ``epsilon_start`` to ``epsilon_end``. Starting high is not
    optional here: at round zero the weights are all zero, so every action ties and the greedy
    argmax is whichever one sorts first — without exploration the first several rounds would
    collect one action over and over and the buffer would contain no information about the
    other five.

    Fresh seeds every round, so the policy is never fitted to one arrival stream. The evaluation
    seeds in ``evaluate.py`` are disjoint from every seed used here.
    """
    encoder = StateEncoder()
    rng = random.Random(seed)
    gamma = discount_gamma(config)

    from allocation.reward.terms import maximum_reward

    learner = QLearner(
        weights=QWeights.zeros(encoder.version, fab.version),
        gamma=gamma,
        learning_rate=learning_rate,
        # From the reward table itself, so re-signing those (unsigned) point values does not
        # silently require re-tuning the learning rate.
        reward_scale=max(1.0, maximum_reward(config)),
    )
    buffer = ReplayBuffer()
    baseline = _baseline_return(config, agent, shifts_per_round, fab, encoder)
    history: list[Round] = []

    decay = (epsilon_end / epsilon_start) ** (1.0 / max(1, rounds - 1))

    for index in range(rounds):
        epsilon = epsilon_start * (decay ** index)

        greedy = LinearQPolicy(config, learner.weights, encoder)
        explorer = EpsilonGreedy(_Routed(greedy, config, agent), epsilon, rng)

        collected = 0
        returns: list[float] = []
        burns: list[float] = []
        wins = seen = 0

        for offset in range(seeds_per_round):
            collect_seed = 1000 + index * 97 + offset
            dataset = generate(
                config, seed=collect_seed, shifts=shifts_per_round,
                policy=explorer, fab=fab, encoder=encoder,
            )
            collected += buffer.add(dataset.transitions, agent=agent)
            returns += [
                e.discounted_return for e in dataset.complete_episodes if e.agent is agent
            ]
            for transition in dataset.transitions:
                if transition.agent is not agent:
                    continue
                seen += 1
                wins += int(transition.won)
                burns.append(transition.burn_rate)

        # Fit against a frozen target, then adopt it. Syncing every update would make the
        # target chase the weights and remove the stabilisation entirely.
        learner.sync_target()
        td_errors: list[float] = []
        alpha_errors: list[float] = []
        for _ in range(updates_per_round):
            batch = buffer.sample(rng, batch_size)
            td_errors.append(learner.update(batch))
            alpha_errors.append(learner.fit_alpha(batch))

        record = Round(
            index=index,
            epsilon=epsilon,
            collected=collected,
            buffer_size=len(buffer),
            td_error=statistics.fmean(td_errors) if td_errors else 0.0,
            alpha_error=statistics.fmean(alpha_errors) if alpha_errors else 0.0,
            mean_return=statistics.fmean(returns) if returns else 0.0,
            burn=statistics.fmean(burns) if burns else 0.0,
            win_share=wins / seen if seen else 0.0,
            explored=explorer.explored / explorer.total if explorer.total else 0.0,
        )
        history.append(record)
        if on_round:
            on_round(record)

    return QRun(
        weights=learner.weights,
        rounds=tuple(history),
        agent=agent,
        baseline_return=baseline,
        encoder_version=encoder.version,
        fabrication_version=fab.version,
        action_coverage=coverage(learner.updates, buffer.items),
    )


class _Routed:
    """The learner drives one agent; the heuristic drives the others."""

    def __init__(self, learned: LinearQPolicy, config: Config, agent: AgentKind) -> None:
        from allocation.policy.heuristic import HeuristicPolicy

        self._learned = learned
        self._baseline = HeuristicPolicy(config)
        self._agent = agent
        self.name = f"q:{agent.value}"

    def decide(self, candidate: Candidate, *args, **kwargs):
        target = self._learned if candidate.agent is self._agent else self._baseline
        return target.decide(candidate, *args, **kwargs)

    def decide_q(self, candidate: Candidate, *args, **kwargs):
        target = self._learned if candidate.agent is self._agent else self._baseline
        return target.decide_q(candidate, *args, **kwargs)

    def _plan(self, action: QAction, pathways):
        return self._learned._plan(action, pathways)


def _baseline_return(
    config: Config,
    agent: AgentKind,
    shifts: int,
    fab: FabricationRegister,
    encoder: StateEncoder,
) -> float:
    returns: list[float] = []
    for seed in (5001, 5002, 5003):
        dataset = generate(config, seed=seed, shifts=shifts, fab=fab, encoder=encoder)
        returns += [
            e.discounted_return for e in dataset.complete_episodes if e.agent is agent
        ]
    return statistics.fmean(returns) if returns else 0.0


# ---------------------------------------------------------------------------------------
# Offline training from a persisted dataset
# ---------------------------------------------------------------------------------------


def load_transitions(path, agent: AgentKind | None = None) -> tuple[list[Transition], dict]:
    """Read ``transitions.jsonl`` back, with the header that says what world it came from.

    The header is returned rather than discarded because the three versions in it are the only
    thing that makes the rows meaningful. Pooling transitions across a different
    ``encoder_version`` would train on vectors whose features mean different things, and across
    a different ``fabrication_version`` would train on two different worlds — both produce a
    policy that runs, emits plausible numbers, and is about nothing.
    """
    import json
    from pathlib import Path

    header: dict = {}
    rows: list[Transition] = []

    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            body = json.loads(line)
            if body.get("_header"):
                header = body
                continue
            kind = AgentKind(body["agent"])
            if agent is not None and kind is not agent:
                continue
            rows.append(
                Transition(
                    auction_id=body["auction_id"],
                    shift_id=body["shift_id"],
                    agent=kind,
                    candidate_id=body["candidate_id"],
                    state=tuple(body["state"]),
                    q_action=QAction(body["q_action"]),
                    alpha=body["alpha"],
                    won=body["won"],
                    bid=body["bid"],
                    utility=body["utility"],
                    ceiling=body["ceiling"],
                    cost=body["cost"],
                    reward=body["reward"],
                    complete=body["complete"],
                    feasible=tuple(body["feasible"]),
                    budget_remaining=body["budget_remaining"],
                    burn_rate=body["burn_rate"],
                    next_state=tuple(body["next_state"]) if body.get("next_state") else None,
                    next_feasible=tuple(body.get("next_feasible", ())),
                    terminal=body.get("terminal", True),
                )
            )
    return rows, header


# ---------------------------------------------------------------------------------------
# Action coverage
# ---------------------------------------------------------------------------------------


#: One row of :func:`coverage`: action, updates it received, transitions where it was
#: *available*, and the verdict.
CoverageRow = tuple[str, int, int, str]

LEARNED = "learned"
NEVER_OFFERED = "n/a — never feasible"
UNTRAINED = "UNTRAINED — Q=0 for every state"


def coverage(
    updates: Mapping[str, int], transitions: Sequence[Transition]
) -> tuple[CoverageRow, ...]:
    """Which actions the fit actually learned, and which it only appears to have.

    **This is the check a TD run needs and the spread of the fitted Q-values cannot give.**
    A weight row that was never updated scores exactly 0.0 everywhere, so it widens the spread
    between the highest and lowest mean Q rather than narrowing it: a policy that learned two of
    six actions passes a ``spread > 0`` test *more* easily than one that learned all six.

    Two kinds of zero, separated because only one is a defect:

    ``UNTRAINED``
        The action was feasible in the data and the behaviour policy never chose it. Nothing was
        learned about it, and a greedy argmax will still pick it whenever every learned action
        scores below zero — including ``withdraw_unplanned``, which abandons the patient with
        nothing arranged. Fixed by exploration (:class:`EpsilonGreedy`), not by more data: the
        deterministic heuristic makes the same choice however many seeds it is run over.

    ``NEVER_OFFERED``
        The action was not feasible anywhere, so no policy could have taken it. For ER and Ward
        that is ``await_next_resource``: its gate is ``P(release within the patient's safe
        window) >= 0.70`` (``rules/pathway.yaml``), and those units' windows are too short to
        clear it. That is the pathway model working — a deteriorating patient should not wait on
        a derived Poisson estimate — so it is reported separately and is **not** a fit failure.
    """
    available = Counter(a for t in transitions for a in t.feasible)
    rows: list[CoverageRow] = []
    for action in ACTIONS:
        name = action.value
        fitted = int(updates.get(name, 0))
        offered = int(available.get(name, 0))
        if fitted:
            verdict = LEARNED
        elif offered:
            verdict = UNTRAINED
        else:
            verdict = NEVER_OFFERED
        rows.append((name, fitted, offered, verdict))
    return tuple(rows)


def coverage_report(rows: Sequence[CoverageRow]) -> str:
    """The coverage table, plus the one-line verdict a caller should act on."""
    learned = [r for r in rows if r[3] == LEARNED]
    untrained = [r for r in rows if r[3] == UNTRAINED]
    lines = [
        f"  {'action':<22} {'updates':>9} {'feasible':>9}   status",
    ]
    for name, fitted, offered, verdict in rows:
        lines.append(f"  {name:<22} {fitted:>9} {offered:>9}   {verdict}")
    lines.append("")
    lines.append(f"  learned {len(learned)}/{len(rows)} actions")
    if untrained:
        names = ", ".join(r[0] for r in untrained)
        lines += [
            f"  INCOMPLETE: {names} — feasible in the data, never taken, so the",
            "  weight rows are still zero. Greedy argmax selects them whenever every learned",
            "  action scores negative. Collect with exploration (scripts/train_q_online.py).",
        ]
    else:
        lines.append("  every feasible action received updates.")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class OfflineFit:
    """A batch fit, scored on transitions the learner never saw."""

    weights: QWeights
    train_curve: tuple[float, ...]
    holdout_curve: tuple[float, ...]
    n_train: int
    n_holdout: int
    #: Mean ``|target|`` on the holdout at each epoch. Recorded because the TD error alone is
    #: uninterpretable without it — see :attr:`converged`.
    holdout_scale: tuple[float, ...] = ()
    #: Per-action coverage — see :func:`coverage`. Carried on the fit rather than recomputed by
    #: callers because :attr:`converged` is not sufficient on its own: a fit can converge
    #: cleanly on two of six actions, which is what the first TD run on heuristic-collected
    #: data did.
    action_coverage: tuple[CoverageRow, ...] = ()

    @property
    def untrained(self) -> tuple[str, ...]:
        """Actions that were feasible in the data and still have an all-zero weight row."""
        return tuple(name for name, _u, _f, verdict in self.action_coverage if verdict == UNTRAINED)

    @property
    def complete(self) -> bool:
        """Converged **and** with no feasible action left untrained.

        Separate from :attr:`converged` because they fail independently and mean different
        things: ``converged`` is about the value function fitting, ``complete`` is about it
        having anything at all to say for each action the policy may be asked to choose.
        """
        return self.converged and not self.untrained

    @property
    def relative_curve(self) -> tuple[float, ...]:
        """Held-out TD error as a fraction of the magnitude being predicted."""
        if not self.holdout_scale:
            return self.holdout_curve
        return tuple(
            err / scale if scale > 1e-9 else 0.0
            for err, scale in zip(self.holdout_curve, self.holdout_scale)
        )

    @property
    def converged(self) -> bool:
        """Held-out TD error, **relative to the target magnitude**, ended below where it began.

        **Relative, and the absolute version was wrong.** A zero-initialised value function
        predicts 0 for targets that at epoch 0 average only ``r / reward_scale`` — about 0.33
        here — so its absolute TD error starts *small*. As bootstrapping fills the value
        function in, the targets grow toward the true discounted return (0.33 -> 3.70 over a
        thousand epochs, with only 8.6% of transitions terminal), and the absolute error grows
        with them. Measured that way a correct learner looks divergent, and the only way to
        pass would be to keep Q near zero.

        The observed run makes the point: absolute error 0.33 -> 0.49 while relative error fell
        100% -> 13.1%, mean ``|Q|`` tracked mean ``|target|`` to within 0.3%, and the fit beat
        a predict-the-mean baseline by 71%. Nothing was wrong with the learner.

        Measured on the holdout, never on the training split. Training TD error can fall simply
        by memorising a small buffer — with 22 features per action and a few hundred samples
        that is a live risk, not a theoretical one.
        """
        curve = self.relative_curve
        if len(curve) < 4:
            return False
        head = statistics.fmean(curve[:3])
        tail = statistics.fmean(curve[-3:])
        return tail < head

    def report(self) -> str:
        relative = self.relative_curve
        lines = [
            f"train / holdout      {self.n_train} / {self.n_holdout} transitions",
            "",
            f"  {'epoch':>6} {'train TD':>10} {'holdout TD':>12} {'|target|':>10} {'rel':>8}",
        ]
        step = max(1, len(self.train_curve) // 12)
        for i in range(0, len(self.train_curve), step):
            scale = self.holdout_scale[i] if self.holdout_scale else float("nan")
            lines.append(
                f"  {i:>6} {self.train_curve[i]:>10.4f} {self.holdout_curve[i]:>12.4f} "
                f"{scale:>10.4f} {relative[i]:>8.1%}"
            )
        lines += [
            "",
            f"  holdout TD  {self.holdout_curve[0]:.4f} -> {self.holdout_curve[-1]:.4f} "
            f"(absolute, rises as the targets grow — not the criterion)",
            f"  relative    {relative[0]:.1%} -> {relative[-1]:.1%}   "
            f"{'CONVERGED' if self.converged else 'NOT CONVERGED'}",
        ]
        if self.action_coverage:
            lines += ["", coverage_report(self.action_coverage)]
        return "\n".join(lines)


def fit_offline(
    config: Config,
    transitions: Sequence[Transition],
    epochs: int = 300,
    batch_size: int = 128,
    learning_rate: float = 0.02,
    holdout_fraction: float = 0.25,
    target_sync_every: int = 25,
    seed: int = 0,
    fabrication_version: str = "",
) -> OfflineFit:
    """Fitted-Q on a persisted dataset, with a held-out split.

    Split by **shift**, not by row. Transitions inside one shift are chained by
    ``next_state``, so a random row split would put a transition in train and its own successor
    in holdout — the holdout would then be scored against states the learner had already fitted,
    and the curve would look better than it is.
    """
    from allocation.reward.terms import maximum_reward

    encoder = StateEncoder()
    rng = random.Random(seed)

    shifts = sorted({(t.agent.value, t.shift_id) for t in transitions})
    rng.shuffle(shifts)
    cut = int(len(shifts) * (1.0 - holdout_fraction))
    train_keys = set(shifts[:cut])

    train = [t for t in transitions if (t.agent.value, t.shift_id) in train_keys]
    holdout = [t for t in transitions if (t.agent.value, t.shift_id) not in train_keys]

    if not train or not holdout:
        raise ValueError(
            f"not enough shifts to split: {len(shifts)} found. Generate a larger dataset — "
            "a fit with no holdout cannot be distinguished from memorisation."
        )

    learner = QLearner(
        weights=QWeights.zeros(encoder.version, fabrication_version),
        gamma=discount_gamma(config),
        learning_rate=learning_rate,
        reward_scale=max(1.0, maximum_reward(config)),
    )

    train_curve: list[float] = []
    holdout_curve: list[float] = []
    holdout_scale: list[float] = []

    for epoch in range(epochs):
        if epoch % target_sync_every == 0:
            learner.sync_target()
        batch = rng.sample(train, min(batch_size, len(train)))
        train_curve.append(learner.update(batch))
        learner.fit_alpha(batch)
        error, scale = _td_error(learner, holdout)
        holdout_curve.append(error)
        holdout_scale.append(scale)

    return OfflineFit(
        weights=learner.weights,
        train_curve=tuple(train_curve),
        holdout_curve=tuple(holdout_curve),
        n_train=len(train),
        n_holdout=len(holdout),
        holdout_scale=tuple(holdout_scale),
        # Against the training split, not the whole set: an action the learner never saw a
        # sample of cannot have been updated by it, whatever the holdout contained.
        action_coverage=coverage(learner.updates, train),
    )


def _td_error(
    learner: QLearner, transitions: Sequence[Transition]
) -> tuple[float, float]:
    """Mean absolute TD error and mean ``|target|``, without updating anything.

    Both, because the error is meaningless alone: it is denominated in whatever the targets
    currently are, and under bootstrapping that scale moves by an order of magnitude over a
    run. Returning the denominator alongside it is what lets :attr:`OfflineFit.converged` ask
    the question it means to ask.
    """
    if not transitions:
        return 0.0, 0.0
    errors: list[float] = []
    scales: list[float] = []
    for t in transitions:
        target = t.reward / learner.reward_scale
        if not t.terminal and t.next_state is not None:
            target += learner.gamma * learner.max_q(t.next_state, t.next_feasible, target=True)
        errors.append(abs(target - learner.q(t.state, t.q_action)))
        scales.append(abs(target))
    return statistics.fmean(errors), statistics.fmean(scales)
