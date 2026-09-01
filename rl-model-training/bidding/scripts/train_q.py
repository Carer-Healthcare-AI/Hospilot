"""Train the Q-function by TD learning on the persisted dataset.

Run:  python scripts/build_dataset.py        # writes artifacts/transitions.jsonl
      python scripts/train_q.py              # this

This is the real reinforcement learning, as distinct from ``scripts/train_er.py``, which runs
the cross-entropy method — policy search that never opens a transition.

What to read in the output, in order of how much it tells you:

**Held-out TD error.** The learning curve that matters. Training TD error can fall by
memorising a few hundred rows against 22 features per action; held-out error falling is the
claim that the value function generalises. The split is by *shift*, not by row, because
transitions within a shift are chained through ``next_state`` and a row-wise split would leak a
transition's own successor into the holdout.

**Action coverage.** Which of the six actions actually received updates. This ranks above the
Q-values themselves, because an action the behaviour policy never took keeps an all-zero weight
row and scores exactly 0.0 everywhere — indistinguishable, in the fitted values, from a learned
"worth nothing". Offline fitting cannot fix that: the heuristic that collected the data makes
the same choice however many seeds it runs over. ``scripts/train_q_online.py`` can, because it
collects under epsilon-greedy exploration.

**Q-values by action.** Read after the coverage table, and only for the actions it marks
``learned``.

Exit codes: ``3`` did not converge, ``4`` converged but left a feasible action untrained.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation.config import load_config
from allocation.contracts import AgentKind
from allocation.rl.encoder import ACTIONS, StateEncoder
from allocation.rl.qlearn import QLearner, fit_offline, load_transitions
from allocation.sim.calibrate import _with_base

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "artifacts" / "transitions.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--agent", default="er")
    parser.add_argument("--epochs", type=int, default=400)
    parser.add_argument("--lr", type=float, default=0.02)
    parser.add_argument("--base", type=float, default=120.0)
    # Defaults to <agent>_q_policy.json, which is the artifact evaluated in
    # artifacts/evaluation_q_offline.log and the only one fitted on a heuristic-only corpus. A
    # run against an exploratory dataset MUST pass --out, or it silently replaces that artifact
    # with one carrying the same name and a different provenance.
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    if not Path(args.data).exists():
        print(f"no dataset at {args.data}. Run scripts/build_dataset.py first.")
        return 1

    config = _with_base(load_config(), args.base)
    agent = AgentKind(args.agent)
    transitions, header = load_transitions(args.data, agent=agent)

    print(f"dataset             {args.data}")
    print(f"  caps_version        {header.get('caps_version')}")
    print(f"  encoder_version     {header.get('encoder_version')}")
    print(f"  fabrication_version {header.get('fabrication_version')}")
    print(f"  {agent.value} transitions   {len(transitions)}")

    encoder = StateEncoder()
    if header.get("encoder_version") not in (None, encoder.version):
        print(
            f"\nREFUSING: dataset encodes as {header.get('encoder_version')}, this build as "
            f"{encoder.version}. The vector's features have changed, so the rows describe "
            "different quantities."
        )
        return 2

    usable = [t for t in transitions if t.complete]
    bootstrapping = [t for t in usable if not t.terminal]
    print(f"  complete            {len(usable)}")
    print(f"  with next-state     {len(bootstrapping)} "
          f"({len(bootstrapping) / max(1, len(usable)):.0%} bootstrappable)")
    print(f"  reward mean/sd      {statistics.fmean(t.reward for t in usable):.1f} / "
          f"{statistics.pstdev([t.reward for t in usable]):.1f}")

    print(f"\nfitting: {args.epochs} epochs, lr {args.lr}, gamma "
          f"{config.reward['discount_gamma']}\n")

    fit = fit_offline(
        config, usable, epochs=args.epochs, learning_rate=args.lr,
        fabrication_version=str(header.get("fabrication_version", "")),
    )
    print(fit.report())

    # Mean Q by action. Read it *next to* the coverage table in fit.report(), never alone: a
    # never-updated row scores exactly 0.0 for every state, so it enters this list looking like
    # a confident "worth nothing" and widens the spread rather than narrowing it. The spread
    # test this block used to end with therefore passed most easily on the policies that had
    # learned least — it reported "actions are separated" on a fit that knew two of six.
    learner = QLearner(weights=fit.weights, gamma=float(config.reward["discount_gamma"]))
    untrained = set(fit.untrained)
    print("\n  mean Q by action, over the dataset's states")
    means = {
        action.value: statistics.fmean(learner.q(t.state, action) for t in usable)
        for action in ACTIONS
    }
    for name, value in sorted(means.items(), key=lambda kv: -kv[1]):
        flag = "  <- untrained, not a learned value" if name in untrained else ""
        print(f"    {name:<22} {value:>8.4f}{flag}")

    destination = (
        Path(args.out) if args.out
        else ROOT / "artifacts" / f"{agent.value}_q_policy.json"
    )
    out = fit.weights.save(destination)
    print(f"\nweights written to {out}")

    if not fit.converged:
        return 3
    # Distinct from 3: the value function fitted, but not for every action the policy may be
    # asked to choose. Non-zero because shipping this is the failure mode that looks like
    # success — see qlearn.coverage.
    return 0 if fit.complete else 4


if __name__ == "__main__":
    raise SystemExit(main())
