"""Generate the training dataset and write it to disk.

Run:  python scripts/build_dataset.py [--seeds 40] [--shifts 12]

Until this runs there is no dataset — ``sim/dataset.generate`` builds transitions in memory and
nothing persists them. Output goes to ``artifacts/transitions.jsonl``, one JSON object per line
behind a header line carrying the three versions every episode is only valid under:
``caps_version`` (what the utilities were denominated in), ``encoder_version`` (what the state
vector means) and ``fabrication_version`` (which invented world it happened in).

**Scale matters more than anything else here.** A single 8-shift run yields roughly 50
transitions per department, which is nowhere near enough to fit 22 features per action — the
first TD run against that many samples produced a value function that tracked noise. Many seeds
rather than many shifts: seeds vary the arrival stream, trajectories and bed releases, whereas
extra shifts on one seed re-sample the same world.

``--epsilon`` — ⚠ READ THIS BEFORE USING IT
-------------------------------------------

At the default ``0.0`` the heuristic acts alone and the corpus is the shape of a real hospital
log: only decisions somebody actually made. That property is the whole reason an offline fit is
interesting, because on real patients you cannot explore. ``artifacts/transitions.jsonl`` is that
corpus and this default reproduces it decision for decision.

Not *byte* for byte, and do not reach for a hash to check it: ``auction_id`` is a fresh UUID on
every run, so two runs of identical worlds differ on all 19919 lines and agree on every other
field. Verified by field-comparing a regenerated corpus against the one on disk — the only
differing key was ``auction_id``. Compare these files by field, never by digest.

Above zero, one agent's decisions are replaced by a random *feasible* action with probability
epsilon. That fixes action coverage — the heuristic never takes four of ER's six actions, so a
fit against it leaves four weight rows at exactly zero (see ``artifacts/train_q.log``) — but it
buys the coverage by fabricating decisions no clinician made. **A policy fitted on an exploratory
corpus is a simulator study and cannot be cited as evidence about production.** Exploratory runs
are written to their own ``transitions.eps<NN>.jsonl`` so the log-shaped corpus above is never
overwritten; the epsilon is recorded in the filename because the JSONL header schema carries only
the three versions, not the behaviour policy.

Exploration is scoped to one agent (``--explore-agent``, default ER) so the other two stay frozen
on the heuristic. That matches how ``train_q_online.py`` collects, which is what makes the two
comparable: the only difference left is fixed-epsilon batch collection versus the alternating
collect-fit loop.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.config import load_config
from allocation.contracts import AgentKind, Candidate
from allocation.policy.heuristic import HeuristicPolicy
from allocation.rl.qlearn import EpsilonGreedy
from allocation.sim.calibrate import _with_base
from allocation.sim.dataset import Dataset, generate
from allocation.sim.fabricated import register

OUT = Path(__file__).resolve().parents[1] / "artifacts"


class _ExploreOne:
    """Epsilon-greedy for one agent, the plain heuristic for the others.

    Same routing shape as ``rl.train.MixedPolicy`` and ``rl.qlearn._Routed``, which take
    ``QWeights`` rather than a policy and so cannot wrap an explorer directly. Keeping the other
    two agents frozen is what makes a change in the fitted policy attributable to the third.
    """

    def __init__(
        self, config, epsilon: float, rng: random.Random, agent: AgentKind
    ) -> None:
        self._baseline = HeuristicPolicy(config)
        self._explorer = EpsilonGreedy(HeuristicPolicy(config), epsilon, rng)
        self._agent = agent
        self.name = f"explore:{agent.value}@{epsilon:.2f}"

    @property
    def explored(self) -> int:
        return self._explorer.explored

    @property
    def total(self) -> int:
        return self._explorer.total

    def decide(self, candidate: Candidate, *args, **kwargs):
        target = self._explorer if candidate.agent is self._agent else self._baseline
        return target.decide(candidate, *args, **kwargs)

    def decide_q(self, candidate: Candidate, *args, **kwargs):
        target = self._explorer if candidate.agent is self._agent else self._baseline
        return target.decide_q(candidate, *args, **kwargs)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=40)
    parser.add_argument("--shifts", type=int, default=12)
    parser.add_argument("--base", type=float, default=120.0)
    parser.add_argument("--release", type=float, default=1.8)
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument(
        "--epsilon", type=float, default=0.0,
        help="exploration rate for --explore-agent. 0.0 (default) = heuristic only, the "
             "log-shaped corpus. Above zero fabricates decisions: simulator study only.",
    )
    parser.add_argument("--explore-agent", type=str, default="er", choices=[a.value for a in AgentKind])
    parser.add_argument("--rng", type=int, default=0, help="seed for the exploration draws")
    args = parser.parse_args()

    if not 0.0 <= args.epsilon < 1.0:
        print(f"--epsilon must be in [0.0, 1.0), got {args.epsilon}")
        return 1

    # Exploratory corpora never inherit the default name. transitions.jsonl is the only one that
    # can be cited as log-shaped, and an accidental overwrite would destroy that quietly.
    if args.out is not None:
        out_path = args.out
    elif args.epsilon > 0:
        out_path = str(OUT / f"transitions.eps{round(args.epsilon * 100):02d}.jsonl")
    else:
        out_path = str(OUT / "transitions.jsonl")

    config = _with_base(load_config(), args.base)
    fab = register({
        "arrival.bed_release_per_hour": args.release,
        "arrival.candidate_per_hour": args.release * 2.0,
    })

    explore_agent = AgentKind(args.explore_agent)
    policy = None
    if args.epsilon > 0:
        policy = _ExploreOne(config, args.epsilon, random.Random(args.rng), explore_agent)

    print(
        f"generating {args.seeds} seeds x {args.shifts} shifts "
        f"(Base {args.base:g}, release {args.release:g}/h)"
    )
    print(f"fabrication_version {fab.version}")
    if policy is None:
        print("behaviour policy     heuristic only — log-shaped corpus")
    else:
        print(f"behaviour policy     {policy.name}  (rng {args.rng})")
        print(
            "WARNING: EXPLORATORY CORPUS - decisions no clinician made. A policy fitted on\n"
            "  this is a simulator study, not evidence about production. See module docstring."
        )
    print()

    episodes = []
    transitions = []
    auctions = abandonments = 0

    for index in range(args.seeds):
        dataset = generate(
            config, seed=7000 + index, shifts=args.shifts, policy=policy, fab=fab
        )
        episodes.extend(dataset.episodes)
        transitions.extend(dataset.transitions)
        auctions += dataset.auctions
        abandonments += dataset.abandonments
        if (index + 1) % 5 == 0:
            print(f"  seed {index + 1:>3}/{args.seeds}   {len(transitions):>6} transitions",
                  flush=True)

    first = generate(config, seed=7000, shifts=1, fab=fab)
    combined = Dataset(
        episodes=tuple(episodes),
        transitions=tuple(transitions),
        caps_version=first.caps_version,
        encoder_version=first.encoder_version,
        fabrication_version=fab.version,
        seed=7000,
        auctions=auctions,
        abandonments=abandonments,
    )

    print()
    print(combined.summary())

    print()
    print("  per-agent transitions and TD linkage")
    for agent in AgentKind:
        rows = [t for t in transitions if t.agent is agent]
        if not rows:
            continue
        linked = [t for t in rows if not t.terminal]
        rewards = [t.reward for t in rows]
        print(
            f"    {agent.value:<6} {len(rows):>6} rows  "
            f"{len(linked):>6} with next-state ({len(linked) / len(rows):.0%})  "
            f"reward mean {statistics.fmean(rewards):>7.1f} "
            f"sd {statistics.pstdev(rewards):>6.1f}"
        )

    # The acceptance criterion for an exploratory corpus. A fit can only separate actions the
    # corpus contains: four of ER's six rows came out of the heuristic-only fit at exactly zero.
    # AWAIT_NEXT_RESOURCE is feasible 0% of the time for ER by design — the gate is P(bed inside
    # the safe window) >= 0.70 and ER windows never clear it — so five, not six, is the ceiling.
    from allocation.rl.encoder import ACTIONS

    print()
    print(f"  action coverage for {explore_agent.value}  (the fittable rows)")
    rows = [t for t in transitions if t.agent is explore_agent]
    counts = {a.value: 0 for a in ACTIONS}
    for transition in rows:
        counts[transition.q_action.value] = counts.get(transition.q_action.value, 0) + 1
    present = 0
    for action, count in counts.items():
        if count:
            present += 1
        share = count / len(rows) if rows else 0.0
        note = "" if count else "  <-- ABSENT, its weight row cannot be fitted"
        print(f"    {action:<24} {count:>6}  {share:>5.1%}{note}")
    print(f"\n    {present}/{len(ACTIONS)} actions present"
          f"  (5/6 is the ceiling — await_next_resource is never feasible for er)")

    if policy is not None:
        print(
            f"\n  exploration      {policy.explored}/{policy.total} decisions randomised "
            f"({policy.explored / policy.total:.1%})" if policy.total else ""
        )

    path = combined.write_jsonl(out_path)
    size = path.stat().st_size / 1_048_576
    print(f"\nwritten {path}  ({size:.1f} MB, {len(transitions) + 1} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
