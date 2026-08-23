"""Comparing policies honestly, and testing whether a policy learned the fabrication.

Two things live here, and the second is the more important.

**Paired evaluation on four metrics.** RL_READINESS §4.2: fixed seeds so both policies face the
same arrivals, trajectories and releases; a tuned heuristic as the baseline; and *"four metrics,
never one — return, burn, win share, ranking respect. A policy that maximises return while
burning 300 % or winning 6-of-6 for ER has broken something the return cannot see."*

**The fabrication sweep.** Everything in ``sim/`` is invented, and the outcome model most of all
— *"whatever it encodes is what the policy optimises"*. So the question that decides whether any
result here is worth reporting is not "did return go up" but **"would this policy still behave
this way in a slightly different world?"** :func:`sweep_fabrication` perturbs one outcome
constant at a time and measures how far the policy's behaviour moves. A policy whose action mix
swings when ``outcome.mortality_severity_slope`` is nudged 20 % has fitted a number nobody
measured, and its advantage over the heuristic is an artefact of this simulator.

This is the cheapest falsification available and it is the one that should gate any claim. What
it cannot do is rescue the deeper limitation, which is worth restating because a good sweep
result is exactly the moment somebody will forget it — RL_READINESS §2.B:

    a simulator comparison answers *"which policy captures the pacing structure better"*.
    It never answers *"which policy saves more patients"*.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from allocation.config import Config
from allocation.contracts import AgentKind, BiddingPolicy, QAction
from allocation.policy.heuristic import HeuristicPolicy
from allocation.rl.encoder import StateEncoder
from allocation.rl.policy import LinearQPolicy, QWeights
from allocation.rl.train import MixedPolicy
from allocation.sim.dataset import Dataset, generate
from allocation.sim.fabricated import DEFAULT, FabricationRegister


@dataclass(frozen=True, slots=True)
class Metrics:
    """One policy's behaviour on one set of seeds."""

    label: str
    discounted_return: float
    burn: float
    win_share: Mapping[str, float]
    ranking_respect: float
    #: Fraction of *awarded* auctions where the winner's bid was pinned by the affordability
    #: guard rather than by the ceiling. This separates the two things ranking respect
    #: conflates: a department that already won three beds losing the fourth is the budget
    #: doing its job, whereas a bid clamped below its ceiling mid-auction is F-25 — budget
    #: mechanics deciding who gets a bed.
    affordability_pinned: float
    unallocated: float
    abandonments: int
    action_mix: Mapping[str, float]
    episodes: int
    completeness: float
    #: The learning agent's discounted return per ``(seed, shift_id)``, kept unaggregated so
    #: :class:`Comparison` can pair shift-by-shift. A difference of means throws away the
    #: pairing that makes the comparison worth anything: the shift-to-shift spread here runs
    #: to several hundred points, so an unpaired delta of forty is unreadable.
    returns_by_shift: Mapping[tuple[int, str], float] = field(default_factory=dict)

    def row(self) -> str:
        shares = " ".join(f"{a}:{v:.0%}" for a, v in sorted(self.win_share.items()))
        return (
            f"  {self.label:<14} {self.discounted_return:>8.2f}  {self.burn:>6.1%}  "
            f"{self.ranking_respect:>6.1%}  {self.affordability_pinned:>6.1%}  "
            f"{self.unallocated:>6.1%}  {self.abandonments:>4}   {shares}"
        )


@dataclass(frozen=True, slots=True)
class Comparison:
    """A paired comparison between a learned policy and the baseline."""

    baseline: Metrics
    learned: Metrics
    seeds: tuple[int, ...]
    agent: AgentKind

    @property
    def return_delta(self) -> float:
        base = self.baseline.discounted_return
        return (self.learned.discounted_return - base) / abs(base) if base else 0.0

    @property
    def paired_diffs(self) -> tuple[float, ...]:
        """Learned minus baseline, shift by shift, on the shifts both policies completed."""
        shared = sorted(
            set(self.baseline.returns_by_shift) & set(self.learned.returns_by_shift)
        )
        return tuple(
            self.learned.returns_by_shift[k] - self.baseline.returns_by_shift[k]
            for k in shared
        )

    @property
    def standard_error(self) -> float:
        """SE of the mean paired difference. ``inf`` below two shifts, not zero."""
        diffs = self.paired_diffs
        if len(diffs) < 2:
            return float("inf")
        return statistics.stdev(diffs) / math.sqrt(len(diffs))

    @property
    def t_ratio(self) -> float:
        """Mean paired difference over its standard error.

        The number that decides whether the headline percentage is a finding or a coin flip.
        Under |t| < 2 the observed difference is inside the noise of the shift-to-shift spread
        and the run has not measured anything, however large the percentage looks.
        """
        diffs = self.paired_diffs
        se = self.standard_error
        if not diffs or se == 0 or math.isinf(se):
            return 0.0
        return statistics.fmean(diffs) / se

    @property
    def resolved(self) -> bool:
        """Whether this comparison has enough shifts to support any claim at all."""
        return abs(self.t_ratio) >= 2.0

    @property
    def shifts_needed(self) -> int:
        """Paired shifts required to resolve the observed effect at |t| = 2.

        ``n = (2 * sd / delta)^2``. Printed because "collect more seeds" is not actionable and
        a number is: at the spread this simulator produces, resolving a forty-point difference
        needs hundreds of shifts, and knowing that before a retrain is worth more than
        discovering it after.
        """
        diffs = self.paired_diffs
        if len(diffs) < 2:
            return 0
        mean = statistics.fmean(diffs)
        if mean == 0:
            return 0
        return math.ceil((2.0 * statistics.stdev(diffs) / abs(mean)) ** 2)

    def report(self) -> str:
        diffs = self.paired_diffs
        mean = statistics.fmean(diffs) if diffs else 0.0
        sd = statistics.stdev(diffs) if len(diffs) > 1 else 0.0
        better = sum(1 for d in diffs if d > 0)

        lines = [
            f"paired on seeds {list(self.seeds)}; learning agent {self.agent.value}",
            "",
            f"  {'policy':<14} {'return':>8}  {'burn':>6}  {'rank':>6}  {'pinned':>6}  "
            f"{'noaward':>6}  {'aband':>4}   win share",
            self.baseline.row(),
            self.learned.row(),
            "",
            f"  return {self.return_delta:+.1%} vs heuristic on identical seeds",
            "",
            "  paired per-shift difference",
            f"    shifts compared    {len(diffs)}",
            f"    mean               {mean:+8.2f}",
            f"    sd                 {sd:8.2f}",
            f"    standard error     {self.standard_error:8.2f}",
            f"    t = mean / se      {self.t_ratio:8.2f}",
            f"    learned better on  {better}/{len(diffs)} shifts",
        ]

        if self.resolved:
            lines.append("")
            lines.append("    RESOLVED — the difference is outside the shift-to-shift noise.")
        else:
            lines += [
                "",
                f"    NOT RESOLVED — |t| < 2. The {self.return_delta:+.1%} headline is inside "
                "the noise;",
                "    this run has not measured a difference in either direction.",
            ]
            needed = self.shifts_needed
            if needed:
                lines.append(
                    f"    Resolving an effect this size needs ~{needed} paired shifts "
                    f"({needed / max(1, len(diffs)):.0f}x the current sample)."
                )
        return "\n".join(lines)


def measure(
    config: Config,
    label: str,
    seeds: Sequence[int],
    shifts: int,
    fab: FabricationRegister,
    agent: AgentKind,
    policy: BiddingPolicy | None = None,
    encoder: StateEncoder | None = None,
) -> Metrics:
    """Run the simulator and score every metric that matters."""
    encoder = encoder or StateEncoder()
    datasets = [
        generate(config, seed=s, shifts=shifts, policy=policy, fab=fab, encoder=encoder)
        for s in seeds
    ]

    returns: list[float] = []
    burns: list[float] = []
    wins: dict[str, int] = {}
    actions: dict[str, int] = {}
    ranked = ranked_ok = pinned = awarded = auctions = 0
    abandonments = 0
    episodes = complete = 0
    by_shift: dict[tuple[int, str], float] = {}

    for seed, dataset in zip(seeds, datasets):
        abandonments += dataset.abandonments
        episodes += len(dataset.episodes)
        complete += len(dataset.complete_episodes)
        returns += [
            e.discounted_return for e in dataset.complete_episodes if e.agent is agent
        ]
        for episode in dataset.complete_episodes:
            if episode.agent is agent:
                by_shift[(seed, episode.shift_id)] = episode.discounted_return

        by_auction: dict[str, list] = {}
        for transition in dataset.transitions:
            by_auction.setdefault(transition.auction_id, []).append(transition)
            actions[transition.q_action.value] = actions.get(transition.q_action.value, 0) + 1
            if transition.agent is agent:
                burns.append(transition.burn_rate)
            if transition.won:
                wins[transition.agent.value] = wins.get(transition.agent.value, 0) + 1

        for group in by_auction.values():
            auctions += 1
            won = [t for t in group if t.won]
            if not won:
                continue
            awarded += 1
            ranked += 1
            if won[0].agent is max(group, key=lambda t: t.utility).agent:
                ranked_ok += 1
            # A winning bid materially below its own ceiling, with budget nearly gone, is the
            # affordability guard deciding the auction rather than the ceiling.
            if won[0].ceiling > 0 and won[0].bid < won[0].ceiling * 0.9 and won[0].burn_rate > 0.6:
                pinned += 1

    total_wins = sum(wins.values()) or 1
    total_actions = sum(actions.values()) or 1

    return Metrics(
        label=label,
        discounted_return=statistics.fmean(returns) if returns else 0.0,
        burn=statistics.fmean(burns) if burns else 0.0,
        win_share={a: n / total_wins for a, n in wins.items()},
        ranking_respect=ranked_ok / ranked if ranked else 0.0,
        affordability_pinned=pinned / awarded if awarded else 0.0,
        unallocated=1.0 - (awarded / auctions) if auctions else 0.0,
        abandonments=abandonments,
        action_mix={a: n / total_actions for a, n in actions.items()},
        episodes=episodes,
        completeness=complete / episodes if episodes else 0.0,
        returns_by_shift=by_shift,
    )


def compare(
    config: Config,
    weights: QWeights,
    agent: AgentKind = AgentKind.ER,
    seeds: Sequence[int] = (101, 102, 103),
    shifts: int = 6,
    fab: FabricationRegister = DEFAULT,
) -> Comparison:
    """Learned against heuristic, on **held-out** seeds.

    Held-out is not a nicety. RL_READINESS §6: *"Offline replay on held-out logged auctions,
    never the ones it trained on."* CEM selects on fitness, so the training seeds are exactly
    where the policy is most over-fitted, and a comparison on them measures memorisation.
    """
    encoder = StateEncoder()
    baseline = measure(config, "heuristic", seeds, shifts, fab, agent, None, encoder)
    learned = measure(
        config, "rl-linear", seeds, shifts, fab, agent,
        MixedPolicy(config, weights, agent, encoder), encoder,
    )
    return Comparison(baseline=baseline, learned=learned, seeds=tuple(seeds), agent=agent)


# ---------------------------------------------------------------------------------------
# The falsification sweep
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SweepResult:
    """How far a policy's behaviour moved when one invented constant did."""

    constant: str
    factor: float
    return_shift: float
    action_shift: float

    @property
    def suspicious(self) -> bool:
        """Behaviour that moves more than the perturbation did.

        A policy responding to a 20 % change in an unmeasured constant with a larger change in
        its own action mix is tracking that constant, not the structure around it.
        """
        return self.action_shift > abs(self.factor - 1.0)


@dataclass(frozen=True, slots=True)
class Sweep:
    """The full sweep over the outcome constants."""

    results: tuple[SweepResult, ...]
    baseline_return: float
    #: Action-mix distance between two fits of the SAME world under different CEM seeds. The
    #: noise floor: CEM is stochastic, so a refit sweep with no control cannot distinguish
    #: "the perturbation changed the policy" from "the optimiser landed somewhere else".
    #: ``None`` for the frozen-weights sweep, which has no refitting and so no such noise.
    control_shift: float | None = None

    @property
    def worst(self) -> SweepResult | None:
        return max(self.results, key=lambda r: r.action_shift, default=None)

    def is_suspicious(self, result: SweepResult) -> bool:
        """Did this perturbation move behaviour by more than noise?

        With a control, the bar is the measured refit noise: a perturbation that shifts the
        action mix no further than re-running the same fit with a different seed has shown
        nothing. Without one, falls back to the perturbation's own magnitude.
        """
        if self.control_shift is None:
            return result.suspicious
        return result.action_shift > max(self.control_shift, 1e-9)

    @property
    def passed(self) -> bool:
        return not any(self.is_suspicious(r) for r in self.results)

    @property
    def vacuous(self) -> bool:
        """True when no perturbation moved behaviour at all — a pass that proves nothing.

        **Structural, not a fluke.** Every ``outcome.*`` constant is read inside
        ``sim/outcomes.py``, which scores an episode *after* the auctions have run. None is read
        by ``sim/world.py``, ``sim/patients.py`` or the auction loop, so an outcome constant
        cannot alter the state trajectory a policy sees. A policy with frozen weights therefore
        emits an identical action sequence under any setting of them, and the total-variation
        distance is exactly zero by construction.

        That makes this sweep unable to fail in the mode it is being run in. The test it is
        named for — *did the policy fit numbers nobody measured?* — needs the policy
        **retrained** under each perturbation and the resulting policies compared. Reported as
        vacuous rather than PASS so nobody cites a result the design cannot produce.
        """
        return bool(self.results) and all(r.action_shift == 0.0 for r in self.results)

    def report(self) -> str:
        lines = [
            "fabrication sweep — does the policy track invented constants?",
            "",
            f"  {'constant':<38} {'x':>5}  {'d return':>9}  {'d actions':>9}  verdict",
        ]
        if self.control_shift is not None:
            lines.insert(
                2,
                f"  control (same world, different CEM seed): {self.control_shift:.1%} "
                "action shift — the noise floor every row below is judged against",
            )
        for result in sorted(self.results, key=lambda r: -r.action_shift):
            verdict = "TRACKS THE FABRICATION" if self.is_suspicious(result) else "ok"
            lines.append(
                f"  {result.constant:<38} {result.factor:>5.2f}  "
                f"{result.return_shift:>+9.1%}  {result.action_shift:>9.1%}  {verdict}"
            )
        if self.vacuous:
            lines += [
                "",
                "  VACUOUS — no perturbation moved behaviour by any amount, because no",
                "  outcome constant is read outside sim/outcomes.py: they price an episode",
                "  after it has run and never enter the state a policy sees. A frozen policy",
                "  is invariant to them by construction, so this sweep cannot fail and its",
                "  pass is not evidence. Answering the question it is named for requires",
                "  RETRAINING under each perturbation and comparing the fitted policies.",
            ]
        elif self.passed:
            lines += ["", "  PASS — behaviour is stable under the invented constants"]
        else:
            lines += [
                "",
                "  FAIL — the policy's behaviour is driven by numbers nobody measured; its "
                "advantage over the heuristic is an artefact of this simulator",
            ]
        return "\n".join(lines)


def sweep_fabrication(
    config: Config,
    weights: QWeights,
    agent: AgentKind = AgentKind.ER,
    seeds: Sequence[int] = (201, 202),
    shifts: int = 5,
    fab: FabricationRegister = DEFAULT,
    factors: Sequence[float] = (0.8, 1.2),
) -> Sweep:
    """Perturb each outcome constant and measure the behavioural response.

    Only ``kind == "outcome"`` constants are swept. The dynamics and arrival constants shape the
    *state distribution*, and a policy is supposed to respond to that — a world with sicker
    patients should be bid differently. The outcome constants are the *objective*, and a policy
    that changes its behaviour when they move has learned the reward's shape rather than the
    allocation problem's.
    """
    encoder = StateEncoder()
    policy = MixedPolicy(config, weights, agent, encoder)
    base = measure(config, "base", seeds, shifts, fab, agent, policy, encoder)

    results: list[SweepResult] = []
    for constant in fab.outcome_constants:
        for factor in factors:
            # ``fab.perturbed``, NOT ``register({...})``. The latter rebuilds from the
            # module-level defaults and silently discards every override in ``fab`` — so a
            # sweep run against a calibrated world (Base 120, release 1.8/h) reverted arrivals
            # to 0.55/h and 1.3/h alongside the intended nudge. The arrival change dominated,
            # every perturbed run measured the same confound, and the sweep reported an
            # identical 8.3% action shift for all fourteen rows while passing on a threshold
            # of 20%. A falsification test that cannot fail is worse than no test.
            perturbed = fab.perturbed(constant.name, factor)
            moved = measure(
                config, "perturbed", seeds, shifts, perturbed, agent, policy, encoder
            )
            results.append(
                SweepResult(
                    constant=constant.name,
                    factor=factor,
                    return_shift=_relative(base.discounted_return, moved.discounted_return),
                    action_shift=_mix_distance(base.action_mix, moved.action_mix),
                )
            )

    return Sweep(results=tuple(results), baseline_return=base.discounted_return)


# ---------------------------------------------------------------------------------------
# The falsification sweep that can actually fail
# ---------------------------------------------------------------------------------------


def refit_sweep(
    config: Config,
    agent: AgentKind = AgentKind.ER,
    train_seeds: Sequence[int] = (11, 12, 13, 14),
    eval_seeds: Sequence[int] = (201, 202),
    shifts: int = 4,
    generations: int = 6,
    population: int = 12,
    fab: FabricationRegister = DEFAULT,
    factors: Sequence[float] = (0.8, 1.2),
    on_step=None,
) -> Sweep:
    """Refit the policy under each perturbed constant, then compare the **fitted policies**.

    This is the test :func:`sweep_fabrication` is named for and structurally cannot perform.
    Perturbing an outcome constant and re-running *frozen* weights moves nothing, because every
    ``outcome.*`` constant is read inside ``sim/outcomes.py`` — which prices an episode after
    the auctions have run — and none is read by ``sim/world.py``, ``sim/patients.py`` or the
    auction loop. An outcome constant cannot reach the state a policy sees, so a fixed policy is
    invariant to it by construction and the sweep always passes.

    The constants are the **objective**, not the dynamics. So the question is not whether one
    policy behaves differently under them, it is whether *training* under them yields a
    different policy. If nudging ``mortality_severity_slope`` by 20 % produces a policy with a
    materially different action mix, then what the previous fit learned was that constant —
    a number nobody measured — and its advantage is an artefact of this simulator.

    Expensive by nature: one full CEM run per perturbation, so ``len(outcome_constants) *
    len(factors)`` runs plus a baseline. That cost is the reason the cheap version existed; it
    is not a reason to read the cheap version as evidence.
    """
    from allocation.rl.train import MixedPolicy as _Mixed, train

    encoder = StateEncoder()

    def fit_and_measure(world: FabricationRegister, label: str, cem_seed: int = 0) -> Metrics:
        run = train(
            config, agent=agent, generations=generations, population=population,
            seeds=train_seeds, shifts=shifts, fab=world, seed=cem_seed,
        )
        # Measured in the BASELINE world on held-out seeds. The policies must be compared on
        # one common distribution — scoring each in the world it was fitted for would confound
        # "the policy changed" with "the world changed".
        return measure(
            config, label, eval_seeds, shifts, fab, agent,
            _Mixed(config, run.weights, agent, encoder), encoder,
        )

    if on_step:
        on_step("baseline", 1.0)
    base = fit_and_measure(fab, "base", cem_seed=0)

    # The control: the same world fitted again under a different CEM seed. Whatever this moves
    # the action mix by is what refitting alone does, and no perturbation below can be called a
    # finding unless it moves behaviour further than this.
    if on_step:
        on_step("control (same world, CEM seed 1)", 1.0)
    control = fit_and_measure(fab, "control", cem_seed=1)
    control_shift = _mix_distance(base.action_mix, control.action_mix)

    results: list[SweepResult] = []
    for constant in fab.outcome_constants:
        for factor in factors:
            if on_step:
                on_step(constant.name, factor)
            moved = fit_and_measure(
                fab.perturbed(constant.name, factor), "perturbed", cem_seed=0
            )
            results.append(
                SweepResult(
                    constant=constant.name,
                    factor=factor,
                    return_shift=_relative(base.discounted_return, moved.discounted_return),
                    action_shift=_mix_distance(base.action_mix, moved.action_mix),
                )
            )

    return Sweep(
        results=tuple(results),
        baseline_return=base.discounted_return,
        control_shift=control_shift,
    )


def _relative(before: float, after: float) -> float:
    return (after - before) / abs(before) if before else 0.0


def _mix_distance(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    """Total variation distance between two action mixes, in ``[0, 1]``.

    Total variation rather than a per-action max: a policy that shifts 5 % from four different
    actions has changed as much as one that shifts 20 % from a single action, and only a
    summed measure sees that.
    """
    keys = set(a) | set(b)
    return sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys) / 2.0
