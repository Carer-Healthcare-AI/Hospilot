"""Cross-entropy method: sample parameters, keep the best, refit, repeat.

CEM rather than a gradient method, for reasons specific to this problem rather than taste:

**The reward arrives once per auction, hours later, for the whole allocation.** There is no
per-round signal and no differentiable path from a parameter to a return — the auction, the
guards and the settlement in between are discrete and full of clamps. A policy-gradient estimator
would be differentiating through a simulator that is not differentiable, and the usual fix
(score-function estimators) needs far more episodes than six-auctions-per-shift supplies.

**The objective is the one §21 actually states.** ``Σ γᵗ Rₜ`` over a shift, not per auction.
CEM optimises exactly that, because the fitness of a parameter vector is simply the mean
discounted return of the episodes it produced. Nothing has to be decomposed or credited.

**It is honest about noise.** Each candidate is evaluated on the *same* seeds, so two candidates
face identical arrivals, identical trajectories and identical bed releases. RL_READINESS §4.2:
*"Otherwise a 5 % difference is indistinguishable from luck."* Without paired evaluation, CEM's
elite set is mostly the luckiest vectors rather than the best ones, and the mean drifts toward
whatever the noise favoured.

**Only one agent learns.** RL_READINESS §5.3 ③: *"Train one agent against two frozen heuristics
first — if all three learn at once the environment is non-stationary and improvement is
unattributable."* :func:`train` takes the agent under training and leaves the others on the
heuristic, so an improvement in return is attributable to the thing that changed.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from random import Random
from typing import Callable, Mapping, Sequence

from allocation.config import Config
from allocation.contracts import AgentKind, BiddingPolicy, Candidate, QAction
from allocation.policy.heuristic import HeuristicPolicy
from allocation.rl.encoder import StateEncoder
from allocation.rl.policy import PARAM_COUNT, LinearQPolicy, QWeights
from allocation.sim.dataset import generate
from allocation.sim.fabricated import DEFAULT, FabricationRegister


@dataclass(frozen=True, slots=True)
class Generation:
    """One CEM iteration."""

    index: int
    #: Return of the **top-ranked** candidate under :meth:`Fitness.rank_key` — which since the
    #: abandonment constraint is not always the highest return in the population. A generation
    #: where most candidates violate can show ``best_fitness`` below ``elite_mean``, and that
    #: is the constraint working rather than a bug: a feasible candidate outranks an infeasible
    #: one whatever the two returned. Read it with the ``feas`` column beside it.
    best_fitness: float
    mean_fitness: float
    elite_mean: float
    sigma: float
    #: Mean burn of the learning agent. Watched alongside fitness because a policy that
    #: maximises return while burning 300 % has broken something the return cannot see —
    #: RL_READINESS §4.2's "four metrics, never one".
    burn: float
    win_share: float
    #: Abandonments committed by the best candidate. Reported per generation because a count
    #: that only appears in the final evaluation is a count nobody watches during the run.
    abandonments: int = 0
    #: How many of the population satisfied the abandonment constraint. When this hits zero
    #: the lexicographic key degenerates to return-ranking and the constraint is inert.
    feasible: int = 0
    population: int = 0


@dataclass(frozen=True, slots=True)
class TrainingRun:
    """A completed fit."""

    weights: QWeights
    generations: tuple[Generation, ...]
    agent: AgentKind
    seeds: tuple[int, ...]
    baseline_fitness: float
    encoder_version: str
    fabrication_version: str

    @property
    def improvement(self) -> float:
        """Return relative to the tuned heuristic on identical seeds.

        The only comparison that means anything. An absolute return is a number about this
        simulator; a paired difference against a baseline is a statement about the policy.
        """
        if self.baseline_fitness == 0:
            return 0.0
        best = self.generations[-1].best_fitness if self.generations else 0.0
        return (best - self.baseline_fitness) / abs(self.baseline_fitness)

    def report(self) -> str:
        lines = [
            f"agent under training  {self.agent.value}  (others stay on the heuristic)",
            f"seeds                 {list(self.seeds)}",
            f"encoder_version       {self.encoder_version}",
            f"fabrication_version   {self.fabrication_version}",
            f"heuristic baseline    {self.baseline_fitness:8.2f}",
            "",
            f"  {'gen':>3}  {'best':>8}  {'elite':>8}  {'mean':>8}  {'sigma':>6}  "
            f"{'burn':>6}  {'win':>5}  {'aband':>5}  {'feas':>7}",
        ]
        for generation in self.generations:
            lines.append(
                f"  {generation.index:>3}  {generation.best_fitness:>8.2f}  "
                f"{generation.elite_mean:>8.2f}  {generation.mean_fitness:>8.2f}  "
                f"{generation.sigma:>6.3f}  {generation.burn:>6.1%}  "
                f"{generation.win_share:>5.0%}  {generation.abandonments:>5}  "
                f"{generation.feasible:>3}/{generation.population:<3}"
            )
        final = self.generations[-1].best_fitness if self.generations else 0.0
        lines += [
            "",
            f"final                 {final:8.2f}   "
            f"({self.improvement:+.1%} vs heuristic on the same seeds)",
        ]
        if self.generations and self.generations[-1].abandonments:
            lines.append(
                f"  ⚠ the selected policy abandons {self.generations[-1].abandonments} "
                "patient(s) — the constraint could not be satisfied by any candidate."
            )
        starved = [g.index for g in self.generations if g.population and not g.feasible]
        if starved:
            lines.append(
                f"  ⚠ generations {starved} had NO candidate inside the abandonment "
                "constraint; selection there was plain return-ranking."
            )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Fitness:
    """What one candidate scored, on the four metrics that matter."""

    ret: float
    burn: float
    win_share: float
    #: Abandonments **by the learning agent only**. Previously this carried
    #: ``dataset.abandonments``, which counts every agent — so a candidate could be ranked down
    #: for abandonments the frozen heuristics committed, which is credit assignment for a
    #: decision the candidate did not make.
    abandonments: int
    episodes: int

    @property
    def abandonment_rate(self) -> float:
        """Abandonments per episode, so a threshold does not move with ``shifts``."""
        return self.abandonments / self.episodes if self.episodes else 0.0

    def rank_key(self, max_abandonment_rate: float) -> tuple[int, float]:
        """Selection key: constraint first, return second.

        **Lexicographic rather than a weighted penalty, and that is the whole point.** A penalty
        term prices abandonment against return, which means some quantity of return always buys
        one — and the reward table already prices it (``additional_boarding`` -20,
        ``emergency_escalation`` -20, ``patient_deterioration`` -60 are all live for an agent
        that arranged nothing). Pricing it twice would be double-counting; what was missing is
        not a bigger price but a *refusal to trade*.

        This is the failure the four-metric table was written to catch and could not, because
        CEM sorts on one scalar. A candidate that abandoned six patients while earning enough
        elsewhere to net positive was ranked above one that abandoned none: the sum cannot see
        a dominated sub-behaviour, and every abandonment observed in evaluation was chosen over
        an available ``RE_ENTER_LATER``, scoring at or below zero every time.
        """
        return (int(self.abandonment_rate > max_abandonment_rate), -self.ret)


def evaluate_weights(
    config: Config,
    weights: QWeights,
    agent: AgentKind,
    seeds: Sequence[int],
    shifts: int,
    fab: FabricationRegister,
    encoder: StateEncoder,
) -> Fitness:
    """Run the simulator with this candidate driving ``agent`` and score it.

    Fitness is the mean **discounted** return over the learning agent's complete episodes —
    §21's actual objective. Incomplete episodes are excluded rather than scored as zero: an
    episode with an unobserved term is not an episode that went badly.
    """
    policy = MixedPolicy(config, weights, agent, encoder)
    returns: list[float] = []
    burns: list[float] = []
    wins = total = 0
    abandonments = 0

    for seed in seeds:
        dataset = generate(config, seed=seed, shifts=shifts, policy=policy, fab=fab, encoder=encoder)
        # Counted off the learning agent's own transitions, not ``dataset.abandonments``,
        # which pools every agent. The candidate is only answerable for its own exits.
        abandonments += sum(
            1
            for t in dataset.transitions
            if t.agent is agent and t.q_action is QAction.WITHDRAW_UNPLANNED
        )
        for episode in dataset.complete_episodes:
            if episode.agent is not agent:
                continue
            returns.append(episode.discounted_return)
        for transition in dataset.transitions:
            if transition.agent is not agent:
                continue
            total += 1
            wins += int(transition.won)
            burns.append(transition.burn_rate)

    return Fitness(
        ret=statistics.fmean(returns) if returns else -1e9,
        burn=statistics.fmean(burns) if burns else 0.0,
        win_share=wins / total if total else 0.0,
        abandonments=abandonments,
        episodes=len(returns),
    )


class MixedPolicy:
    """The learning agent uses the network; everybody else stays on the heuristic.

    One object rather than per-agent policies because the auction takes a single policy and
    asks it about each bidder in turn. Routing inside keeps the auction unchanged and keeps the
    experiment valid: two of the three bidders are genuinely frozen, so a change in return is
    attributable to the third.
    """

    def __init__(
        self,
        config: Config,
        weights: QWeights,
        agent: AgentKind,
        encoder: StateEncoder | None = None,
    ) -> None:
        self._learner = LinearQPolicy(config, weights, encoder)
        self._baseline = HeuristicPolicy(config)
        self._agent = agent
        self.name = f"rl:{agent.value}"

    def decide(self, candidate: Candidate, *args, **kwargs):
        target = self._learner if candidate.agent is self._agent else self._baseline
        return target.decide(candidate, *args, **kwargs)

    def decide_q(self, candidate: Candidate, *args, **kwargs):
        target = self._learner if candidate.agent is self._agent else self._baseline
        return target.decide_q(candidate, *args, **kwargs)


def train(
    config: Config,
    agent: AgentKind = AgentKind.ER,
    generations: int = 8,
    population: int = 24,
    elite_fraction: float = 0.25,
    seeds: Sequence[int] = (11, 12, 13),
    shifts: int = 6,
    sigma: float = 0.6,
    sigma_decay: float = 0.88,
    #: Floor under the per-parameter spread. The elite standard deviation is what CEM refits on,
    #: and with an elite of three to six vectors over 161 parameters it collapses fast: the
    #: six-generation run went 0.379 -> 0.029, so by generation 4 the population was sampling a
    #: near-point distribution and the last two generations explored nothing. The old floor of
    #: 1e-3 is a numerical guard, not an exploration one.
    sigma_floor: float = 1e-3,
    seed: int = 0,
    fab: FabricationRegister = DEFAULT,
    on_generation: Callable[[Generation], None] | None = None,
    checkpoint: str | None = None,
    max_abandonment_rate: float = 0.0,
) -> TrainingRun:
    """Fit ``agent``'s policy by CEM.

    The distribution starts at zero mean, which is a policy that scores every action equally and
    bids at ``alpha = 0.5``. Deliberately not initialised at the heuristic: seeding the search at
    the baseline makes any improvement hard to attribute, since the first generation would
    already carry the baseline's hand-written rules.

    ``max_abandonment_rate`` is a constraint on selection, not a term in the objective — see
    :meth:`Fitness.rank_key`. It defaults to zero because the tuned heuristic abandons nobody,
    so any positive rate is a regression against the policy actually in service.
    """
    encoder = StateEncoder()
    rng = Random(seed)
    n_elite = max(2, int(population * elite_fraction))

    mean = [0.0] * PARAM_COUNT
    spread = [sigma] * PARAM_COUNT

    baseline = _baseline_fitness(config, agent, seeds, shifts, fab, encoder)
    history: list[Generation] = []

    for index in range(generations):
        candidates = [
            [rng.gauss(m, s) for m, s in zip(mean, spread)] for _ in range(population)
        ]
        scored: list[tuple[Fitness, list[float]]] = []
        for vector in candidates:
            weights = QWeights.from_flat(vector, encoder.version, fab.version)
            scored.append(
                (
                    evaluate_weights(config, weights, agent, seeds, shifts, fab, encoder),
                    vector,
                )
            )

        # Constraint first, return second. Sorting on ``-ret`` alone is what let a candidate
        # that abandoned patients outrank one that did not.
        scored.sort(key=lambda item: item[0].rank_key(max_abandonment_rate))
        elite = scored[:n_elite]

        # If every candidate violates, selection has silently degraded to plain return-ranking
        # and the constraint is doing nothing. Say so rather than let the run look clean.
        feasible = sum(
            1 for f, _ in scored if f.abandonment_rate <= max_abandonment_rate
        )

        # Refit the sampling distribution to the elite. The standard deviation is recomputed
        # from them rather than merely decayed, so a generation where the elite disagree keeps
        # exploring and one where they converge narrows on its own.
        mean = [statistics.fmean(v[i] for _, v in elite) for i in range(PARAM_COUNT)]
        spread = [
            max(
                sigma_floor,
                (statistics.pstdev([v[i] for _, v in elite]) if len(elite) > 1 else sigma)
                * sigma_decay,
            )
            for i in range(PARAM_COUNT)
        ]

        best = scored[0][0]
        generation = Generation(
            index=index,
            best_fitness=best.ret,
            mean_fitness=statistics.fmean(f.ret for f, _ in scored),
            elite_mean=statistics.fmean(f.ret for f, _ in elite),
            sigma=statistics.fmean(spread),
            burn=best.burn,
            win_share=best.win_share,
            abandonments=best.abandonments,
            feasible=feasible,
            population=len(scored),
        )
        history.append(generation)
        if on_generation:
            on_generation(generation)

        # CHECKPOINT EVERY GENERATION. Weights used to be written only after the final
        # generation, so a run that hung — and this loop has hung twice, cause not yet found —
        # lost every generation it had already completed. A partial fit is worth far more than
        # nothing, and the elite mean is monotone enough that generation N-1 is a usable policy.
        if checkpoint is not None:
            QWeights.from_flat(mean, encoder.version, fab.version).save(checkpoint)

    return TrainingRun(
        weights=QWeights.from_flat(mean, encoder.version, fab.version),
        generations=tuple(history),
        agent=agent,
        seeds=tuple(seeds),
        baseline_fitness=baseline,
        encoder_version=encoder.version,
        fabrication_version=fab.version,
    )


def _baseline_fitness(
    config: Config,
    agent: AgentKind,
    seeds: Sequence[int],
    shifts: int,
    fab: FabricationRegister,
    encoder: StateEncoder,
) -> float:
    """The heuristic's discounted return on the same seeds.

    RL_READINESS §4.2 ②: *"If a learned policy cannot beat a well-tuned heuristic, that is a
    finding, not a failure."* Measuring it first means the finding is available either way.
    """
    returns: list[float] = []
    for seed in seeds:
        dataset = generate(config, seed=seed, shifts=shifts, fab=fab, encoder=encoder)
        returns += [
            e.discounted_return for e in dataset.complete_episodes if e.agent is agent
        ]
    return statistics.fmean(returns) if returns else 0.0
